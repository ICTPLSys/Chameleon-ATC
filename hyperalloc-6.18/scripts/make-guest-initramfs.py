#!/usr/bin/env python3
"""Create a self-contained test guest; no root privileges or disk image required."""
import gzip
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"
stage = BUILD / "guest-initramfs"
stage.mkdir(exist_ok=True)
init = stage / "init"
init.write_text('''#!/bin/sh
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev
mount -t tmpfs tmpfs /tmp
mount -t debugfs debugfs /sys/kernel/debug
hostname hyperalloc-test
echo HYPERALLOC_GUEST_BOOT
uname -a
if ! insmod /guest_allocator.ko; then
  echo HYPERALLOC_INIT_FAILED
  exec sh
fi
echo HYPERALLOC_READY
exec sh
''')
os.chmod(init, 0o755)
spec = [f"dir /{p} 0755 0 0" for p in
        ("bin", "sbin", "dev", "proc", "sys", "tmp", "etc", "root")]
spec += ["nod /dev/console 0600 0 0 c 5 1", "nod /dev/null 0666 0 0 c 1 3",
         "nod /dev/tty 0666 0 0 c 5 0"]
for target, source in [("/bin/busybox", Path("/bin/busybox")),
                       ("/init", init),
                       ("/memory_test", ROOT / "tests/memory_test"),
                       ("/guest_allocator.ko", ROOT / "tests/guest_allocator.ko")]:
    if not source.is_file():
        raise SystemExit(f"Build missing input first: {source}")
    spec.append(f"file {target} {source} 0755 0 0")
applets = subprocess.check_output(["/bin/busybox", "--list"], text=True).splitlines()
for name in applets:
    if name != "busybox":
        spec.append(f"slink /bin/{name} busybox 0777 0 0")
spec += ["slink /sbin/init ../init 0777 0 0", "slink /etc/mtab /proc/mounts 0777 0 0"]
extra_files = sorted((ROOT / "tests/upstream/bin").glob("*"))
for name in ("pmu_probe", "guest-msr-guard", "chameleon_tuning", "chameleon_tracker", "chameleon_manager", "chameleon_shadow", "chameleon_control", "chameleon_modes", "chameleon_policy", "chameleon_hermit", "chameleon_scale", "chameleon_fault_accounting", "chameleon_swap_tracking", "chameleon_commit_rollback", "chameleon_shadow_backend.ko", "chameleon_psi_load.ko"):
    if (ROOT / "tests" / name).is_file():
        extra_files.append(ROOT / "tests" / name)
if (ROOT / "hermit/client/rswap-client.ko").is_file():
    extra_files.append(ROOT / "hermit/client/rswap-client.ko")
if extra_files:
    spec.append("dir /tests 0755 0 0")
    for source in extra_files:
        if source.is_file():
            spec.append(f"file /tests/{source.name} {source} 0755 0 0")
specfile = stage / "files.list"
specfile.write_text("\n".join(spec) + "\n")
generator = BUILD / "host/usr/gen_init_cpio"
archive = subprocess.check_output([str(generator), str(specfile)])
output = BUILD / "guest-initramfs.cpio.gz"
output.write_bytes(gzip.compress(archive, compresslevel=6, mtime=0))
print(f"{output}: {output.stat().st_size} bytes")
