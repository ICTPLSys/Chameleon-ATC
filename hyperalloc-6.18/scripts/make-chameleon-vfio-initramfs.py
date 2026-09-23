#!/usr/bin/env python3
"""Build a real VFIO test using an emulated PCI NIC inside an isolated L1 Host."""
import argparse
import importlib.util
from pathlib import Path
import tempfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('vfio_archive', ROOT / 'scripts/make-hermit-network-initramfs.py')
archive = importlib.util.module_from_spec(spec)
spec.loader.exec_module(archive)

INIT = archive.BASE_INIT + '''
set -eu
modprobe kvm_intel
modprobe vfio_pci
modprobe vfio_iommu_type1 dma_entry_limit=1048576
[ -c /dev/kvm ]
echo "VFIO_HOST_KERNEL $(uname -r)"
/sbin/ip link set lo up
/sbin/ip link set eth0 up
/sbin/ip addr add 10.0.2.15/24 dev eth0
/sbin/ip route add default via 10.0.2.2
pci=0000:01:00.0
[ -d /sys/bus/pci/devices/$pci/iommu_group ]
echo vfio-pci > /sys/bus/pci/devices/$pci/driver_override
if [ -L /sys/bus/pci/devices/$pci/driver ]; then
    echo "$pci" > /sys/bus/pci/devices/$pci/driver/unbind
fi
echo "$pci" > /sys/bus/pci/drivers_probe
[ "$(basename "$(readlink /sys/bus/pci/devices/$pci/driver)")" = vfio-pci ]
echo "VFIO_PCI_READY pci=$pci group=$(basename "$(readlink /sys/bus/pci/devices/$pci/iommu_group)")"
mount -t tracefs tracefs /sys/kernel/tracing
echo 8192 > /sys/kernel/tracing/buffer_size_kb
for event in /sys/kernel/tracing/events/kvm/*chameleon*/enable; do
    [ ! -f "$event" ] || echo 1 > "$event"
done
/usr/bin/qemu-system-x86_64 \
    -machine q35,accel=kvm,mem-merge=off,memory-backend=ram0,max-ram-below-4g=1G \
    -object memory-backend-ram,id=ram0,size=2048M,thp=off,share=off,merge=off \
    -cpu host -smp 4 -m 2048M \
    -nodefaults -display none -monitor none -no-reboot -L /usr/share/qemu \
    -kernel /boot/guest-bzImage -initrd /boot/guest-initramfs.cpio.gz \
    -append "console=ttyS0 panic=-1 nokaslr" \
    -S -qmp tcp:0.0.0.0:4444,server=on,wait=off \
    -serial tcp:0.0.0.0:4445,server=on,wait=off \
    -object iothread,id=auto \
    -object iothread,id=install0 -object iothread,id=install1 \
    -object iothread,id=install2 -object iothread,id=install3 \
    -device '{"driver":"virtio-llfree-balloon","id":"ha","auto-mode":false,"chameleon":true,"chameleon-vfio":true,"chameleon-policy":true,"chameleon-batch-pages":529,"chameleon-watermark-bytes":0,"auto-mode-iothread":"auto","iothread-vq-mapping":[{"iothread":"install0"},{"iothread":"install1"},{"iothread":"install2"},{"iothread":"install3"}]}' \
    -device '{"driver":"pcie-root-port","id":"guestport","chassis":1}' \
    -device '{"driver":"vfio-pci","host":"0000:01:00.0","bus":"guestport","rombar":0}' > /tmp/inner-qemu.log 2>&1 &
inner_pid=$!
for attempt in $(seq 1 180); do
    kill -0 "$inner_pid" 2>/dev/null || { cat /tmp/inner-qemu.log; poweroff -f; }
    if grep -q ':115C ' /proc/net/tcp && grep -q ':115D ' /proc/net/tcp; then
        echo "VFIO_NESTED_READY qmp=4444 serial=4445 paused=1 pid=$inner_pid"
        break
    fi
    sleep 1
done
set +e
wait "$inner_pid"
echo "VFIO_NESTED_EXIT status=$?"
echo VFIO_QEMU_LOG_BEGIN
cat /tmp/inner-qemu.log
echo VFIO_QEMU_LOG_END
echo VFIO_HOST_TRACE_BEGIN
cat /sys/kernel/tracing/trace
echo VFIO_HOST_TRACE_END
echo VFIO_HOST_DMESG_BEGIN
dmesg
echo VFIO_HOST_DMESG_END
sleep 1
poweroff -f
'''

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu', type=Path, default=ROOT / 'build/deploy-qemu/qemu-system-x86_64')
    parser.add_argument('--host-package', type=Path, default=ROOT / 'build/deploy-host/package')
    parser.add_argument('--guest-kernel', type=Path, default=ROOT / 'build/guest/arch/x86/boot/bzImage')
    parser.add_argument('--guest-initramfs', type=Path, default=ROOT / 'build/guest-initramfs.cpio.gz')
    parser.add_argument('--guest-deployment', type=Path,
                        help='Build a matching Guest initramfs from this deployment kernel/package/test-modules/hermit-client tree')
    parser.add_argument('--backend', choices=('dram', 'rdma'), default='dram')
    parser.add_argument('--output', type=Path, default=ROOT / 'build/chameleon-vfio-host.cpio.gz')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='chameleon-vfio-') as temporary:
        stage = Path(temporary)
        host = archive.Archive(stage, ROOT / 'build/deploy-host/usr/gen_init_cpio', ROOT / 'build/hermit-rdma-root')
        host.common()
        host.dynamic(args.qemu, '/usr/bin/qemu-system-x86_64')
        if args.guest_deployment:
            deployment = args.guest_deployment.resolve()
            args.guest_kernel = deployment / 'arch/x86/boot/bzImage'
            (stage / 'deployment-guest').mkdir()
            deployed = archive.Archive(stage / 'deployment-guest', host.generator, host.rdma_root)
            deployed.common()
            for module in sorted((deployment / 'package/lib/modules').rglob('*')):
                if module.is_file() and not module.is_symlink():
                    deployed.file(module, '/' + str(module.relative_to(deployment / 'package')), '0644')
            deployed.file(deployment / 'test-modules/guest_allocator.ko', '/guest_allocator.ko', '0644')
            deployed.file(deployment / 'test-modules/chameleon_psi_load.ko', '/tests/chameleon_psi_load.ko', '0644')
            deployed.file(deployment / 'hermit-client/rswap-client.ko', '/tests/rswap-client.ko', '0644')
            deployed.file(ROOT / 'tests/chameleon_hermit', '/tests/chameleon_hermit')
            deployed.text('init', '/init', archive.BASE_INIT + '''set -eu
modprobe rdma_rxe
modprobe rdma_ucm
modprobe ib_uverbs
insmod /guest_allocator.ko
echo HYPERALLOC_GUEST_BOOT
uname -a
echo HYPERALLOC_READY
exec sh
''')
            args.guest_initramfs = stage / 'deployment-guest.cpio.gz'
            deployed.write(args.guest_initramfs)
        host.file(args.guest_kernel, '/boot/guest-bzImage', '0644')
        guest_initramfs = args.guest_initramfs
        if args.backend == 'rdma':
            (stage / 'guest').mkdir()
            guest = archive.Archive(stage / 'guest', host.generator, host.rdma_root)
            guest.common()
            guest_initramfs = stage / 'guest-rdma.cpio.gz'
            guest.write(guest_initramfs, args.guest_initramfs)
            host.dynamic(ROOT / 'hermit/server/rswap-server', '/usr/bin/rswap-server')
        host.file(guest_initramfs, '/boot/guest-initramfs.cpio.gz', '0644')
        for module in sorted((args.host_package / 'lib/modules').rglob('*')):
            if module.is_file() and not module.is_symlink():
                host.file(module, '/' + str(module.relative_to(args.host_package)), '0644')
        for name in ('bios-256k.bin', 'kvmvapic.bin', 'linuxboot.bin', 'linuxboot_dma.bin', 'pvh.bin'):
            host.file(Path('/usr/share/qemu') / name, '/usr/share/qemu/' + name, '0644')
        init = INIT
        if args.backend == 'rdma':
            init = init.replace('/usr/bin/qemu-system-x86_64 ', '''modprobe rdma_rxe
modprobe rdma_ucm
modprobe ib_uverbs
/sbin/hermit-network-setup eth1 192.0.2.1 rxe
/usr/bin/rswap-server 192.0.2.1 9400 128 > /tmp/hermit-server.log 2>&1 &
server_pid=$!
/usr/bin/qemu-system-x86_64 ''', 1)
            init = init.replace('echo VFIO_QEMU_LOG_BEGIN', '''kill "$server_pid" 2>/dev/null
echo VFIO_HERMIT_SERVER_LOG_BEGIN
cat /tmp/hermit-server.log
echo VFIO_HERMIT_SERVER_LOG_END
echo VFIO_QEMU_LOG_BEGIN''')
        host.text('init', '/init', init)
        host.write(args.output)
    print(args.output)

if __name__ == '__main__':
    main()
