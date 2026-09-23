#!/usr/bin/env python3
"""Build candidate KVM modules against a private snapshot of the running host.

Does not install modules, change the original source/build, or stop any VM.
The Linux 6.18 reference build is separate; this bundle preserves the currently
running 6.18.31-bf3+ kernel's local source changes for module compatibility.
"""
import argparse
import json
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parent.parent


def run(args, **kw):
    return subprocess.run([str(a) for a in args], check=True, **kw)


def capture(args, **kw):
    return subprocess.check_output([str(a) for a in args], text=True, **kw).strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--running-build", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=24)
    parser.add_argument("--patch", type=Path, default=ROOT / "patches/linux-6.18.31-bf3-kvm-pebs-meminfo.patch")
    parser.add_argument("--resume", action="store_true", help="Build an inspected, already-patched private snapshot after a preparation failure")
    args = parser.parse_args()
    patch = args.patch.resolve()
    if not patch.is_file():
        parser.error(f"Finish and review the PEBS source patch first: {patch}")
    source = ROOT / "build/runtime-kvm-source"
    build = ROOT / "build/runtime-kvm-build"
    bundle = ROOT / "build/runtime-kvm-modules"
    if (source.exists() or build.exists() or bundle.exists()) and not args.resume:
        parser.error("Runtime candidate directories already exist; inspect them instead of overwriting a prior candidate")
    if (bundle / "manifest.json").exists():
        parser.error("A completed candidate already exists; do not overwrite a deployable bundle")
    release = capture(["uname", "-r"])
    expected = (args.running_build / "include/config/kernel.release").read_text().strip()
    if release != expected:
        parser.error(f"Running kernel {release} does not match source build release {expected}")
    head = capture(["git", "-C", args.source, "rev-parse", "HEAD"])
    dirty = subprocess.check_output(["git", "-C", str(args.source), "diff", "--binary", "HEAD"])
    if args.resume:
        if (bundle / "running-source-local-changes.patch").read_bytes() != dirty:
            parser.error("Original source changes differ from the recorded snapshot")
        run(["git", "apply", "--reverse", "--check", patch], cwd=source)
    else:
        source.mkdir(parents=True)
        build.mkdir()
        bundle.mkdir()
        (bundle / "running-source-local-changes.patch").write_bytes(dirty)
        archiver = subprocess.Popen(["git", "-C", str(args.source), "archive", "--format=tar", "HEAD"], stdout=subprocess.PIPE)
        try:
            run(["tar", "-xf", "-", "-C", source], stdin=archiver.stdout)
        finally:
            archiver.stdout.close()
        if archiver.wait():
            raise RuntimeError("git archive failed")
        if dirty:
            run(["git", "apply", "--check", "-"], input=dirty, cwd=source)
            run(["git", "apply", "-"], input=dirty, cwd=source)
        run(["git", "apply", "--check", patch], cwd=source)
        run(["git", "apply", patch], cwd=source)
    shutil.copy2(args.running_build / ".config", build / ".config")
    # Signatures and BTF do not alter the KVM runtime ABI; no signing key is copied.
    run([source / "scripts/config", "--file", build / ".config",
         "--disable", "LOCALVERSION_AUTO", "--disable", "MODULE_SIG_ALL",
         "--disable", "DEBUG_INFO_BTF_MODULES"])
    base = ["make", "-C", source, f"O={build}", "CC=gcc", "LOCALVERSION=+"]
    with (ROOT / "results/runtime-kvm-build.log").open("w") as log:
        run(base + ["olddefconfig", "modules_prepare"], stdout=log, stderr=subprocess.STDOUT)
        shutil.copy2(args.running_build / "Module.symvers", build / "Module.symvers")
        run(base + [f"-j{args.jobs}", f"M={source / 'arch/x86/kvm'}",
                    f"MO={build / 'arch/x86/kvm'}", "modules"],
            stdout=log, stderr=subprocess.STDOUT)
    actual = (build / "include/config/kernel.release").read_text().strip()
    if actual != release:
        raise RuntimeError(f"Candidate release mismatch: {actual} != {release}")
    for name in ("kvm.ko", "kvm-intel.ko"):
        shutil.copy2(build / "arch/x86/kvm" / name, bundle / name)
        candidate_version = capture(["modinfo", "-F", "vermagic", bundle / name])
        installed_version = capture(["modinfo", "-F", "vermagic", name[:-3]])
        if not candidate_version.startswith(release + " ") or candidate_version != installed_version:
            raise RuntimeError(f"Candidate vermagic mismatch: {name}")
    shutil.copy2(patch, bundle / patch.name)
    metadata = {
        "running_kernel": release, "snapshot_source": str(args.source),
        "snapshot_head": head, "original_source_modified": False,
        "original_build_modified": False, "installed": False,
        "source_patch": str(patch.relative_to(ROOT)),
        "modules": {name: capture(["modinfo", "-F", "vermagic", bundle / name])
                    for name in ("kvm.ko", "kvm-intel.ko")},
        "signing": "unsigned; running kernel must allow unsigned modules",
        "validation": "compiled and version checked; hardware validation requires an authorized host module reload",
    }
    (bundle / "manifest.json").write_text(json.dumps(metadata, indent=2) + "\n")
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()
