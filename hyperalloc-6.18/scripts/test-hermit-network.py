#!/usr/bin/env python3
"""Run real cross-VM RDMA read/write validation without touching L0 networking."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import socket
import subprocess

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('hermit_nested', ROOT / 'scripts/test-nested-vm.py')
nested = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nested)
vm = nested.vm


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host-kernel', type=Path, default=ROOT / 'build/host/arch/x86/boot/bzImage')
    parser.add_argument('--initramfs', type=Path, default=ROOT / 'build/hermit-network-host.cpio.gz')
    parser.add_argument('--outer-qemu', type=Path, default=Path('/usr/bin/qemu-system-x86_64'))
    parser.add_argument('--provider', choices=('rxe', 'siw'), default='rxe')
    parser.add_argument('--name', default='hermit-network-rxe')
    args = parser.parse_args()
    output = ROOT / 'results' / ('vm-' + args.name)
    output.mkdir(parents=True, exist_ok=True)
    qmp_port, serial_port = nested.reserve_ports()
    command = [str(args.outer_qemu), '-accel', 'kvm', '-cpu', 'host', '-m', '4096', '-smp', '8',
        '-nodefaults', '-display', 'none', '-serial', 'stdio', '-monitor', 'none', '-no-reboot',
        '-kernel', str(args.host_kernel), '-initrd', str(args.initramfs),
        '-append', 'console=ttyS0 rdinit=/init panic=1 nokaslr',
        '-netdev', f'user,id=outer,hostfwd=tcp:127.0.0.1:{qmp_port}-:4444,hostfwd=tcp:127.0.0.1:{serial_port}-:4445',
        '-device', 'virtio-net-pci,netdev=outer']
    (output / 'command.json').write_text(json.dumps(command, indent=2) + '\n')
    evidence = {'status': 'RUNNING', 'scope': 'Real RDMA CM + one-sided reads/writes with rping payload validation; networking prerequisite, not Hermit acceptance',
                'provider': args.provider, 'l1': '192.0.2.1/24 TAP hermit0', 'l2': '192.0.2.2/24 virtio-net eth0',
                'input_sha256': {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in
                                 (args.host_kernel, args.initramfs)}}
    process = qmp = serial = console = None
    try:
        with (output / 'outer-qemu.log').open('w') as log:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log)
            host = vm.Console(process.stdout.fileno(), process.stdin.fileno(), output / 'host-serial.log')
            host.wait(r'^HERMIT_NESTED_READY .*$', timeout=180)
            require('HERMIT_RDMA_READY interface=hermit0 address=192.0.2.1' in host.data,
                    'L1 software RDMA endpoint must initialize')
            serial = socket.create_connection(('127.0.0.1', serial_port), timeout=30)
            serial.settimeout(None)
            console = vm.Console(serial.fileno(), serial.fileno(), output / 'guest-serial.log')
            qmp = vm.QMP(('127.0.0.1', qmp_port))
            require(qmp.execute('query-kvm')['enabled'], 'L2 must run with nested KVM')
            qmp.execute('cont')
            console.wait(r'^(?:HERMIT_GUEST_READY|HYPERALLOC_READY)$', timeout=180)
            result = console.command('export PATH=/opt/hermit-rdma/bin:/usr/bin:/usr/sbin:/sbin:/bin; '
                'export LD_LIBRARY_PATH=/opt/hermit-rdma/lib; ulimit -l unlimited; dmesg -n 5; '
                f'/sbin/hermit-network-setup eth0 192.0.2.2 {args.provider}; echo HERMIT_SETUP_EXIT=$?', timeout=30)
            require('HERMIT_SETUP_EXIT=0' in result, 'L2 software RDMA endpoint must initialize')
            evidence['guest_endpoint'] = result
            result = console.command('ping -c 3 192.0.2.1; echo HERMIT_IP_EXIT=$?', timeout=15)
            require('HERMIT_IP_EXIT=0' in result, 'TAP to virtio-net IP path must be reachable')
            evidence['rdma_before'] = console.command('rdma statistic show link rdma0/1')
            evidence['transfers'] = []
            for size, count in ((4096, 32), (32768, 8)):
                cmd = f'rping -c -I 192.0.2.2 -a 192.0.2.1 -p 7471 -S {size} -C {count} -V -v'
                result = console.command(cmd + f' > /tmp/rping-{size}.log 2>&1; '
                    f'echo HERMIT_RPING_{size}_EXIT=$?; '
                    f'printf HERMIT_VALIDATED_{size}=; grep -c "^ping data:" /tmp/rping-{size}.log', timeout=90)
                require(f'HERMIT_RPING_{size}_EXIT=0' in result, f'Validated RDMA transfers of {size} bytes failed')
                require(re.search(rf'HERMIT_VALIDATED_{size}={count}(?:\r?\n|$)', result),
                        'Every requested rping iteration must finish its payload comparison')
                require(not re.search(r'validation failed|completion.*error|wc status', result, re.I),
                        'RDMA data/transport validation error')
                evidence['transfers'].append({'command': cmd, 'bytes': size, 'iterations': count, 'result': 'PASS'})
            evidence['rdma_after'] = console.command('rdma statistic show link rdma0/1')
            guest_log = console.command('dmesg')
            (output / 'guest-dmesg.log').write_text(guest_log)
            evidence['guest_kernel'] = console.command('uname -r').strip()
            evidence['chameleon_device'] = qmp.execute('query-llfree-balloon')
            require(not re.search(r'BUG:|WARNING:|Oops:|Bad page state|Kernel panic|general protection fault', guest_log),
                    'Guest kernel diagnostics must remain clean')
            qmp.execute('quit')
            host.wait(r'^HERMIT_HOST_DMESG_END$', timeout=30)
            require(process.wait(timeout=40) == 0, 'L1 QEMU must exit cleanly')
            require('HERMIT_NESTED_EXIT status=0' in host.data, 'L2 QEMU must exit cleanly')
            require(not re.search(r'BUG:|WARNING:|Oops:|Bad page state|Kernel panic|general protection fault', host.data),
                    'L1 kernel diagnostics must remain clean')
            evidence['status'] = 'PASS'
    except Exception as error:
        evidence.update(status='FAIL', error=repr(error))
        if qmp is not None:
            try:
                qmp.execute('quit')
                host.wait(r'^HERMIT_HOST_DMESG_END$', timeout=30)
                process.wait(timeout=30)
            except Exception:
                pass
        raise
    finally:
        if process is not None and process.poll() is None:
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
        (output / 'report.json').write_text(json.dumps(evidence, indent=2) + '\n')
        print(json.dumps(evidence, indent=2), flush=True)


if __name__ == '__main__':
    main()
