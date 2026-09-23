#!/usr/bin/env python3
"""Record completed builds in independent-repository or monorepo layouts.

Historical component commits are retained from the existing manifest; those
commits need not be objects in the monorepo. The Linux download is optional,
while required configurations, kernel releases, and build artifacts must exist.
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parent.parent
COMPONENTS = ("linux", "qemu", "llfree-c")
REQUIRED_ARTIFACTS = (
    "configs/guest.config", "configs/host.config",
    "build/guest/arch/x86/boot/bzImage", "build/host/arch/x86/boot/bzImage",
    "build/qemu/qemu-system-x86_64", "build/guest-initramfs.cpio.gz",
    "build/host-initramfs.cpio.gz",
)
OPTIONAL_ARTIFACTS = ("downloads/linux-6.18.tar.xz",)


def command(*args):
    return subprocess.check_output(args, cwd=ROOT, text=True).strip()


def optional_git(directory, *args):
    try:
        return subprocess.check_output(
            ("git", "--no-optional-locks", "-C", str(directory), *args),
            cwd=ROOT, text=True, stderr=subprocess.DEVNULL).strip()
    except subprocess.CalledProcessError:
        return None


def git_revision(directory):
    top = optional_git(directory, "rev-parse", "--show-toplevel")
    if top is None:
        return None
    return {
        "root": Path(top).resolve(),
        "head": optional_git(directory, "rev-parse", "--verify", "HEAD"),
        "status": optional_git(directory, "status", "--porcelain", "--", "."),
    }


def source_metadata(previous):
    recorded = previous.get("source_provenance")
    if recorded is None:
        recorded = {
            "manifest_generated_utc": previous.get("generated_utc"),
            "component_commits": {
                repo: previous.get("sources", {}).get(repo, {}).get("head")
                for repo in COMPONENTS
            },
        }
    # Do not relabel the project revision as a historical component commit.
    provenance = {
        **recorded,
        "meaning": "Historical independent component commits, retained from the prior manifest; "
                   "not upstream release commits or the current monorepo revision.",
    }
    project = git_revision(ROOT)
    project_revision = None if project is None else {
        "head": project["head"], "status": project["status"],
        "port_path": ROOT.relative_to(project["root"]).as_posix(),
    }
    sources = {}
    pristine = previous.get("linux_pristine_local_import")
    for repo in COMPONENTS:
        directory = (ROOT / repo).resolve()
        if not directory.is_dir():
            raise FileNotFoundError(f"Required source directory is missing: {directory}")
        revision = git_revision(directory)
        entry = {"provenance_commit": provenance["component_commits"].get(repo)}
        if revision is None:
            entry.update(layout="unversioned", status=None)
        elif revision["root"] == directory:
            entry.update(layout="independent", head=revision["head"], status=revision["status"])
            if repo == "linux":
                pristine = optional_git(directory, "rev-parse", "--verify",
                                        "refs/tags/upstream-v6.18^{commit}") or pristine
        else:
            entry.update(layout="monorepo", project_head=revision["head"],
                         path=directory.relative_to(revision["root"]).as_posix(),
                         status=revision["status"])
        sources[repo] = entry
    return {"sources": sources, "source_provenance": provenance,
            "project_revision": project_revision,
            "linux_pristine_local_import": pristine}


def fingerprint(relative):
    path = ROOT / relative
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"path": relative, "bytes": path.stat().st_size, "sha256": digest.hexdigest()}


def artifact_metadata():
    # Fail on missing build outputs even when the optional archive is absent.
    artifacts = [fingerprint(path) for path in REQUIRED_ARTIFACTS]
    missing = []
    for path in OPTIONAL_ARTIFACTS:
        if (ROOT / path).is_file():
            artifacts.append(fingerprint(path))
        else:
            missing.append(path)
    return {"artifacts": artifacts, "optional_artifacts_absent": missing}


def generate_manifest(previous):
    manifest = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "HyperAlloc port and optional guest PEBS MEMINFO prerequisite; no Chameleon tracker or Hermit",
        "linux_upstream_version": "6.18",
        "linux_tarball_url": "https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.18.tar.xz",
        "hyperalloc_guest_source": "628c12d183414a447e14a30414b1ee6158bf37f6",
        "hyperalloc_qemu_base": "ad01356cf917631416ea518a40f4cb4ce2871d19",
        "llfree_base": "9f10f948556c530b7b8a8a3cab605591c8d51110",
        **source_metadata(previous),
        "releases": {side: (ROOT / f"build/{side}/include/config/kernel.release").read_text().strip()
                     for side in ("host", "guest")},
        "compilers": {cc: command(cc, "--version").splitlines()[0] for cc in ("gcc", "clang")},
        "physical_test_host": command("uname", "-a"),
        **artifact_metadata(),
        "core_vm_reports": {mode: f"results/vm-{mode}/report.json"
                            for mode in ("manual", "auto", "failure", "nested")},
        "core_vm_reports_baseline_sources": {
            "linux": "6cff96baebb6e2f9880468e0b5c6b8f12cdd785f",
            "qemu": "e37a6541f94a48b250b7a37a2abb41ad2f23ba71",
            "llfree-c": "893d745e7650659274c04a19743bc6d7a189be05",
        },
        "pebs_status": "results/pebs-status.json",
    }
    runtime = ROOT / "build/runtime-kvm-modules/manifest.json"
    if runtime.is_file():
        manifest["runtime_kvm_candidate"] = str(runtime.relative_to(ROOT))
    return manifest


def main():
    path = ROOT / "manifest.json"
    previous = json.loads(path.read_text()) if path.is_file() else {}
    manifest = generate_manifest(previous)
    path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(path)


if __name__ == "__main__":
    main()
