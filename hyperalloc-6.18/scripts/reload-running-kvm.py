#!/usr/bin/env python3
"""Explicit, reversible activation of the prepared KVM module candidate.

Default --check is read-only. --apply requires root, idle KVM, compatible
modules and loadable saved parameters. /lib/modules is never changed.
--restore uses the saved originals even if candidate files are missing or an
interrupted activation left only part of the module pair loaded.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shlex
import struct
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
BUNDLE = ROOT / "build/runtime-kvm-modules"
STATE = BUNDLE / "activation-state.json"
SYS_MODULE = Path("/sys/module")
MODULES = ("kvm", "kvm_intel")
# This callback has sysfs state but does not emit a modinfo parmtype record.
CALLBACK_PARAMETERS = {"kvm": set(), "kvm_intel": {"vmentry_l1d_flush"}}


def output(args):
    return subprocess.check_output([str(a) for a in args], text=True).strip()


def loaded():
    return {m for m in MODULES if (SYS_MODULE / m).exists()}


def parameters(module):
    directory = SYS_MODULE / module / "parameters"
    return {p.name: p.read_text().strip() for p in sorted(directory.iterdir())}


def load_options(raw):
    options = {m: dict(raw[m]) for m in MODULES}
    # The getter returns these derived states, but the setter accepts only
    # auto/never/cond/always. The same hardware/EPT state recomputes the result.
    value = options["kvm_intel"].get("vmentry_l1d_flush")
    if value in ("not required", "EPT disabled"):
        options["kvm_intel"]["vmentry_l1d_flush"] = "auto"
    elif value is not None and value not in ("auto", "never", "cond", "always"):
        raise RuntimeError(f"Unrecognized vmentry_l1d_flush state: {value!r}")
    if options["kvm_intel"].get("guest_pebs_baseline", "N") not in ("N", "0"):
        raise RuntimeError("The broad adaptive-PEBS debug switch must remain off")
    return options


def file_build_id(path):
    match = re.search(r"Build ID:\s*([0-9a-fA-F]+)", output(["readelf", "-n", path]))
    if not match:
        raise RuntimeError(f"Module has no usable build ID: {path}")
    return match.group(1).lower()


def live_build_id(module):
    note = (SYS_MODULE / module / "notes/.note.gnu.build-id").read_bytes()
    if len(note) < 12:
        raise RuntimeError(f"Invalid loaded module build-ID note: {module}")
    namesz, descsz, note_type = struct.unpack_from("=III", note)
    start = 12 + ((namesz + 3) & ~3)
    if note_type != 3 or note[12:12 + namesz] != b"GNU\0" or start + descsz > len(note):
        raise RuntimeError(f"Invalid loaded module build-ID note: {module}")
    return note[start:start + descsz].hex()


def idle_pair():
    if loaded() != set(MODULES):
        raise RuntimeError("Both KVM modules must be loaded for activation")
    holders = {p.name for p in (SYS_MODULE / "kvm/holders").iterdir()}
    refs = {m: int((SYS_MODULE / m / "refcnt").read_text()) for m in MODULES}
    if holders != {"kvm_intel"} or refs != {"kvm": 1, "kvm_intel": 0}:
        raise RuntimeError(f"KVM is in use: holders={holders}, refcounts={refs}. "
                           "Stop VMs and KVM clients before retrying; this script will not stop them.")


def check_module_changes_allowed():
    if Path("/proc/sys/kernel/modules_disabled").read_text().strip() != "0":
        raise RuntimeError("Kernel module changes are disabled")


def verify_files(paths, options, release, expected_ids=None):
    check_module_changes_allowed()
    lockdown = Path("/sys/kernel/security/lockdown")
    locked = lockdown.exists() and "[none]" not in lockdown.read_text()
    sig_file = SYS_MODULE / "module/parameters/sig_enforce"
    sig_required = sig_file.exists() and sig_file.read_text().strip() in ("Y", "1")
    ids = {}
    for module in MODULES:
        path = Path(paths[module])
        if not path.is_file():
            raise RuntimeError(f"Module file is missing: {path}")
        if not output(["modinfo", "-F", "vermagic", path]).startswith(release + " "):
            raise RuntimeError(f"Module release mismatch: {path}")
        declared = {}
        for line in output(["modinfo", "-F", "parmtype", path]).splitlines():
            if ":" in line:
                name, kind = line.split(":", 1)
                declared[name] = kind
        unknown = set(options[module]) - declared.keys() - CALLBACK_PARAMETERS[module]
        if unknown:
            raise RuntimeError(f"{path} lacks saved load parameters: {sorted(unknown)}")
        for name, value in options[module].items():
            kind = declared.get(name)
            if kind in ("bool", "bint") and value not in ("Y", "N", "y", "n", "1", "0"):
                raise RuntimeError(f"Invalid boolean load value {module}.{name}={value!r}")
            if kind in ("int", "uint", "long", "ulong", "ullong", "short", "ushort", "byte"):
                number = int(value, 0)
                if kind in ("uint", "ulong", "ullong", "ushort", "byte") and number < 0:
                    raise RuntimeError(f"Invalid unsigned load value {module}.{name}={value!r}")
        if (locked or sig_required) and not output(["modinfo", "-F", "signer", path]):
            raise RuntimeError(f"Unsigned module cannot load under signature enforcement: {path}")
        ids[module] = file_build_id(path)
        if expected_ids and ids[module] != expected_ids[module]:
            raise RuntimeError(f"Saved module file changed: {path}")
    return ids


def load_one(module, paths, options):
    subprocess.run(["insmod", str(paths[module])] +
                   [f"{name}={value}" for name, value in options[module].items()], check=True)


def load_pair(paths, options):
    for module in MODULES:
        load_one(module, paths, options)


def unload_pair():
    # rmmod itself checks references again; never force removal after a race.
    idle_pair()
    subprocess.run(["rmmod", "kvm_intel"], check=True)
    subprocess.run(["rmmod", "kvm"], check=True)


def verify_active(expected_ids, raw_parameters):
    if loaded() != set(MODULES):
        raise RuntimeError("KVM module pair is incomplete after load")
    for module in MODULES:
        if live_build_id(module) != expected_ids[module]:
            raise RuntimeError(f"Unexpected active {module} build ID")
        actual = parameters(module)
        differences = {name: (value, actual.get(name))
                       for name, value in raw_parameters[module].items()
                       if actual.get(name) != value}
        if differences:
            raise RuntimeError(f"{module} parameters did not restore: {differences}")


def restore_original(state):
    release = output(["uname", "-r"])
    if state["running_kernel"] != release:
        raise RuntimeError("Saved activation belongs to a different running kernel")
    paths = state["original_modules"]
    options = load_options(state["parameters"])
    original_ids = verify_files(paths, options, release, state.get("original_build_ids"))
    candidate_ids = state.get("candidate_build_ids", {})
    present = loaded()
    if "kvm_intel" in present and "kvm" not in present:
        raise RuntimeError("Unexpected backend loaded without KVM core; inspect before restoring")
    current = {m: live_build_id(m) for m in present}
    for module, identity in current.items():
        if identity not in (original_ids[module], candidate_ids.get(module)):
            raise RuntimeError(f"Unrecognized loaded {module}; will not remove another module build")
    if present == set(MODULES) and current == original_ids:
        # Unload may have failed before changing anything. No reload needed.
        verify_active(original_ids, state["parameters"])
        return
    if present == set(MODULES):
        unload_pair()
        present = loaded()
    if present == {"kvm"}:
        if current["kvm"] == original_ids["kvm"]:
            # Backend removal succeeded but original core removal failed, or
            # restore was interrupted after reloading the original core.
            load_one("kvm_intel", paths, options)
            verify_active(original_ids, state["parameters"])
            return
        holders = list((SYS_MODULE / "kvm/holders").iterdir())
        refs = int((SYS_MODULE / "kvm/refcnt").read_text())
        if holders or refs:
            raise RuntimeError(f"Candidate KVM core still busy: holders={holders}, refcount={refs}; state retained")
        subprocess.run(["rmmod", "kvm"], check=True)
    load_pair(paths, options)
    verify_active(original_ids, state["parameters"])


def archive_state(kind):
    STATE.rename(BUNDLE / f"activation-{kind}-{time.time_ns()}.json")


def prepare_candidate():
    release = output(["uname", "-r"])
    metadata = json.loads((BUNDLE / "manifest.json").read_text())
    if metadata["running_kernel"] != release:
        raise RuntimeError("Candidate was built for a different running kernel")
    idle_pair()
    raw = {m: parameters(m) for m in MODULES}
    options = load_options(raw)
    originals = {m: output(["modinfo", "-F", "filename", m]) for m in MODULES}
    candidates = {"kvm": str(BUNDLE / "kvm.ko"), "kvm_intel": str(BUNDLE / "kvm-intel.ko")}
    original_ids = verify_files(originals, options, release)
    verify_active(original_ids, raw)
    candidate_ids = verify_files(candidates, options, release)
    for module in MODULES:
        original_magic = output(["modinfo", "-F", "vermagic", originals[module]])
        candidate_magic = output(["modinfo", "-F", "vermagic", candidates[module]])
        if original_magic != candidate_magic:
            raise RuntimeError(f"Full vermagic differs for {module}: "
                               f"original={original_magic!r}, candidate={candidate_magic!r}")
    state = {"state_version": 2, "running_kernel": release,
             "original_modules": originals, "parameters": raw, "load_options": options,
             "original_build_ids": original_ids, "candidate_build_ids": candidate_ids}
    return state, candidates


def main():
    global BUNDLE, STATE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path,
                        help="prepared module directory; defaults to build/runtime-kvm-modules")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--check", action="store_true")
    action.add_argument("--apply", action="store_true")
    action.add_argument("--restore", action="store_true")
    args = parser.parse_args()
    if args.bundle is not None:
        BUNDLE = args.bundle.resolve()
        STATE = BUNDLE / "activation-state.json"
    if (args.apply or args.restore) and os.geteuid() != 0:
        parser.error("Host activation/restoration requires root; run this explicit action with sudo")
    # Recovery deliberately does not inspect candidate files or require both
    # modules to exist. The saved original paths and identities are enough.
    if args.restore:
        saved = json.loads(STATE.read_text())
        restore_original(saved)
        archive_state("restored")
        print("Original KVM modules restored; /lib/modules was never changed.")
        return
    if STATE.exists():
        parser.error("An activation record already exists; inspect or --restore before another activation/check")
    state, candidates = prepare_candidate()
    if not args.apply:
        print(json.dumps({"status": "READY", "mutated": False, **state}, indent=2))
        return
    temp = STATE.with_suffix(".tmp")
    temp.write_text(json.dumps(state, indent=2) + "\n")
    os.replace(temp, STATE)
    try:
        unload_pair()
        load_pair(candidates, state["load_options"])
        verify_active(state["candidate_build_ids"], state["parameters"])
    except (Exception, KeyboardInterrupt) as activation_error:
        try:
            restore_original(state)
        except (Exception, KeyboardInterrupt) as restore_error:
            raise RuntimeError(f"Activation failed ({activation_error}); automatic restore stopped "
                               f"({restore_error}). Recovery state retained at {STATE}") from activation_error
        archive_state("rolled-back")
        raise
    print("Candidate KVM modules active. Run tests/kvm-pebs-control, then the required PEBS VM probe.")
    bundle_option = " --bundle " + shlex.quote(str(BUNDLE)) if args.bundle is not None else ""
    print("Restore after stopping VMs: sudo python3 scripts/reload-running-kvm.py" +
          bundle_option + " --restore")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
