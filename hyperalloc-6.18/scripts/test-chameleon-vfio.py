#!/usr/bin/env python3
"""Validate real VFIO pins, DMA and Chameleon discard without touching L0 PCI."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import socket
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('hermit_vfio_test', ROOT / 'scripts/test-chameleon-hermit.py')
hermit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hermit)
vm, require = hermit.vm, hermit.require


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--name', default='chameleon-vfio')
    parser.add_argument('--backend', choices=('dram', 'rdma'), default='dram')
    parser.add_argument('--initramfs', type=Path, default=ROOT / 'build/chameleon-vfio-host.cpio.gz')
    args = parser.parse_args()
    output = ROOT / 'results' / ('vm-' + args.name)
    output.mkdir(parents=True, exist_ok=True)
    kernel = ROOT / 'build/deploy-host/arch/x86/boot/bzImage'
    qmp_port, serial_port = hermit.nested.reserve_ports()
    # A hub propagates receiver backpressure while the passed-through NIC is
    # being reset, causing spurious Host virtio-net watchdogs before boot.
    # Two loopback UDP endpoints provide a private wire with no L0 net changes.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as first, \
         socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as second:
        first.bind(('127.0.0.1', 0))
        second.bind(('127.0.0.1', 0))
        nic_udp, server_udp = first.getsockname()[1], second.getsockname()[1]
    command = ['/usr/bin/qemu-system-x86_64', '-machine', 'q35,accel=kvm,kernel-irqchip=split',
        '-device', 'intel-iommu,intremap=on,caching-mode=on', '-cpu', 'host', '-m', '6144', '-smp', '8',
        '-nodefaults', '-display', 'none', '-serial', 'stdio', '-monitor', 'none', '-no-reboot',
        '-kernel', str(kernel), '-initrd', str(args.initramfs),
        '-append', 'console=ttyS0 rdinit=/init panic=1 nokaslr intel_iommu=on iommu.strict=1',
        '-netdev', f'user,id=outer,hostfwd=tcp:127.0.0.1:{qmp_port}-:4444,hostfwd=tcp:127.0.0.1:{serial_port}-:4445',
        '-device', 'virtio-net-pci,netdev=outer',
        '-device', 'pcie-root-port,id=nicport,chassis=1,slot=1',
        '-netdev', (f'socket,id=nic,udp=127.0.0.1:{server_udp},localaddr=127.0.0.1:{nic_udp}'
                    if args.backend == 'rdma' else 'user,id=nic,net=198.18.0.0/24'),
        '-object', f'filter-dump,id=nicdump,netdev=nic,file={output / "nic.pcap"}',
        '-device', 'e1000e,bus=nicport,netdev=nic,mac=52:54:00:12:00:03,romfile=']
    if args.backend == 'rdma':
        command += ['-netdev', f'socket,id=server,udp=127.0.0.1:{nic_udp},localaddr=127.0.0.1:{server_udp}',
                    '-device', 'virtio-net-pci,netdev=server,mac=52:54:00:12:00:01']
    (output / 'command.json').write_text(json.dumps(command, indent=2) + '\n')
    evidence = dict(status='RUNNING', backend=args.backend,
                    scope=('Hermit SoftRoCE over real VFIO passthrough of an emulated e1000e L1 PCI NIC; no physical RNIC or L0 PCI changes'
                           if args.backend == 'rdma' else 'real VFIO passthrough of an emulated e1000e L1 PCI NIC; no physical RNIC or L0 PCI changes'),
                    input_sha256={str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in (kernel, args.initramfs)})
    process = qmp = serial = console = None
    started = time.monotonic()
    try:
        with (output / 'outer-qemu.log').open('w') as log:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log)
            host = vm.Console(process.stdout.fileno(), process.stdin.fileno(), output / 'host-serial.log')
            host.wait(r'^VFIO_NESTED_READY .*$', timeout=240)
            require('VFIO_PCI_READY pci=0000:01:00.0 group=' in host.data, 'real isolated Host PCI device must bind to VFIO')
            serial = socket.create_connection(('127.0.0.1', serial_port), timeout=30)
            serial.settimeout(None)
            console = vm.Console(serial.fileno(), serial.fileno(), output / 'guest-serial.log')
            qmp = vm.QMP(('127.0.0.1', qmp_port))
            require(qmp.execute('query-kvm')['enabled'], 'nested real KVM must be enabled')
            require(qmp.execute('query-llfree-balloon')['chameleon']['vfio-coordinated'], 'explicit DMA coordination must be enabled')
            qmp.execute('cont')
            console.wait(r'^HYPERALLOC_READY$', timeout=240)
            evidence['nic'] = console.command('cat /sys/class/net/eth0/address; readlink /sys/class/net/eth0/device/driver')
            require('52:54:00:12:00:03' in evidence['nic'] and 'e1000e' in evidence['nic'], 'Guest owns the passed-through PCI NIC')
            local_ip, peer_ip = ('192.0.2.2', '192.0.2.1') if args.backend == 'rdma' else ('198.18.0.15', '198.18.0.2')
            console.command(f'ifconfig eth0 {local_ip} netmask 255.255.255.0 up')
            console.command('for n in $(seq 1 30); do [ "$(cat /sys/class/net/eth0/carrier)" = 1 ] && break; sleep 1; done; '
                            'test "$(cat /sys/class/net/eth0/carrier)" = 1', timeout=35)
            evidence['dma_before'] = console.command(f'ping -c 4 -W 3 {peer_ip}', timeout=30)
            require('4 packets received' in evidence['dma_before'], 'real passthrough RX/TX DMA before reclaim')
            if args.backend == 'rdma':
                console.command('/sbin/hermit-network-setup eth0 192.0.2.2 rxe', timeout=30)
            console.command(f'dmesg -n 5; insmod /tests/chameleon_psi_load.ko; '
                            f'insmod /tests/rswap-client.ko backend={args.backend} pool_mb=4 sip=192.0.2.1 sport=9400', timeout=40)
            evidence['hermit'] = hermit.run_hermit(console, qmp, output, args.backend, vfio=True)
            evidence['high_ram_retires'] = []
            for phase, entry in evidence['hermit'].items():
                if isinstance(entry, dict) and phase.startswith('retired_'):
                    require(all(r['dma-unmapped'] and not r['host-installed'] for r in entry['qmp']['chameleon']['ranges']),
                            'VFIO targets remain DMA unmapped until Host backing is installed')
                    evidence['high_ram_retires'].extend(
                        dict(phase=phase, gpa=r['gpa'], pages=r['pages'])
                        for r in entry['qmp']['chameleon']['ranges'] if r['gpa'] >= 1 << 32)
            require(evidence['high_ram_retires'], 'actual reclaimed targets must cover RAM above the PCI hole')
            evidence['dma_after'] = console.command(f'ping -c 4 -W 3 {peer_ip}', timeout=30)
            require('4 packets received' in evidence['dma_after'], 'real passthrough RX/TX DMA after all reclaim cycles')
            console.command('rmmod rswap_client; rmmod chameleon_psi_load')
            evidence['guest_dmesg'] = console.command('dmesg')
            require(not re.search(hermit.BAD, evidence['guest_dmesg']), 'Guest diagnostics clean')
            try:
                qmp.execute('quit')
            except RuntimeError as error:
                # QEMU may close QMP before flushing the quit response.
                # The independent Host exit status below is authoritative.
                if str(error) != 'QMP disconnected':
                    raise
            host.wait(r'^VFIO_HOST_DMESG_END$', timeout=90)
            require(process.wait(timeout=30) == 0, 'outer VM completed')
            require('VFIO_NESTED_EXIT status=0' in host.data, 'passed-through Guest QEMU exits cleanly')
            require(not re.search(hermit.BAD, host.data), 'Host diagnostics clean')
            require(not re.search(r'NETDEV WATCHDOG|TX timeout', host.data), 'Host test network has no queue stalls')
            require('vfio_dma_map' not in host.data and 'vfio_dma_unmap() failed' not in host.data, 'VFIO maps and unmaps succeed')
            if args.backend == 'rdma':
                require('READY Hermit RDMA' in host.data and 'VFIO_HERMIT_SERVER_LOG_BEGIN' in host.data,
                        'real remote server runs outside the Guest on the passed-through NIC network')
            evidence['host_trace'] = hermit.trace_check(host.data, 'VFIO_HOST_TRACE', output, evidence['hermit'])
            evidence.update(status='PASS', kernel_diagnostics='Host and Guest clean')
    except Exception as error:
        evidence.update(status='FAIL', error=repr(error))
        if console:
            try:
                (output / 'guest-failure.log').write_text(console.command(
                    'cat /proc/net/dev; cat /proc/net/arp; cat /proc/interrupts; dmesg', timeout=10))
            except Exception:
                pass
        if qmp:
            try:
                evidence['failure_qmp'] = qmp.execute('query-llfree-balloon')
                qmp.execute('quit')
                host.wait(r'^VFIO_HOST_DMESG_END$', timeout=30)
            except Exception:
                pass
        elif process:
            try:
                host.wait(r'^VFIO_HOST_DMESG_END$', timeout=15)
            except Exception:
                pass
        raise
    finally:
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if qmp:
            qmp.file.close()
            qmp.sock.close()
        if serial:
            serial.close()
        evidence['duration_seconds'] = round(time.monotonic() - started, 3)
        (output / 'report.json').write_text(json.dumps(evidence, indent=2) + '\n')
        print(json.dumps({k: v for k, v in evidence.items() if k not in ('hermit', 'guest_dmesg', 'host_trace')}, indent=2), flush=True)

if __name__ == '__main__':
    main()
