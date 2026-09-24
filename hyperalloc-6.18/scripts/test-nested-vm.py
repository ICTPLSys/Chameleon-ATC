#!/usr/bin/env python3
"""Exercise HyperAlloc with both guest and host running our Linux 6.18 builds."""

import argparse
import errno
import importlib.util
import json
from pathlib import Path
import re
import socket
import subprocess


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("hyperalloc_vm_tests",
                                            ROOT / "scripts/test-vm.py")
vm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vm)


def reserve_ports():
    sockets = [socket.socket(), socket.socket()]
    try:
        for sock in sockets:
            sock.bind(("127.0.0.1", 0))
        return [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--outer-qemu", type=Path,
                        default=Path("/usr/bin/qemu-system-x86_64"))
    parser.add_argument("--smoke", action="store_true",
                        help="Only prove L1 boot, nested KVM and L2 boot")
    parser.add_argument("--name")
    parser.add_argument("--expect-pebs-control", action="store_true",
                        help="Require patched L1 to reject MEMINFO with EOPNOTSUPP")
    args = parser.parse_args()
    name = args.name or ("nested-smoke" if args.smoke else "nested")
    output = ROOT / "results" / ("vm-" + name)
    output.mkdir(parents=True, exist_ok=True)
    for path in (args.outer_qemu, ROOT / "build/host/arch/x86/boot/bzImage",
                 ROOT / "build/host-initramfs.cpio.gz"):
        if not path.is_file():
            parser.error(f"Missing input: {path}")
    qmp_port, serial_port = reserve_ports()
    command = [str(args.outer_qemu), "-accel", "kvm", "-cpu", "host",
               "-m", "4096", "-smp", "8", "-nodefaults", "-display", "none",
               "-serial", "stdio", "-monitor", "none", "-no-reboot",
               "-kernel", str(ROOT / "build/host/arch/x86/boot/bzImage"),
               "-initrd", str(ROOT / "build/host-initramfs.cpio.gz"),
               "-append", "console=ttyS0 rdinit=/init panic=1 nokaslr",
               "-netdev", "user,id=net0,"
               f"hostfwd=tcp:127.0.0.1:{qmp_port}-:4444,"
               f"hostfwd=tcp:127.0.0.1:{serial_port}-:4445",
               "-device", "virtio-net-pci,netdev=net0"]
    (output / "command.json").write_text(json.dumps(command, indent=2) + "\n")
    evidence = {
        "mode": "nested", "status": "RUNNING", "memory_mib": 2048,
        "scope": "boot smoke only" if args.smoke else "full run_tests",
        "physical_host": subprocess.check_output(["uname", "-r"],
                                                  text=True).strip(),
        "outer_qemu": subprocess.check_output([str(args.outer_qemu),
                                               "--version"], text=True),
        "ports": {"qmp": qmp_port, "serial": serial_port},
    }
    qmp = serial = console = None
    log = open(output / "outer-qemu.log", "w", buffering=1)
    process = subprocess.Popen(command, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=log)
    host = vm.Console(process.stdout.fileno(), process.stdin.fileno(),
                      output / "host-serial.log")
    try:
        host.wait(r"^NESTED_QEMU_READY .*$", timeout=180)
        matched = re.search(r"^NESTED_HOST_KERNEL (\S+)$", host.data, re.MULTILINE)
        assert matched, host.data[-5000:]
        evidence["host_kernel"] = matched.group(1)
        assert re.fullmatch(r"6\.18(?:\.0)?-hyperalloc-host\+?",
                            evidence["host_kernel"])
        evidence["host_kvm_parameters"] = re.findall(
            r"^NESTED_HOST_PARAMETER (.*)$", host.data, re.MULTILINE)
        if args.expect_pebs_control:
            cap = re.search(r"^CAPABILITY id=0x48410001 supported=(\d+)$",
                            host.data, re.MULTILINE)
            rejected = re.search(r"^ENABLE_REJECT errno=(\d+) path=(\S+)$",
                                 host.data, re.MULTILINE)
            finished = re.search(r"^NESTED_PEBS_NEGATIVE_EXIT status=(\d+)$",
                                 host.data, re.MULTILINE)
            assert cap and int(cap.group(1)) == 0, "L1 MEMINFO capability gate"
            assert rejected and int(rejected.group(1)) == errno.EOPNOTSUPP, \
                "Expected patched L1 hardware gate, not an unknown capability"
            assert rejected.group(2) == "hardware_gated_extension"
            assert finished and int(finished.group(1)) == 0, "L1 PEBS control test"
            evidence["pebs_control"] = {
                "status": "PASS", "capability": 0,
                "enable_errno": errno.EOPNOTSUPP,
                "enable_path": rejected.group(2), "exit_status": 0,
                "scope": "negative controls only; positive sampling needs L0 host",
            }

        serial = socket.create_connection(("127.0.0.1", serial_port), timeout=30)
        serial.settimeout(None)
        console = vm.Console(serial.fileno(), serial.fileno(),
                             output / "serial.log")
        qmp = vm.QMP(("127.0.0.1", qmp_port))
        evidence["qemu"] = qmp.greeting
        evidence["nested_kvm"] = qmp.execute("query-kvm")
        assert evidence["nested_kvm"]["enabled"]
        assert evidence["nested_kvm"]["present"]
        qmp.execute("cont")
        console.wait(r"^HYPERALLOC_READY$", timeout=180)
        # Late device INFO messages can split a userspace PASS line on UART.
        # Keep warnings/errors visible and still inspect the complete dmesg
        # buffer at the end of run_tests().
        console.command("dmesg -n 5")
        evidence["guest_console_loglevel"] = 5
        if args.smoke:
            console.wait(r"^HYPERALLOC_READY$", timeout=180)
            console.command("uname -a; cat /proc/llfree_protocol")
            evidence["guest_boot"] = "PASS"
            evidence["guest_protocol"] = vm.protocol(console)
        else:
            vm.run_tests(console, qmp, "nested", 2048, evidence)
        try:
            qmp.execute("quit")
        except RuntimeError as error:
            # L1 powers off after QEMU exits. Usernet can observe the TCP close
            # before the final reply; require successful L2 and L1 exit below.
            if str(error) != "QMP disconnected":
                raise
            evidence["quit_reply"] = "TCP closed during L1 shutdown"
        process.wait(timeout=30)
        assert process.returncode == 0, process.returncode
        host.wait(r"^NESTED_QEMU_EXIT status=0$", timeout=10)
        bad = re.findall(
            r"^.*(?:BUG:|WARNING:|kernel BUG|Kernel panic|Oops:|Bad page state|general protection fault).*$",
            host.data, re.MULTILINE)
        assert not bad, bad
        evidence["host_diagnostics"] = "no BUG/WARNING/Oops/panic/bad-page"
        evidence["status"] = "PASS"
    except Exception as error:
        evidence["status"] = "FAIL"
        evidence["error"] = repr(error)
        raise
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if qmp is not None:
            qmp.file.close()
            qmp.sock.close()
        if serial is not None:
            serial.close()
        log.close()
        (output / "report.json").write_text(json.dumps(evidence, indent=2) + "\n")
        print(json.dumps(evidence, indent=2), flush=True)


if __name__ == "__main__":
    main()
