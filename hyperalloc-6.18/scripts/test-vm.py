#!/usr/bin/env python3
"""Real KVM integration tests. All assertions and guest output are retained."""
import argparse
import json
from pathlib import Path
import re
import select
import socket
import subprocess
import threading
import time

ROOT = Path(__file__).resolve().parent.parent
MIB = 1024 * 1024


class Console:
    def __init__(self, read_fd, write_fd, log):
        self.read_fd, self.write_fd = read_fd, write_fd
        self.data = ""
        self.closed = False
        self.log = open(log, "w", buffering=1)
        self.lock = threading.Condition()
        self.count = 0
        threading.Thread(target=self.reader, daemon=True).start()

    def reader(self):
        import os
        while True:
            chunk = os.read(self.read_fd, 65536)
            if not chunk:
                break
            text = chunk.decode(errors="replace").replace("\r", "")
            with self.lock:
                self.data += text
                self.log.write(text)
                self.lock.notify_all()
        with self.lock:
            self.closed = True
            self.lock.notify_all()

    def wait(self, expression, start=0, timeout=120):
        deadline = time.monotonic() + timeout
        with self.lock:
            while True:
                found = re.search(expression, self.data[start:], re.MULTILINE)
                if found:
                    return found, self.data[start:]
                if re.search(r"Kernel panic|kernel BUG|Oops:|Bad page state", self.data[start:]):
                    raise RuntimeError("Guest kernel failed:\n" + self.data[-7000:])
                if self.closed or time.monotonic() >= deadline:
                    raise RuntimeError(f"Console closed/timeout waiting for {expression!r}:\n{self.data[-6000:]}")
                self.lock.wait(min(1, deadline - time.monotonic()))

    def command(self, command, timeout=120):
        import os
        self.count += 1
        token = f"HA_COMMAND_{self.count}_RC"
        with self.lock:
            start = len(self.data)
        os.write(self.write_fd, (command + f"; printf '\\n{token}=%s\\n' \"$?\"\n").encode())
        found, output = self.wait(r"^" + token + r"=(\d+)\s*$", start, timeout)
        if int(found.group(1)):
            raise RuntimeError(f"Guest command failed ({found.group(1)}): {command}\n{output}")
        return output


class QMP:
    def __init__(self, address):
        self.address = address
        deadline = time.monotonic() + 120
        while True:
            self.sock = socket.socket(socket.AF_INET if isinstance(address, tuple) else socket.AF_UNIX)
            try:
                self.sock.connect(address)
                break
            except (ConnectionRefusedError, FileNotFoundError):
                self.sock.close()
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.1)
        self.sock.settimeout(30)
        self.file = self.sock.makefile("rwb", buffering=0)
        self.ident = 0
        self.greeting = json.loads(self.file.readline())
        assert "QMP" in self.greeting, self.greeting
        self.execute("qmp_capabilities")

    def execute(self, command, arguments=None):
        self.ident += 1
        request = {"execute": command, "id": self.ident}
        if arguments is not None:
            request["arguments"] = arguments
        self.file.write((json.dumps(request) + "\n").encode())
        while True:
            raw = self.file.readline()
            if not raw:
                raise RuntimeError("QMP disconnected")
            response = json.loads(raw)
            if response.get("id") == self.ident:
                if "error" in response:
                    raise RuntimeError(response)
                return response["return"]

    def resize(self, mib):
        self.execute("llfree-balloon", {"value": mib * MIB})
        deadline = time.monotonic() + 45
        while True:
            info = self.execute("query-llfree-balloon")
            if info["actual"] == mib * MIB:
                return info
            if time.monotonic() > deadline:
                raise RuntimeError(f"Balloon never reached {mib} MiB: {info}")
            time.sleep(0.1)


def protocol(console):
    output = console.command("cat /proc/llfree_protocol")
    counters = {key: int(value) for key, value in
                re.findall(r"^(install_\w+)\s*[:=]?\s+(\d+)\s*$", output, re.MULTILINE)}
    if not counters:
        raise RuntimeError("Missing LLFree protocol counters: " + output)
    return counters


def meminfo(console):
    output = console.command("cat /proc/meminfo")
    return {key: int(value) for key, value in
            re.findall(r"^(\w+):\s+(\d+) kB$", output, re.MULTILINE)}


def allocator(console, first=0, last=10, loops=4):
    output = console.command(f"echo '{first} {last} {loops}' > /proc/hyperalloc_test && cat /proc/hyperalloc_test")
    if not re.search(r"^PASS run=", output, re.MULTILINE):
        raise RuntimeError("Kernel allocation test failed: " + output)


def run_tests(console, qmp, mode, memory_mib, evidence, rss_reader=None):
    console.wait(r"^HYPERALLOC_READY$", timeout=180)
    console.command("uname -a; cat /proc/meminfo; cat /proc/llfree_protocol")
    evidence["initial_protocol"] = protocol(console)
    evidence["initial_balloon"] = qmp.execute("query-llfree-balloon")
    assert evidence["initial_balloon"]["actual"] == memory_mib * MIB
    allocator(console)
    evidence["orders_0_through_10"] = "PASS"
    output = console.command("/memory_test mthp", timeout=180)
    assert output.count("PASS mthp size_kb=") == 8, output
    assert output.count("PASS split source_kb=") == 9, output
    evidence["mthp_sizes_kb"] = [16, 32, 64, 128, 256, 512, 1024, 2048]
    evidence["native_folio_split"] = "8 sizes to 4 KiB; 2 MiB to 64 KiB; PFNs and all bytes verified"
    touch_mib = memory_mib * 3 // 4
    console.command(f"/memory_test touch {touch_mib}")
    before = protocol(console)
    evidence["before_reclaim_protocol"] = before
    if mode in ("manual", "failure", "nested"):
        evidence["memory_before_hard_reclaim_kib"] = meminfo(console)
        if rss_reader:
            evidence["rss_before_hard_reclaim_kib"] = rss_reader()
        evidence["hard_shrink"] = qmp.resize(memory_mib // 4)
        if rss_reader:
            evidence["rss_after_hard_reclaim_kib"] = rss_reader()
            dropped = evidence["rss_before_hard_reclaim_kib"] - evidence["rss_after_hard_reclaim_kib"]
            assert dropped > memory_mib * 1024 // 4, evidence
        evidence["memory_after_hard_reclaim_kib"] = meminfo(console)
        free_drop = (evidence["memory_before_hard_reclaim_kib"]["MemFree"] -
                     evidence["memory_after_hard_reclaim_kib"]["MemFree"])
        expected_drop = memory_mib * 3 * 1024 // 4
        assert abs(free_drop - expected_drop) < 32 * 1024, (free_drop, expected_drop)
        evidence["hard_return"] = qmp.resize(memory_mib)
        returned_memory = meminfo(console)
        assert abs(returned_memory["MemFree"] - evidence["memory_before_hard_reclaim_kib"]["MemFree"]) < 32 * 1024
        evidence["global_free_memory_accounting"] = "PASS"
        if mode == "failure":
            # A userspace fault can retry ENOMEM. It must never see discarded backing.
            console.command(f"/memory_test touch {touch_mib}", timeout=180)
            evidence["injected_failure_recovery"] = protocol(console)
            assert evidence["injected_failure_recovery"]["install_failure"] > 0
            qmp.resize(memory_mib // 4)
            qmp.resize(memory_mib)
    else:
        # The automatic worker runs periodically. The QEMU log additionally proves discard.
        time.sleep(5)
        evidence["after_soft_balloon"] = qmp.execute("query-llfree-balloon")
        assert evidence["after_soft_balloon"]["actual"] == memory_mib * MIB
    output = console.command(f"echo 'batch 10 {touch_mib // 4}' > /proc/hyperalloc_test && cat /proc/hyperalloc_test", timeout=180)
    assert re.search(r"^PASS batch run=", output, re.MULTILINE), output
    batch_stats = protocol(console)
    assert batch_stats["install_order10"] > before["install_order10"], (before, batch_stats)
    evidence["order10_reinstall_batch"] = batch_stats
    console.command(f"/memory_test touch {touch_mib}", timeout=180)
    after = protocol(console)
    evidence["after_reinstall_protocol"] = after
    assert after["install_success"] > before["install_success"], (before, after)
    # Allocation under concurrent host reclaim must preserve all live contents.
    console.command("/memory_test stress 192 4 15 > /tmp/stress.log 2>&1 & stress_pid=$!")
    for _ in range(4):
        qmp.resize(memory_mib * 3 // 4)
        time.sleep(0.4)
        qmp.resize(memory_mib)
    output = console.command("wait $stress_pid; stress_rc=$?; cat /tmp/stress.log; test $stress_rc = 0", timeout=90)
    assert "PASS stress" in output, output
    evidence["concurrent_allocation_resize"] = "PASS"
    output = console.command("/memory_test mthp", timeout=180)
    assert output.count("PASS mthp size_kb=") == 8, output
    assert output.count("PASS split source_kb=") == 9, output
    evidence["mthp_after_reclaim"] = "PASS"
    allocator(console, 0, 10, 8)
    evidence["final_protocol"] = protocol(console)
    if mode == "failure":
        assert evidence["final_protocol"]["install_failure"] > 0
    else:
        assert evidence["final_protocol"]["install_failure"] == 0
    dmesg = console.command("dmesg")
    bad = re.findall(r"^.*(?:BUG:|WARNING:|kernel BUG|Kernel panic|Oops:|Bad page state|general protection fault).*$", dmesg, re.MULTILINE)
    assert not bad, bad
    evidence["kernel_diagnostics"] = "no BUG/WARNING/Oops/panic/bad-page"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["manual", "auto", "failure"], default="manual")
    parser.add_argument("--memory-mib", type=int, default=2048)
    parser.add_argument("--name")
    parser.add_argument("--extended", action="store_true",
                        help="Also run selected upstream MM tests, STREAM and a PMU probe")
    args = parser.parse_args()
    name = args.name or args.mode
    output = ROOT / "results" / ("vm-" + name)
    output.mkdir(parents=True, exist_ok=True)
    qmp_path = output / "qmp.sock"
    if qmp_path.exists():
        qmp_path.unlink()
    device = {"driver": "virtio-llfree-balloon", "id": "ha", "auto-mode": args.mode == "auto",
              "auto-mode-iothread": "auto", "iothread-vq-mapping":
              [{"iothread": f"install{i}"} for i in range(4)]}
    if args.mode == "failure":
        device["install-fail-count"] = 1
    command = [str(ROOT / "build/qemu/qemu-system-x86_64"), "-L", "/usr/share/qemu",
               "-accel", "kvm", "-cpu", "host", "-m", str(args.memory_mib), "-smp", "4",
               "-nodefaults", "-display", "none", "-serial", "stdio", "-no-reboot",
               "-kernel", str(ROOT / "build/guest/arch/x86/boot/bzImage"),
               "-initrd", str(ROOT / "build/guest-initramfs.cpio.gz"),
               "-append", "console=ttyS0 rdinit=/init panic=1 nokaslr",
               "-qmp", f"unix:{qmp_path},server=on,wait=off"]
    for iothread in ["auto"] + [f"install{i}" for i in range(4)]:
        command += ["-object", f"iothread,id={iothread}"]
    command += ["-device", json.dumps(device)]
    (output / "command.json").write_text(json.dumps(command, indent=2) + "\n")
    evidence = {"mode": args.mode, "status": "RUNNING", "memory_mib": args.memory_mib,
                "physical_host": subprocess.check_output(["uname", "-r"], text=True).strip()}
    log = open(output / "qemu.log", "w", buffering=1)
    proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log)
    console = Console(proc.stdout.fileno(), proc.stdin.fileno(), output / "serial.log")
    try:
        qmp = QMP(str(qmp_path))
        evidence["qemu"] = qmp.greeting
        def rss_reader():
            status = Path(f"/proc/{proc.pid}/status").read_text()
            return int(re.search(r"^VmRSS:\s+(\d+)", status, re.MULTILINE).group(1))
        run_tests(console, qmp, args.mode, args.memory_mib, evidence, rss_reader)
        if args.extended:
            evidence["extended"] = {}
            selected_tests = json.loads((ROOT / "tests/upstream/manifest.json").read_text())["tests"]
            for case in selected_tests:
                test = case["binary"]
                command_args = " ".join(case["args"])
                test_name = test + ("-" + command_args if command_args else "")
                prefix = "mkdir -p /tmp/stream-check && cd /tmp/stream-check && " if test == "stream-check" else ""
                output_text = console.command(prefix + "/tests/" + test + " " + command_args, timeout=240)
                (output / (test_name + ".log")).write_text(output_text)
                assert not re.search(r"^not ok|# SKIP|\b(?:Fail|Timeout|Skip)\b", output_text, re.MULTILINE), output_text
                if "expect_tap_cases_current_config" in case:
                    assert len(re.findall(r"^ok\s+\d+", output_text, re.MULTILINE)) == case["expect_tap_cases_current_config"], output_text
                    assert f"1..{case['expect_tap_cases_current_config']}" in output_text, output_text
                if "expect_marker" in case:
                    assert case["expect_marker"] in output_text, output_text
                if test == "stream-check":
                    assert "Solution Validates" in output_text and "Failed Validation" not in output_text, output_text
                evidence["extended"][test_name] = "PASS"
            output_text = console.command("/tests/pmu_probe", timeout=120)
            (output / "pmu_probe.log").write_text(output_text)
            evidence["extended"]["pmu_probe"] = "capabilities recorded"
            dmesg = console.command("dmesg")
            assert not re.search(r"BUG:|WARNING:|kernel BUG|Kernel panic|Oops:|Bad page state", dmesg)
        qlog = (output / "qemu.log").read_text()
        if args.mode == "auto":
            counts = [int(n) for n in re.findall(r"Auto unmap: (\d+) huge pages", qlog)]
            assert sum(counts) > 0, qlog
            evidence["soft_reclaimed_huge_pages"] = sum(counts)
        if args.mode == "failure":
            assert "injected install failure" in qlog, qlog
        evidence["status"] = "PASS"
        qmp.execute("quit")
        proc.wait(timeout=20)
    except Exception as error:
        evidence["status"] = "FAIL"
        evidence["error"] = repr(error)
        raise
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        log.close()
        (output / "report.json").write_text(json.dumps(evidence, indent=2) + "\n")
        print(json.dumps(evidence, indent=2), flush=True)


if __name__ == "__main__":
    main()
