#!/usr/bin/env python3
"""Boot the accepted HyperAlloc guest once to probe an optional CPU exposure mode."""
import argparse
import importlib.util
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("hyperalloc_vm", ROOT / "scripts/test-vm.py")
vm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vm)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cpu", default="host,migratable=off,pmu=on")
    parser.add_argument("--name", default="pmu")
    parser.add_argument("--initrd", type=Path, default=ROOT / "build/guest-initramfs.cpio.gz")
    parser.add_argument("--kernel", type=Path, default=ROOT / "build/guest/arch/x86/boot/bzImage")
    parser.add_argument("--meminfo", action="store_true", help="Require the host's restricted MEMINFO extension")
    parser.add_argument("--require-pebs", action="store_true", help="Fail unless exact IP, data VA and guest PA all validate")
    parser.add_argument("--regression", action="store_true", help="After PEBS validation, run the HyperAlloc VM regression")
    parser.add_argument("--mthp-kb", type=int, default=0)
    parser.add_argument("--offset", type=int, default=0)
    args = parser.parse_args()
    output = ROOT / "results" / ("vm-" + args.name)
    output.mkdir(parents=True, exist_ok=True)
    qmp_path = output / "qmp.sock"
    if qmp_path.exists():
        qmp_path.unlink()
    device = {"driver": "virtio-llfree-balloon", "id": "ha", "auto-mode": False,
              "auto-mode-iothread": "auto", "iothread-vq-mapping":
              [{"iothread": f"install{i}"} for i in range(4)]}
    command = [str(ROOT / "build/qemu/qemu-system-x86_64"), "-L", "/usr/share/qemu",
               "-accel", "kvm,hyperalloc-pebs-meminfo=on" if args.meminfo else "kvm",
               "-cpu", args.cpu, "-m", "2048", "-smp", "4",
               "-nodefaults", "-display", "none", "-serial", "stdio", "-no-reboot",
               "-kernel", str(args.kernel),
               "-initrd", str(args.initrd),
               "-append", "console=ttyS0 rdinit=/init panic=1 nokaslr",
               "-qmp", f"unix:{qmp_path},server=on,wait=off"]
    for iothread in ["auto"] + [f"install{i}" for i in range(4)]:
        command += ["-object", f"iothread,id={iothread}"]
    command += ["-device", json.dumps(device)]
    (output / "command.json").write_text(json.dumps(command, indent=2) + "\n")
    report = {"cpu": args.cpu, "status": "RUNNING",
              "meminfo_opt_in": args.meminfo,
              "scope": "PEBS address validation" + (" and HyperAlloc regression" if args.regression else "")}
    with open(output / "qemu.log", "w", buffering=1) as log:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log)
        console = vm.Console(process.stdout.fileno(), process.stdin.fileno(), output / "serial.log")
        try:
            qmp = vm.QMP(str(qmp_path))
            console.wait(r"^HYPERALLOC_READY$", timeout=90)
            report["guest_kernel"] = console.command("uname -r")
            if args.meminfo:
                # The guest msr driver otherwise warns on every expected write
                # fault. Its hardware instructions still run and must #GP;
                # the driver also sets a diagnostic taint even on failed writes.
                guard = console.command(
                    "cat /sys/module/msr/parameters/allow_writes > /tmp/msr-allow-before && "
                    "echo on > /sys/module/msr/parameters/allow_writes && "
                    "/tests/guest-msr-guard > /tmp/guest-msr-guard.log 2>&1; guard_rc=$?; "
                    "cat /tmp/msr-allow-before > /sys/module/msr/parameters/allow_writes; restore_rc=$?; "
                    "cat /tmp/guest-msr-guard.log; echo GUEST_MSR_GUARD_EXIT=$guard_rc; "
                    "echo GUEST_MSR_POLICY_RESTORE=$restore_rc; "
                    "cat /proc/sys/kernel/tainted", timeout=30)
                (output / "guest-msr-guard.log").write_text(guard)
                if (not re.search(r"^PASS GUEST_MSR_GUARD\b", guard, re.MULTILINE)
                        or not re.search(r"^GUEST_MSR_GUARD_EXIT=0$", guard, re.MULTILINE)
                        or not re.search(r"^GUEST_MSR_POLICY_RESTORE=0$", guard, re.MULTILINE)):
                    raise RuntimeError("Guest MSR instruction restrictions did not validate")
                report["guest_msr_guard"] = "PASS"
            probe = "/tests/pmu_probe" + (" --require-pebs" if args.require_pebs else "")
            probe += f" --offset {args.offset}"
            if args.mthp_kb:
                probe += f" --mthp-kb {args.mthp_kb}"
            if args.meminfo:
                probe += " --check-fixed-precise"
            result = console.command(probe + " > /tmp/pmu_probe.log 2>&1; probe_rc=$?; cat /tmp/pmu_probe.log; echo PMU_PROBE_EXIT=$probe_rc", timeout=90)
            (output / "pmu_probe.log").write_text(result)
            validation = re.search(r"^PEBS_VALIDATION (.*)$", result, re.MULTILINE)
            fields = dict(re.findall(r"(\w+)=([^\s]+)", validation.group(1))) if validation else {}
            report["pebs_validation"] = fields
            report["pebs_addresses_observed"] = fields.get("virtual_verified") == "1"
            report["pagewalk_counters_observed"] = bool(re.search(r"^PTW pending=.*status=observed$", result, re.MULTILINE))
            report["physical_address_samples_observed"] = fields.get("guest_physical_verified") == "1"
            report["probe_exit"] = int(re.search(r"^PMU_PROBE_EXIT=(\d+)$", result, re.MULTILINE).group(1))
            dmesg = console.command("dmesg")
            report["meminfo_guest_detected"] = "hyperalloc-meminfo-v1" in dmesg
            if args.meminfo and not report["meminfo_guest_detected"]:
                raise RuntimeError("Guest did not recognize the MEMINFO v1 protocol")
            if re.search(r"BUG:|WARNING:|kernel BUG|Kernel panic|Oops:|Bad page state", dmesg):
                raise RuntimeError("kernel diagnostics failed")
            report["kernel_diagnostics"] = "clean"
            passed = report["physical_address_samples_observed"] and report["pagewalk_counters_observed"]
            report["status"] = "PASS" if passed else "CAPABILITY_UNAVAILABLE"
            if args.require_pebs and (not passed or report["probe_exit"]):
                raise RuntimeError("PEBS did not validate exact IP, workload VA and guest physical addresses")
            if args.regression:
                if not passed:
                    raise RuntimeError("PEBS must pass before the combined regression")
                report["hyperalloc_regression"] = {}
                vm.run_tests(console, qmp, "manual", 2048, report["hyperalloc_regression"])
            qmp.execute("quit")
            process.wait(timeout=20)
        except Exception as error:
            report["status"] = "FAIL"
            report["error"] = repr(error)
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
