#!/usr/bin/env python3
"""Run Chameleon milestones in a real KVM guest, preserving raw evidence."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("hyperalloc_vm", ROOT / "scripts/test-vm.py")
vm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vm)


def run_milestone(console, output, stage, negative=False):
    executable = {"c1": "chameleon_tracker", "c2": "chameleon_manager", "c3": "chameleon_shadow", "c6": "chameleon_modes", "tuning": "chameleon_tuning"}[stage]
    if stage == "c3":
        console.command("insmod /tests/chameleon_shadow_backend.ko")
    option = " --expect-unavailable" if negative else ""
    command = (f"/tests/{executable}{option} > /tmp/{stage}.log 2>&1; "
               f"rc=$?; cat /tmp/{stage}.log; test $rc = 0")
    start = len(console.data)
    try:
        result = console.command(command, timeout=300)
    except Exception:
        (output / f"{stage}.log").write_text(console.data[start:])
        raise
    (output / f"{stage}.log").write_text(result)
    label = stage.upper()
    found = re.search(r"^PASS CHAMELEON_" + label + r" checks=(\d+)$", result, re.MULTILINE)
    if negative:
        if "PASS CHAMELEON_C1_UNAVAILABLE errno=EOPNOTSUPP" not in result:
            raise RuntimeError("Missing-capability negative test did not pass")
    elif not found:
        raise RuntimeError(f"{label} final PASS marker missing")
    phases = {"c1": ("deterministic", "hardware", "lifecycle"),
              "c2": ("hhh", "split", "collapse", "selector", "lifecycle", "background", "concurrent"),
              "c3": ("prepare", "restore", "lifecycle", "batch", "backend", "backend_async"),
              "c6": ("tracking", "hardware", "split", "selector"), "tuning": ("tracker", "manager")}[stage]
    for phase in (() if negative else phases):
        if not re.search(r"^PASS " + label + " " + phase + r"\b", result, re.MULTILINE):
            raise RuntimeError(f"{label} {phase} evidence missing")
    if stage == "c3":
        console.command("rmmod chameleon_shadow_backend")
    return {"status": "PASS", "checks": int(found.group(1)) if found else None,
            "hardware": [dict(re.findall(r"(\w+)=([^\s]+)", line))
                         for line in result.splitlines() if line.startswith("HARDWARE ")],
            "observation": next((line for line in result.splitlines()
                                 if line.startswith("OBSERVATION ")), None)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=["c1", "c2", "c3", "c6", "tuning"], default="c1")
    parser.add_argument("--name")
    parser.add_argument("--qemu", type=Path, default=ROOT / "build/qemu/qemu-system-x86_64")
    parser.add_argument("--regression", action="store_true")
    parser.add_argument("--negative", action="store_true", help="Require clean rejection without PEBS MEMINFO")
    parser.add_argument("--without-pebs", action="store_true", help="Run C3 without the optional hardware MEMINFO extension")
    parser.add_argument("--tracker-regression", action="store_true", help="Run C1 before the selected later milestone")
    parser.add_argument("--manager-regression", action="store_true", help="Run C2 before the selected later milestone")
    parser.add_argument("--shadow-regression", action="store_true", help="Run C3 before C6 mechanism comparisons")
    args = parser.parse_args()
    if args.negative and args.stage != "c1":
        parser.error("--negative is a C1 capability test")
    if args.without_pebs and (args.stage != "c3" or args.tracker_regression or args.manager_regression):
        parser.error("--without-pebs requires C3 without C1/C2 hardware regression")
    if args.shadow_regression and args.stage != "c6":
        parser.error("--shadow-regression requires C6")
    output = ROOT / "results" / ("vm-" + (args.name or f"chameleon-{args.stage}"))
    output.mkdir(parents=True, exist_ok=True)
    qmp_path = output / "qmp.sock"
    qmp_path.unlink(missing_ok=True)
    kernel = ROOT / "build/guest/arch/x86/boot/bzImage"
    device = {"driver": "virtio-llfree-balloon", "id": "ha", "auto-mode": False,
              "auto-mode-iothread": "auto", "iothread-vq-mapping":
              [{"iothread": f"install{i}"} for i in range(4)]}
    command = [str(args.qemu), "-L", "/usr/share/qemu",
               "-accel", "kvm" if args.negative or args.without_pebs else "kvm,hyperalloc-pebs-meminfo=on",
               "-cpu", "host,migratable=off,pmu=on", "-m", "2048", "-smp", "4",
               "-nodefaults", "-display", "none", "-serial", "stdio", "-no-reboot",
               "-kernel", str(kernel), "-initrd", str(ROOT / "build/guest-initramfs.cpio.gz"),
               "-append", "console=ttyS0 rdinit=/init panic=1 nokaslr",
               "-qmp", f"unix:{qmp_path},server=on,wait=off"]
    for thread in ["auto"] + [f"install{i}" for i in range(4)]:
        command += ["-object", f"iothread,id={thread}"]
    command += ["-device", json.dumps(device)]
    (output / "command.json").write_text(json.dumps(command, indent=2) + "\n")
    report = {"stage": args.stage, "status": "RUNNING", "negative_capability_test": args.negative,
              "pebs_meminfo_enabled": not (args.negative or args.without_pebs),
              "source_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT.parent, text=True).strip(),
              "kernel_sha256": hashlib.sha256(kernel.read_bytes()).hexdigest(),
              "qemu_sha256": hashlib.sha256(args.qemu.read_bytes()).hexdigest(),
              "physical_host": subprocess.check_output(["uname", "-r"], text=True).strip()}
    inputs = [ROOT / "build/guest-initramfs.cpio.gz", ROOT / "tests/chameleon_tracker",
              ROOT / "tests/chameleon_tracker.c", ROOT / "linux/mm/chameleon.c",
              ROOT / "configs/guest.config"]
    if args.stage == "tuning":
        inputs += [ROOT / "tests/chameleon_tuning.c", ROOT / "tests/chameleon_tuning", ROOT / "linux/mm/chameleon_policy.c"]
    if args.stage in ("c2", "c3", "c6", "tuning"):
        inputs += [ROOT / "tests/chameleon_manager", ROOT / "tests/chameleon_manager.c",
                   ROOT / "linux/mm/chameleon_mm.c", ROOT / "linux/mm/khugepaged.c"]
    if args.stage in ("c3", "c6"):
        inputs += [ROOT / "tests/chameleon_shadow", ROOT / "tests/chameleon_shadow.c",
                   ROOT / "tests/chameleon_shadow_backend.ko", ROOT / "tests/chameleon_shadow_backend.c",
                   ROOT / "linux/mm/chameleon_shadow.c", ROOT / "linux/include/linux/chameleon_shadow.h",
                   ROOT / "linux/mm/memory.c", ROOT / "linux/mm/mprotect.c", ROOT / "linux/mm/mremap.c",
                   ROOT / "linux/mm/mlock.c", ROOT / "linux/fs/proc/task_mmu.c",
                   ROOT / "linux/include/linux/swap.h", ROOT / "linux/include/linux/swapops.h",
                   ROOT / "linux/include/linux/mm_types.h", ROOT / "linux/include/linux/mm.h"]
    if args.stage == "c6":
        inputs += [ROOT / "tests/chameleon_modes.c", ROOT / "tests/chameleon_modes",
                   ROOT / "tests/chameleon_policy.c", ROOT / "tests/chameleon_policy",
                   ROOT / "linux/mm/chameleon_policy.c", ROOT / "linux/kernel/sched/psi.c"]
    report["input_sha256"] = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                              for path in inputs}
    report["source_status"] = subprocess.check_output(["git", "status", "--short"], cwd=ROOT.parent, text=True)
    with open(output / "qemu.log", "w", buffering=1) as log:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log)
        console = vm.Console(process.stdout.fileno(), process.stdin.fileno(), output / "serial.log")
        try:
            qmp = vm.QMP(str(qmp_path))
            console.wait(r"^HYPERALLOC_READY$", timeout=90)
            report["guest_kernel"] = console.command("uname -r")
            if args.tracker_regression and args.stage != "c1":
                report["tracker_regression"] = run_milestone(console, output, "c1")
            if args.manager_regression and args.stage not in ("c1", "c2"):
                report["manager_regression"] = run_milestone(console, output, "c2")
            if args.shadow_regression:
                report["shadow_regression"] = run_milestone(console, output, "c3")
            if args.stage == "c6":
                unavailable = console.command('/tests/chameleon_policy --expect-unavailable')
                (output / 'c5-unavailable.log').write_text(unavailable)
                if 'PASS CHAMELEON_C5_UNAVAILABLE errno=EOPNOTSUPP' not in unavailable:
                    raise RuntimeError('C5 missing-capability rejection failed')
                report['policy_unavailable'] = 'PASS'
            report.update(run_milestone(console, output, args.stage, args.negative))
            if args.regression:
                report["hyperalloc_regression"] = {}
                vm.run_tests(console, qmp, "manual", 2048, report["hyperalloc_regression"])
            dmesg = console.command("dmesg")
            (output / "dmesg.log").write_text(dmesg)
            if re.search(r"BUG:|WARNING:|kernel BUG|Kernel panic|Oops:|Bad page state|general protection fault", dmesg):
                raise RuntimeError("kernel diagnostics failed")
            report["kernel_diagnostics"] = "clean"
            report["status"] = "PASS"
            qmp.execute("quit")
            if process.wait(timeout=20):
                raise RuntimeError("QEMU exited unsuccessfully")
        except Exception as error:
            report["status"] = "FAIL"
            report["error"] = repr(error)
            if not console.closed:
                try:
                    diagnostics = console.command("cat /sys/kernel/debug/chameleon/stats; "
                                                  "cat /sys/kernel/debug/chameleon_mm/stats 2>/dev/null; "
                                                  "cat /sys/kernel/debug/chameleon_shadow/stats 2>/dev/null; dmesg", timeout=10)
                    (output / "failure-diagnostics.log").write_text(diagnostics)
                except Exception as diagnostic_error:
                    report["diagnostic_error"] = repr(diagnostic_error)
            raise
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
            print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()
