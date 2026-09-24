#!/usr/bin/env python3
"""Build nested or standalone Hermit RDMA images; no L0 network changes."""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PREFIX = Path('/opt/hermit-rdma')
ENV = """export PATH=/opt/hermit-rdma/bin:/usr/bin:/usr/sbin:/sbin:/bin
export LD_LIBRARY_PATH=/opt/hermit-rdma/lib
"""
SETUP = '#!/bin/sh\nset -eu\n' + ENV + '''
/sbin/ip link set lo up
/sbin/ip link set "$1" up
/sbin/ip addr replace "$2/24" dev "$1"
if [ ! -d /sys/class/infiniband/rdma0 ]; then
    rdma link add rdma0 type "$3" netdev "$1"
fi
rdma link show
ibv_devinfo -d rdma0
test -c /dev/infiniband/rdma_cm
test -c /dev/infiniband/uverbs0
echo "HERMIT_RDMA_READY interface=$1 address=$2 provider=$3"
'''
BASE_INIT = '#!/bin/sh\n' + ENV + '''
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev
mount -t tmpfs tmpfs /tmp
mount -t debugfs debugfs /sys/kernel/debug
ulimit -l unlimited
'''
GUEST_INIT = BASE_INIT + 'uname -a\necho HERMIT_GUEST_READY\nexec sh\n'
SERVER_INIT = BASE_INIT + '''
echo "HERMIT_SERVER_KERNEL $(uname -r)"
/sbin/hermit-network-setup eth0 192.0.2.1 @PROVIDER@ || {
    echo SKEW_SERVER_FAILED reason=network
    exec sh
}
@SERVER_START@
ready=0
for attempt in $(seq 1 180); do
    kill -0 "$server_pid" 2>/dev/null || break
    # rswap-server prints this only after allocating/touching the complete
    # pool and successfully binding and listening on the RDMA CM endpoint.
    if grep -Fxq 'READY Hermit RDMA 192.0.2.1:9400 pool_bytes=@POOL_BYTES@' /tmp/hermit-server.log; then
        ready=1
        break
    fi
    sleep 1
done
cat /tmp/hermit-server.log
if [ "$ready" != 1 ] || ! kill -0 "$server_pid" 2>/dev/null; then
    echo "SKEW_SERVER_FAILED reason=not_ready pid=$server_pid pool_mib=@SERVER_MIB@"
    exec sh
fi
echo "SKEW_SERVER_READY pid=$server_pid pool_mib=@SERVER_MIB@"
exec sh
'''
HOST_INIT = BASE_INIT + '''
echo "HERMIT_HOST_KERNEL $(uname -r)"
test -c /dev/kvm || { echo HERMIT_NO_KVM; poweroff -f; }
/sbin/ip link set lo up
/sbin/ip link set eth0 up
/sbin/ip addr add 10.0.2.15/24 dev eth0
/sbin/ip route add default via 10.0.2.2
/sbin/ip tuntap add dev hermit0 mode tap
/sbin/ip link set hermit0 address 52:54:00:12:00:01
/sbin/hermit-network-setup hermit0 192.0.2.1 @PROVIDER@ || { echo HERMIT_HOST_RDMA_FAILED; poweroff -f; }
mount -t tracefs tracefs /sys/kernel/tracing
echo 8192 > /sys/kernel/tracing/buffer_size_kb
for event in /sys/kernel/tracing/events/kvm/*chameleon*/enable; do
    [ ! -f "$event" ] || echo 1 > "$event"
done
/usr/bin/qemu-system-x86_64 \
    -machine pc,accel=kvm,mem-merge=off -cpu host -smp @GUEST_CPUS@ -m @GUEST_MIB@M \
    -nodefaults -display none -monitor none -no-reboot -L /usr/share/qemu \
    -kernel /boot/guest-bzImage -initrd /boot/guest-initramfs.cpio.gz \
    -append "console=ttyS0 panic=-1 nokaslr" \
    -S -qmp tcp:0.0.0.0:4444,server=on,wait=off \
    -serial tcp:0.0.0.0:4445,server=on,wait=off \
    -netdev tap,id=hermit,ifname=hermit0,script=no,downscript=no \
    -device virtio-net-pci,netdev=hermit,mac=52:54:00:12:00:02,romfile= \
    -object iothread,id=auto \
    -object iothread,id=install0 -object iothread,id=install1 \
    -object iothread,id=install2 -object iothread,id=install3 \
    -device '{"driver":"virtio-llfree-balloon","id":"ha","auto-mode":false,"chameleon":true,"chameleon-policy":true,"chameleon-batch-pages":529,"chameleon-watermark-bytes":0,"auto-mode-iothread":"auto","iothread-vq-mapping":[{"iothread":"install0"},{"iothread":"install1"},{"iothread":"install2"},{"iothread":"install3"}]}' &
inner_pid=$!
sleep 1
rping -s -P -V -S 65535 -a 192.0.2.1 -p 7471 > /tmp/rping-server.log 2>&1 &
rping_pid=$!
@SERVER_START@
sleep 1
kill -0 "$rping_pid" || { cat /tmp/rping-server.log; echo HERMIT_RPING_SERVER_FAILED; poweroff -f; }
for attempt in $(seq 1 30); do
    kill -0 "$inner_pid" 2>/dev/null || break
    if grep -q ':115C ' /proc/net/tcp && grep -q ':115D ' /proc/net/tcp; then
        echo "HERMIT_NESTED_READY qmp=4444 serial=4445 paused=1 pid=$inner_pid"
        break
    fi
    sleep 1
done
wait "$inner_pid"
inner_status=$?
echo "HERMIT_NESTED_EXIT status=$inner_status"
kill "$rping_pid" 2>/dev/null
echo HERMIT_RPING_SERVER_LOG_BEGIN
cat /tmp/rping-server.log
echo HERMIT_RPING_SERVER_LOG_END
@SERVER_END@
echo HERMIT_HOST_TRACE_BEGIN
cat /sys/kernel/tracing/trace
echo HERMIT_HOST_TRACE_END
echo HERMIT_HOST_DMESG_BEGIN
dmesg
echo HERMIT_HOST_DMESG_END
sleep 1
poweroff -f
'''


class Archive:
    def __init__(self, stage, generator, rdma_root):
        self.stage, self.generator, self.rdma_root = stage, generator, rdma_root
        self.entries, self.libraries = {}, {}

    def directory(self, target):
        target = Path(target)
        if str(target) in ('/', '.') or str(target) in self.entries:
            return
        self.directory(target.parent)
        self.entries[str(target)] = f'dir {target} 0755 0 0'

    def file(self, source, target, mode='0755'):
        source = Path(source).resolve()
        if not source.is_file():
            raise FileNotFoundError(source)
        self.directory(Path(target).parent)
        self.entries[str(target)] = f'file {target} {source} {mode} 0 0'

    def text(self, name, target, value):
        source = self.stage / name
        source.write_text(value)
        self.file(source, target)

    def dynamic(self, source, target):
        self.file(source, target)
        env = dict(os.environ, LD_LIBRARY_PATH=str(self.rdma_root / 'opt/hermit-rdma/lib'))
        result = subprocess.run(['ldd', str(source)], env=env, capture_output=True, text=True)
        if result.returncode or 'not found' in result.stdout:
            raise RuntimeError(f'Unresolved libraries for {source}: {result.stdout}{result.stderr}')
        for library in set(re.findall(r'(?:=>\s+)?(/[^\s()]+)', result.stdout)):
            path = Path(library)
            destination = '/' + str(path.relative_to(self.rdma_root)) if path.is_relative_to(self.rdma_root) else library
            self.file(path, destination)
            self.libraries[destination] = str(path.resolve())

    def common(self):
        for directory in ('/dev', '/proc', '/sys', '/sys/kernel/debug', '/sys/kernel/tracing', '/tmp', '/run', '/root', '/etc'):
            self.directory(directory)
        self.file('/bin/busybox', '/bin/busybox')
        for name in subprocess.check_output(['/bin/busybox', '--list'], text=True).split():
            if name != 'busybox':
                self.entries['/bin/' + name] = f'slink /bin/{name} busybox 0777 0 0'
        for path, major, minor in (('/dev/console', 5, 1), ('/dev/null', 1, 3), ('/dev/tty', 5, 0)):
            self.entries[path] = f'nod {path} 0600 0 0 c {major} {minor}'
        self.dynamic(Path('/sbin/ip'), '/sbin/ip')
        self.dynamic(Path('/usr/bin/rdma'), '/usr/bin/rdma')
        # glibc loads the unwinder at pthread_cancel/exit time, outside ldd.
        self.file('/lib/x86_64-linux-gnu/libgcc_s.so.1', '/lib/x86_64-linux-gnu/libgcc_s.so.1')
        for name in ('rping', 'ibv_devinfo', 'ibv_devices'):
            self.dynamic(self.rdma_root / 'opt/hermit-rdma/bin' / name, PREFIX / 'bin' / name)
        for name in ('rxe', 'siw'):
            library = self.rdma_root / f'opt/hermit-rdma/lib/libibverbs/lib{name}-rdmav57.so'
            self.dynamic(library, PREFIX / 'lib/libibverbs' / library.name)
            configuration = Path(f'/etc/opt/hermit-rdma/libibverbs.d/{name}.driver')
            self.file(self.rdma_root / str(configuration).lstrip('/'), configuration, '0644')
        self.text('network-setup', '/sbin/hermit-network-setup', SETUP)

    def write(self, target, base=None):
        spec = self.stage / 'files.list'
        spec.write_text('\n'.join(self.entries.values()) + '\n')
        raw = subprocess.check_output([str(self.generator), '-t', '0', str(spec)])
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('wb') as output:
            if base:
                output.write(base.read_bytes())
            output.write(gzip.compress(raw, compresslevel=1, mtime=0))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu', type=Path, default=ROOT / 'build/qemu/qemu-system-x86_64')
    parser.add_argument('--guest-kernel', type=Path, default=ROOT / 'build/guest/arch/x86/boot/bzImage')
    parser.add_argument('--guest-initramfs', type=Path, help='Existing image plus tools overlay; otherwise minimal guest')
    parser.add_argument('--guest-extra', action='append', default=[], metavar='SOURCE:DESTINATION')
    parser.add_argument('--guest-mib', type=int, default=2048, help='Nested Guest RAM in MiB')
    parser.add_argument('--guest-cpus', type=int, default=4, help='Nested Guest vCPU count')
    parser.add_argument('--server', type=Path, help='Start Hermit rswap-server at 192.0.2.1:9400')
    parser.add_argument('--server-mib', type=int, default=128)
    parser.add_argument('--server-only', action='store_true',
                        help='Build a standalone eth0 RDMA server without nested QEMU; still create the Guest tools overlay')
    parser.add_argument('--provider', choices=('rxe', 'siw'), default='rxe')
    parser.add_argument('--rdma-root', type=Path, default=ROOT / 'build/hermit-rdma-root')
    parser.add_argument('--gen-init-cpio', type=Path, default=ROOT / 'build/host/usr/gen_init_cpio')
    parser.add_argument('--guest-output', type=Path, default=ROOT / 'build/hermit-network-guest.cpio.gz')
    parser.add_argument('--output', type=Path, default=ROOT / 'build/hermit-network-host.cpio.gz')
    args = parser.parse_args()
    if not 1 <= args.server_mib <= 65536:
        parser.error('server-mib must be 1..65536')
    if args.guest_mib < 1 or args.guest_cpus < 1:
        parser.error('guest-mib and guest-cpus must be positive')
    if args.server_only and args.server is None:
        parser.error('--server-only requires --server')
    for key in ('qemu', 'guest_kernel', 'rdma_root', 'gen_init_cpio', 'guest_initramfs', 'server', 'output', 'guest_output'):
        if getattr(args, key) is not None:
            setattr(args, key, getattr(args, key).resolve())
    if args.guest_initramfs in (args.guest_output, args.output) or args.guest_output == args.output:
        parser.error('Input and output archives must have distinct paths')
    with tempfile.TemporaryDirectory(prefix='hermit-network-') as temporary:
        stage = Path(temporary)
        (stage / 'guest').mkdir()
        guest = Archive(stage / 'guest', args.gen_init_cpio, args.rdma_root)
        guest.common()
        if args.guest_initramfs is None:
            guest.text('init', '/init', GUEST_INIT)
        for extra in args.guest_extra:
            source, target = extra.split(':', 1)
            if not target.startswith('/'):
                parser.error('guest-extra destination must be absolute')
            guest.file(source, target)
        guest.write(args.guest_output, args.guest_initramfs)
        (stage / 'host').mkdir()
        host = Archive(stage / 'host', args.gen_init_cpio, args.rdma_root)
        host.common()
        if not args.server_only:
            host.dynamic(args.qemu, '/usr/bin/qemu-system-x86_64')
            host.file(args.guest_kernel, '/boot/guest-bzImage', '0644')
            host.file(args.guest_output, '/boot/guest-initramfs.cpio.gz', '0644')
            for name in ('bios-256k.bin', 'kvmvapic.bin', 'linuxboot.bin', 'linuxboot_dma.bin', 'pvh.bin'):
                firmware = Path('/usr/share/qemu') / name
                if firmware.is_file():
                    host.file(firmware, '/usr/share/qemu/' + name, '0644')
        init = (SERVER_INIT if args.server_only else HOST_INIT).replace('@PROVIDER@', args.provider)
        init = init.replace('@GUEST_CPUS@', str(args.guest_cpus)).replace('@GUEST_MIB@', str(args.guest_mib))
        init = init.replace('@SERVER_MIB@', str(args.server_mib)).replace('@POOL_BYTES@', str(args.server_mib << 20))
        server_start = server_end = ''
        if args.server:
            host.dynamic(args.server, '/usr/bin/rswap-server')
            server_start = f'/usr/bin/rswap-server 192.0.2.1 9400 {args.server_mib} > /tmp/hermit-server.log 2>&1 &\nserver_pid=$!'
            server_end = 'kill "$server_pid" 2>/dev/null\necho HERMIT_SERVER_LOG_BEGIN\ncat /tmp/hermit-server.log\necho HERMIT_SERVER_LOG_END'
        init = init.replace('@SERVER_START@', server_start).replace('@SERVER_END@', server_end)
        host.text('init', '/init', init)
        host.write(args.output)
        inputs = [args.guest_output, args.output]
        if not args.server_only:
            inputs += [args.qemu, args.guest_kernel]
        if args.server:
            inputs.append(args.server)
        manifest = {'provider': args.provider,
                    'scope': ('Standalone RDMA server eth0 192.0.2.1; no nested QEMU or L0 network changes'
                              if args.server_only else
                              'L1 TAP 192.0.2.1 / L2 virtio-net 192.0.2.2; no L0 network changes'),
                    'server_only': args.server_only, 'server_mib': args.server_mib if args.server else None,
                    'guest_mib': args.guest_mib if not args.server_only else None,
                    'guest_cpus': args.guest_cpus if not args.server_only else None,
                    'guest_output': str(args.guest_output), 'host_output': str(args.output),
                    'host_libraries': host.libraries, 'guest_libraries': guest.libraries,
                    'inputs': {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs}}
        args.output.with_suffix('.manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(f'Created {args.guest_output} and {args.output}')


if __name__ == '__main__':
    main()
