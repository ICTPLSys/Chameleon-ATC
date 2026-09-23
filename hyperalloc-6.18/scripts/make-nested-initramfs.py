#!/usr/bin/env python3
"""Build a Linux 6.18 L1 host initramfs carrying the tested L2 guest.

No root privileges or host device/configuration changes are required. The
outer VM must expose vmx/svm and a NIC attached to QEMU's 10.0.2.0/24 usernet.
After NESTED_QEMU_READY, connect serial 4445 and QMP 4444, then issue QMP cont.
"""

import argparse
import gzip
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]
INIT = r'''#!/bin/busybox sh
export PATH=/bin:/sbin:/usr/bin:/usr/sbin
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev
mount -t tmpfs tmpfs /tmp
echo "NESTED_HOST_KERNEL $(uname -r)"
echo "NESTED_HOST_KVM_DEVICE"
ls -l /dev/kvm
if [ ! -c /dev/kvm ]; then
    echo NESTED_HOST_FAIL_NO_KVM
    poweroff -f
fi
for parameter in /sys/module/kvm_intel/parameters/nested /sys/module/kvm_amd/parameters/nested; do
    if [ -f "$parameter" ]; then
        echo "NESTED_HOST_PARAMETER $parameter=$(cat "$parameter")"
    fi
done
ip link set lo up
ip link set eth0 up
ip addr add 10.0.2.15/24 dev eth0
ip route add default via 10.0.2.2
echo NESTED_HOST_NETWORK
ip addr show eth0

/usr/bin/qemu-system-x86_64 \
    -machine pc,accel=kvm -cpu host -smp 4 -m 2048M \
    -nodefaults -display none -monitor none -no-reboot \
    -L /usr/share/qemu \
    -kernel /boot/guest-bzImage -initrd /boot/guest-initramfs.cpio.gz \
    -append "console=ttyS0 panic=-1 nokaslr" \
    -S -qmp tcp:0.0.0.0:4444,server=on,wait=off \
    -serial tcp:0.0.0.0:4445,server=on,wait=off \
    -object iothread,id=auto \
    -object iothread,id=install0 -object iothread,id=install1 \
    -object iothread,id=install2 -object iothread,id=install3 \
    -device '{"driver":"virtio-llfree-balloon","id":"ha","auto-mode":false,"auto-mode-iothread":"auto","iothread-vq-mapping":[{"iothread":"install0"},{"iothread":"install1"},{"iothread":"install2"},{"iothread":"install3"}]}' &
inner_pid=$!
ready=0
for attempt in $(seq 1 30); do
    if ! kill -0 "$inner_pid" 2>/dev/null; then
        break
    fi
    if grep -q ':115C ' /proc/net/tcp && grep -q ':115D ' /proc/net/tcp; then
        ready=1
        break
    fi
    sleep 1
done
if [ "$ready" = 1 ]; then
    echo "NESTED_QEMU_READY qmp=4444 serial=4445 paused=1 pid=$inner_pid"
else
    echo NESTED_HOST_FAIL_QEMU_LISTEN
    kill "$inner_pid" 2>/dev/null
fi
wait "$inner_pid"
inner_status=$?
echo "NESTED_QEMU_EXIT status=$inner_status"
# Let the TCP QMP quit response drain before removing the L1 network stack.
sleep 1
sync
poweroff -f
'''


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qemu", type=Path,
                        default=ROOT / "build/qemu/qemu-system-x86_64")
    parser.add_argument("--guest-kernel", type=Path,
                        default=ROOT / "build/guest/arch/x86/boot/bzImage")
    parser.add_argument("--guest-initramfs", type=Path,
                        default=ROOT / "build/guest-initramfs.cpio.gz")
    parser.add_argument("--busybox", type=Path, default=Path("/bin/busybox"))
    parser.add_argument("--gen-init-cpio", type=Path,
                        default=ROOT / "build/host/usr/gen_init_cpio")
    parser.add_argument("--host-module-root", type=Path,
                        help="Deployment package/staging root containing lib/modules for modular KVM")
    parser.add_argument("--output", type=Path,
                        default=ROOT / "build/host-initramfs.cpio.gz")
    parser.add_argument("--test-pebs-control", action="store_true",
                        help="Run the hardware-gated PEBS control negative test in L1")
    parser.add_argument("--chameleon-control-only", action="store_true",
                        help="Run the actual Chameleon KVM control test in L1, then power off")
    parser.add_argument("--chameleon", action="store_true",
                        help="Enable the C4 range device and capture actual L1 transaction traces")
    parser.add_argument("--chameleon-batch-pages", type=int, default=529)
    parser.add_argument("--chameleon-policy", action="store_true",
                        help="Negotiate the optional C5 native PSI capacity-control lease")
    parser.add_argument("--auto-mode", action="store_true",
                        help="Keep legacy HyperAlloc automatic reclaim enabled for lease arbitration tests")
    parser.add_argument("--host-shell", action="store_true", help="Expose L1 test shell while L2 runs (for debugfs integration tests)")
    parser.add_argument("--chameleon-tuning-tests", action="store_true", help="Run Host scalar debugfs tests before standalone KVM control tests")
    args = parser.parse_args()
    if args.host_shell and not args.chameleon:
        parser.error("--host-shell requires --chameleon")
    if args.chameleon_tuning_tests and not args.chameleon_control_only:
        parser.error("--chameleon-tuning-tests requires --chameleon-control-only")
    if args.chameleon and args.chameleon_control_only:
        parser.error("Select either the integrated device or the standalone Host control test")
    if args.chameleon_policy and not args.chameleon:
        parser.error("--chameleon-policy requires --chameleon")
    if args.chameleon_batch_pages < 1:
        parser.error("Chameleon batch threshold must be positive")
    for attr in ("qemu", "guest_kernel", "guest_initramfs", "busybox",
                 "gen_init_cpio"):
        path = getattr(args, attr).resolve()
        if not path.is_file():
            parser.error(f"Required {attr} does not exist: {path}")
        setattr(args, attr, path)

    ldd = subprocess.check_output(["ldd", str(args.qemu)], text=True)
    if "not found" in ldd:
        parser.error("QEMU has missing dynamic libraries:\n" + ldd)
    libraries = sorted(set(re.findall(r"(?:=>\s+)?(/[^\s()]+)", ldd)))
    for library in libraries:
        if not Path(library).is_file():
            parser.error(f"Invalid library from ldd: {library}")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    results = ROOT / "results"
    results.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="nested-initramfs-",
                                     dir=args.output.parent) as temp:
        stage = Path(temp)
        entries = []
        directories = set()

        def directory(path):
            path = Path(path)
            if str(path) in ("/", ".") or str(path) in directories:
                return
            directory(path.parent)
            directories.add(str(path))
            entries.append(f"dir {path} 0755 0 0")

        def add_file(source, target, mode="0644"):
            directory(Path(target).parent)
            entries.append(f"file {target} {source} {mode} 0 0")

        for name in ("/dev", "/proc", "/sys", "/tmp", "/run", "/root"):
            directory(name)
        add_file(args.busybox, "/bin/busybox", "0755")
        for applet in ("sh", "mount", "umount", "cat", "ls", "echo", "ip",
                       "grep", "sleep", "seq", "uname", "sync", "poweroff",
                       "dmesg", "kill", "mkdir", "mknod", "ps", "modprobe"):
            entries.append(f"slink /bin/{applet} busybox 0777 0 0")
        entries += ["nod /dev/console 0600 0 0 c 5 1",
                    "nod /dev/null 0666 0 0 c 1 3"]
        init = stage / "init"
        init_text = INIT
        if args.chameleon:
            trace_start = '''mount -t tracefs tracefs /sys/kernel/tracing
mount -t debugfs debugfs /sys/kernel/debug
echo 8192 > /sys/kernel/tracing/buffer_size_kb
echo 1 > /sys/kernel/tracing/events/kvm/kvm_chameleon/enable
echo CHAMELEON_TRACE_READY
'''
            trace_end = '''echo 0 > /sys/kernel/tracing/tracing_on
echo CHAMELEON_TRACE_BEGIN
cat /sys/kernel/tracing/trace
echo CHAMELEON_TRACE_END
echo CHAMELEON_HOST_DMESG_BEGIN
dmesg
echo CHAMELEON_HOST_DMESG_END
'''
            init_text = init_text.replace("ip link set lo up", trace_start + "ip link set lo up")
            init_text = init_text.replace("-machine pc,accel=kvm", "-machine pc,accel=kvm,mem-merge=off")
            init_text = init_text.replace('"auto-mode":false',
                '"auto-mode":false,"chameleon":true,"chameleon-batch-pages":' +
                str(args.chameleon_batch_pages) + ',"chameleon-watermark-bytes":0')
            init_text = init_text.replace('# Let the TCP QMP quit response',
                                         trace_end + '# Let the TCP QMP quit response')
            if args.chameleon_policy:
                init_text = init_text.replace('"chameleon":true',
                                              '"chameleon":true,"chameleon-policy":true')
        if args.auto_mode:
            init_text = init_text.replace('"auto-mode":false', '"auto-mode":true')
        if args.chameleon_control_only:
            control = ROOT / "tests/kvm-chameleon-control"
            if not control.is_file():
                parser.error("Build tests/kvm-chameleon-control first")
            add_file(control, "/tests/kvm-chameleon-control", "0755")
            directory("/sys/kernel/tracing")
            init_text = INIT[:INIT.index("ip link set lo up")] + '''
mount -t tracefs tracefs /sys/kernel/tracing
mount -t debugfs debugfs /sys/kernel/debug
echo 8192 > /sys/kernel/tracing/buffer_size_kb
trace_ready=0
for event in /sys/kernel/tracing/events/kvm/*chameleon*/enable; do
    if [ -f "$event" ]; then
        echo 1 > "$event"
        trace_ready=1
    fi
done
echo "CHAMELEON_TRACE_READY value=$trace_ready"
/tests/kvm-chameleon-control
control_status=$?
echo "NESTED_CHAMELEON_CONTROL_EXIT status=$control_status"
echo 0 > /sys/kernel/tracing/tracing_on
echo CHAMELEON_TRACE_BEGIN
cat /sys/kernel/tracing/trace
echo CHAMELEON_TRACE_END
echo CHAMELEON_HOST_DMESG_BEGIN
dmesg
echo CHAMELEON_HOST_DMESG_END
sync
poweroff -f
'''
        if args.test_pebs_control:
            control = ROOT / "tests/kvm-pebs-control"
            if not control.is_file():
                parser.error("Build tests/kvm-pebs-control first")
            add_file(control, "/tests/kvm-pebs-control", "0755")
            hook = '''/tests/kvm-pebs-control --expect-unavailable
pebs_status=$?
echo "NESTED_PEBS_NEGATIVE_EXIT status=$pebs_status"
if [ "$pebs_status" != 0 ]; then
    poweroff -f
fi
'''
            init_text = init_text.replace("ip link set lo up", hook + "ip link set lo up")
        if args.host_shell:
            init_text = init_text.replace('wait "$inner_pid"', 'echo NESTED_TUNING_SHELL_READY\nsh\nwait "$inner_pid"')
        if args.chameleon_tuning_tests:
            add_file(ROOT / "tests/kvm-chameleon-tuning", "/tests/kvm-chameleon-tuning", "0755")
            init_text = init_text.replace('/tests/kvm-chameleon-control\ncontrol_status=',
                '/tests/kvm-chameleon-tuning\necho NESTED_TUNING_EXIT=$?\n/tests/kvm-chameleon-control\ncontrol_status=')
        if args.host_module_root:
            modules = args.host_module_root.resolve() / "lib/modules"
            if not modules.is_dir():
                parser.error(f"Missing deployment modules: {modules}")
            for source in sorted(modules.rglob("*")):
                if source.is_file() and not source.is_symlink():
                    add_file(source, "/lib/modules/" + str(source.relative_to(modules)))
            module_init = '''if grep -q GenuineIntel /proc/cpuinfo; then
    modprobe kvm_intel
else
    modprobe kvm_amd
fi
for driver in vfio_pci mlx5_ib ib_ipoib; do
    if ! modprobe "$driver"; then
        echo "NESTED_HOST_MODULE_LOAD_FAILED driver=$driver"
        poweroff -f
    fi
done
echo "NESTED_HOST_MODULES"
cat /proc/modules
'''
            init_text = init_text.replace('echo "NESTED_HOST_KVM_DEVICE"',
                                          module_init + 'echo "NESTED_HOST_KVM_DEVICE"')
        init.write_text(init_text)
        add_file(init, "/init", "0755")
        add_file(args.qemu, "/usr/bin/qemu-system-x86_64", "0755")
        add_file(args.guest_kernel, "/boot/guest-bzImage")
        add_file(args.guest_initramfs, "/boot/guest-initramfs.cpio.gz")
        for library in libraries:
            # gen_init_cpio follows source symlinks, preserving the guest path.
            add_file(Path(library).resolve(), library, "0755")
        firmware = []
        for name in ("bios-256k.bin", "kvmvapic.bin", "linuxboot.bin",
                     "linuxboot_dma.bin", "pvh.bin"):
            path = Path("/usr/share/qemu") / name
            if path.is_file():
                add_file(path.resolve(), f"/usr/share/qemu/{name}")
                firmware.append(name)
            elif name in ("bios-256k.bin", "linuxboot_dma.bin"):
                parser.error(f"Missing required firmware: {path}")

        spec = stage / "cpio.list"
        spec.write_text("\n".join(entries) + "\n")
        command = [str(args.gen_init_cpio), "-t", "0", str(spec)]
        process = subprocess.Popen(command, stdout=subprocess.PIPE)
        with args.output.open("wb") as output:
            with gzip.GzipFile(filename="", mode="wb", fileobj=output,
                               compresslevel=1, mtime=0) as compressed:
                shutil.copyfileobj(process.stdout, compressed, 1024 * 1024)
        if process.wait() != 0:
            args.output.unlink(missing_ok=True)
            raise SystemExit("gen_init_cpio failed")

    subprocess.run(["gzip", "-t", str(args.output)], check=True)
    manifest = {
        "output": str(args.output), "bytes": args.output.stat().st_size,
        "pebs_control_negative": args.test_pebs_control,
        "chameleon_control_only": args.chameleon_control_only,
        "host_module_root": str(args.host_module_root) if args.host_module_root else None,
        "chameleon": args.chameleon,
        "chameleon_policy": args.chameleon_policy,
        "auto_mode": args.auto_mode,
        "chameleon_batch_pages": args.chameleon_batch_pages if args.chameleon else None,
        "libraries": libraries, "firmware": firmware,
        "inputs": {key: {"path": str(getattr(args, key)),
                         "sha256": sha256(getattr(args, key))}
                   for key in ("qemu", "guest_kernel", "guest_initramfs",
                               "busybox")},
        "l2": {"vcpus": 4, "ram_mib": 2048, "accelerator": "kvm",
               "qmp_port": 4444, "serial_port": 4445, "paused": True,
               "hyperalloc_mode": "manual"},
    }
    (results / "nested-initramfs-manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n")
    (results / "nested-host-init.sh").write_text(init_text)
    print(f"Created {args.output} ({args.output.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
