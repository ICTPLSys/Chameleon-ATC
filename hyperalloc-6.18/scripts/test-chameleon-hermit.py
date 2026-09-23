#!/usr/bin/env python3
"""Validate real Hermit saved bytes across Host discard and Guest faults in KVM."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import socket
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('hermit_nested', ROOT / 'scripts/test-nested-vm.py')
nested = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nested)
vm = nested.vm
PAGE, RAM = 4096, 2 << 30
BAD = r'BUG:|WARNING:|Oops:|Bad page state|Kernel panic|general protection fault'
PHASES = (['initial'] + [f'retired_order_{n}' for n in (0,2,3,4,5,6,7,8,9)] +
    ['orders_done', 'store_failed', 'retired_store_retry', 'retired_load_failure',
     'install_failed', 'load_failed', 'failures_done', 'fast_ready', 'fast_done',
     'retired_concurrent', 'retired_partial', 'retired_mprotect', 'retired_mremap_partial',
     'retired_mremap_pmd', 'retired_fork',
     'retired_exit', 'lifecycle_done', 'pool_full', 'pool_done', 'retired_policy',
     'policy_done', 'cycles_done', 'finished'])


def require(value, reason):
    if not value:
        raise AssertionError(reason)


def stats(chunk, tag, phase):
    match = re.search(r'^H_' + tag + '_BEGIN ' + phase + r'\n(.*?)^H_' + tag + '_END ' + phase + '$',
                      chunk, re.MULTILINE | re.DOTALL)
    require(match, f'{phase} must include {tag} statistics')
    return {key: int(val) if re.fullmatch(r'-?\d+', val) else val
            for key, val in re.findall(r'^(\w+) (\S+)$', match[1], re.MULTILINE)}


def run_hermit(console, qmp, output, backend, vfio=False, ram=RAM, binary='/tests/chameleon_hermit'):
    RAM = ram
    result = {}
    start = cursor = len(console.data)
    prefix = 'CHAMELEON_TEST_VFIO=1 ' if vfio else ''
    os.write(console.write_fd, (prefix + shlex.quote(binary) + '; echo CHAMELEON_HERMIT_EXIT=$?\n').encode())
    phases = list(PHASES)
    if vfio:
        phases.insert(phases.index('install_failed') + 1, 'dma_map_failed')
    try:
        for phase in phases:
            found, chunk = console.wait(r'^H_WAIT ' + phase + r'\s*$|^CHAMELEON_HERMIT_EXIT=\d+\s*$',
                                        start=cursor, timeout=120)
            require(found[0].startswith('H_WAIT '), 'Guest failed before ' + phase)
            cursor = len(console.data)
            current = qmp.execute('query-llfree-balloon')
            ch = current['chameleon']
            remote, shadow = stats(chunk, 'BACKEND', phase), stats(chunk, 'SHADOW', phase)
            result[phase] = {'qmp': current, 'backend': remote, 'shadow': shadow}
            print(f'HERMIT {backend} {phase}: retired={ch["retired-pages"]} slots={remote["live_slots"]}', flush=True)
            require(remote['backend'] == backend, 'requested real backend must be active')
            require(current['actual'] == RAM - ch['retired-pages'] * PAGE, 'actual capacity agrees with retired pages')
            require(all(r['residency-status'] == 0 for r in ch['ranges']), 'real Host mincore must succeed')
            if phase == 'initial':
                qmp.execute('chameleon-configure', {'config': {'batch-pages': 1,
                    'watermark-bytes': 0, 'ept-mode': 'deferred'}})
            elif phase.startswith('retired_') or phase == 'install_failed':
                obj = re.search(r'^H_OBJECT phase=' + phase + r' token=(\d+) pfn=(\d+) pages=(\d+) order=(\d+)$',
                                chunk, re.MULTILINE)
                require(obj, 'Guest source identity must be recorded')
                token, pfn, pages, order = map(int, obj.groups())
                active = [r for r in ch['ranges'] if r['token'] == token]
                require(len(active) == 1, 'exact saved token must exist at Host')
                r = active[0]
                require((r['gpa'], r['pages'], r['order'], r['state'], r['flags']) ==
                        (pfn * PAGE, pages, order, 5, 2), 'retired range is exact real source with DATA_SAVED authorization')
                require(not r['resident-pages'] and r['no-huge'] and ch['blocked-pages'] == pages and
                        ch['retired-pages'] == pages, 'Host backing is truly discarded before data access')
                if vfio:
                    require(r['dma-unmapped'] and not r['host-installed'],
                            'VFIO source remains DMA unmapped until Host backing is installed')
                require(remote['live_slots'] == 1 and remote['allocated_pages'] == pages,
                        'remote saved object covers every discarded source page')
                if phase == 'retired_load_failure':
                    qmp.execute('chameleon-configure', {'config': {'install-fail-count': 1}})
                if phase == 'install_failed' and vfio:
                    qmp.execute('chameleon-configure', {'config': {'dma-map-fail-count': 1}})
                if phase == 'retired_policy':
                    require(ch['policy']['lease'] and shadow['data_ready_objects'] > 0 and
                            ch['policy']['local-bytes'] == RAM - 16 * PAGE,
                            'ordinary C5 data automatically reaches real Hermit READY')
            elif phase == 'dma_map_failed':
                require(len(ch['ranges']) == 1 and ch['ranges'][0]['state'] == 5 and
                        ch['ranges'][0]['host-installed'] and ch['ranges'][0]['dma-unmapped'] and
                        ch['ranges'][0]['resident-pages'] == 16 and not ch['blocked-pages'] and
                        shadow['reservation_pages'] == 16 and remote['live_slots'] == 1,
                        'actual DMA map rollback retains Guest reservation until successful remap')
            elif phase == 'store_failed':
                require(not ch['range-records'] and not ch['retired-pages'] and remote['store_failures'] > 0,
                        'failed store never grants discard authorization')
            elif phase == 'load_failed':
                require(len(ch['ranges']) == 1 and ch['ranges'][0]['state'] == 6 and
                        current['actual'] == RAM and remote['live_slots'] == 1 and shadow['reservation_pages'] == 16,
                        'failed read retains Guest reservation and remote data after Host INSTALL')
            elif phase == 'fast_ready':
                qmp.execute('chameleon-configure', {'config': {'batch-pages': 1 << 20}})
            elif phase == 'pool_full':
                require(ch['retired-pages'] == ch['blocked-pages'] == remote['allocated_pages'] == 1024 and
                        len(ch['ranges']) == remote['live_slots'] == 2 and
                        all(r['state'] == 5 and r['flags'] == 2 and not r['resident-pages'] for r in ch['ranges']),
                        'two distinct 2 MiB remote objects fill the pool with actual discarded backing')
            if phase.endswith('_done') or phase in ('initial', 'fast_ready', 'finished'):
                require(not ch['range-records'] and not ch['retired-pages'] and not ch['blocked-pages'] and
                        not ch['registered-pages'] and current['actual'] == RAM and not remote['live_slots'] and
                        not remote['allocated_pages'] and not remote['inflight'] and not shadow['reservation_pages'],
                        'completed phase leaves no remote slot or backing reservation')
                if phase == 'fast_done':
                    require(ch['begin-batches'] == result['fast_ready']['qmp']['chameleon']['begin-batches'],
                            'early cancel completes without backing discard')
                    qmp.execute('chameleon-configure', {'config': {'batch-pages': 1}})
            os.write(console.write_fd, b'go\n')
        found, chunk = console.wait(r'^CHAMELEON_HERMIT_EXIT=(\d+)\s*$', start=start, timeout=120)
        require(int(found[1]) == 0, 'Hermit Guest executable must pass')
        passed = re.search(r'^PASS CHAMELEON_HERMIT checks=(\d+)$', chunk, re.MULTILINE)
        require(passed, 'final full data PASS marker is required')
        for label in ('orders', 'failures', 'async_cancel', 'lifecycle', 'pool', 'policy', 'repeated'):
            require(re.search(r'^PASS HERMIT ' + label + r'\b', chunk, re.MULTILINE), label + ' evidence missing')
        result['checks'] = int(passed[1])
        return result
    finally:
        (output / 'hermit.log').write_text(console.data[start:])
        (output / 'hermit-phases.json').write_text(json.dumps(result, indent=2) + '\n')


def trace_check(text, prefix, output, evidence):
    match = re.search(prefix + r'_BEGIN\n(.*?)' + prefix + '_END', text, re.DOTALL)
    require(match, 'capture actual modified Host trace')
    (output / 'host-trace.log').write_text(match[1])
    groups = {}
    for tx, phase, ranges, pages, seq in re.findall(
        r'kvm_chameleon: transaction=(\d+) phase=(\w+) ranges=(\d+) pages=(\d+) nativeflush_seq=(\d+)', match[1]):
        groups.setdefault(int(tx), []).append(dict(phase=phase, ranges=int(ranges), pages=int(pages), nativeflush_seq=int(seq)))
    expected = evidence['finished']['qmp']['chameleon']['begin-batches']
    require(len(groups) == expected and expected == 38, 'every actual discard transaction must be present in Host trace')
    for tx, rows in groups.items():
        phases = [r['phase'] for r in rows]
        require(phases[:3] == ['BEGIN_ZAP', 'FLUSH_DONE', 'REPORT'] and phases[3:] == ['INSTALL'] * rows[0]['ranges'],
                f'transaction {tx} performs actual zap/flush/report/install in order')
        require(rows[1]['nativeflush_seq'] == rows[0]['nativeflush_seq'] + 1 and
                rows[2]['nativeflush_seq'] == rows[1]['nativeflush_seq'], 'one real native flush precedes discard')
        require(rows[0]['pages'] == rows[1]['pages'] == sum(r['pages'] for r in rows[3:]),
                'Host reinstalls every discarded source page')
    return groups


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', choices=('dram', 'rdma'), required=True)
    parser.add_argument('--name')
    parser.add_argument('--initramfs', type=Path)
    parser.add_argument('--regression', action='store_true')
    args = parser.parse_args()
    args.name = args.name or 'hermit-' + args.backend
    args.initramfs = args.initramfs or ROOT / ('build/hermit-network-host.cpio.gz' if args.backend == 'rdma' else 'build/hermit-dram-host.cpio.gz')
    args.initramfs = args.initramfs.resolve()
    output = ROOT / 'results' / ('vm-' + args.name)
    output.mkdir(parents=True, exist_ok=True)
    qmp_port, serial_port = nested.reserve_ports()
    kernel = ROOT / 'build/host/arch/x86/boot/bzImage'
    command = ['/usr/bin/qemu-system-x86_64', '-accel', 'kvm', '-cpu', 'host', '-m', '4096', '-smp', '8',
        '-nodefaults', '-display', 'none', '-serial', 'stdio', '-monitor', 'none', '-no-reboot',
        '-kernel', str(kernel), '-initrd', str(args.initramfs), '-append', 'console=ttyS0 rdinit=/init panic=1 nokaslr',
        '-netdev', f'user,id=outer,hostfwd=tcp:127.0.0.1:{qmp_port}-:4444,hostfwd=tcp:127.0.0.1:{serial_port}-:4445',
        '-device', 'virtio-net-pci,netdev=outer']
    (output / 'command.json').write_text(json.dumps(command, indent=2) + '\n')
    inputs = [kernel, args.initramfs, ROOT / 'build/guest/arch/x86/boot/bzImage', ROOT / 'build/qemu/qemu-system-x86_64',
              ROOT / 'hermit/client/rswap-client.ko', ROOT / 'tests/chameleon_hermit']
    evidence = dict(status='RUNNING', backend=args.backend, scope='actual saved bytes, Host backing discard, application demand faults and native PSI restoration',
                    input_sha256={str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs})
    network = args.backend == 'rdma'
    ready = 'HERMIT_NESTED_READY' if network else 'NESTED_QEMU_READY'
    end = 'HERMIT_HOST_DMESG_END' if network else 'CHAMELEON_HOST_DMESG_END'
    trace = 'HERMIT_HOST_TRACE' if network else 'CHAMELEON_TRACE'
    process = qmp = serial = console = None
    try:
        with (output / 'outer-qemu.log').open('w') as log:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log)
            host = vm.Console(process.stdout.fileno(), process.stdin.fileno(), output / 'host-serial.log')
            host.wait('^' + ready + ' .*$', timeout=180)
            serial = socket.create_connection(('127.0.0.1', serial_port), timeout=30)
            serial.settimeout(None)
            console = vm.Console(serial.fileno(), serial.fileno(), output / 'guest-serial.log')
            qmp = vm.QMP(('127.0.0.1', qmp_port))
            require(qmp.execute('query-kvm')['enabled'], 'actual nested KVM is required')
            qmp.execute('cont')
            console.wait(r'^HYPERALLOC_READY$', timeout=180)
            console.command('dmesg -n 5; insmod /tests/chameleon_psi_load.ko')
            if network:
                console.command('export PATH=/opt/hermit-rdma/bin:/usr/bin:/usr/sbin:/sbin:/bin; '
                    'export LD_LIBRARY_PATH=/opt/hermit-rdma/lib; ulimit -l unlimited; '
                    '/sbin/hermit-network-setup eth0 192.0.2.2 rxe', timeout=30)
                evidence['rdma_before'] = console.command('rdma statistic show link rdma0/1')
            console.command(f'insmod /tests/rswap-client.ko backend={args.backend} pool_mb=4 sip=192.0.2.1 sport=9400', timeout=40)
            evidence['hermit'] = run_hermit(console, qmp, output, args.backend)
            console.command('rmmod rswap_client; test ! -d /sys/module/rswap_client; rmmod chameleon_psi_load')
            console.command(f'insmod /tests/rswap-client.ko backend={args.backend} pool_mb=4 sip=192.0.2.1 sport=9400', timeout=40)
            evidence['reloaded_backend'] = console.command('cat /sys/kernel/debug/hermit/stats')
            require('registered 1\n' in evidence['reloaded_backend'] and 'live_slots 0\n' in evidence['reloaded_backend'],
                    'a fresh module connects and registers again after complete cleanup')
            console.command('rmmod rswap_client; test ! -d /sys/module/rswap_client')
            if network:
                evidence['rdma_after'] = console.command('rdma statistic show link rdma0/1')
                for key in ('rdma_sends', 'rdma_recvs'):
                    before = re.search(key + r' (\d+)', evidence['rdma_before'])
                    after = re.search(key + r' (\d+)', evidence['rdma_after'])
                    require(before and after and int(after[1]) > int(before[1]), 'actual software RDMA completion counters increase')
            if args.regression:
                evidence['hyperalloc_regression'] = {}
                vm.run_tests(console, qmp, 'nested', 2048, evidence['hyperalloc_regression'])
            dmesg = console.command('dmesg')
            (output / 'guest-dmesg.log').write_text(dmesg)
            require(not re.search(BAD, dmesg), 'Guest kernel diagnostics must remain clean')
            qmp.execute('quit')
            host.wait('^' + end + '$', timeout=40)
            require(process.wait(timeout=40) == 0, 'outer VM exits successfully')
            require(not re.search(BAD, host.data), 'Host kernel diagnostics must remain clean')
            evidence['host_trace'] = trace_check(host.data, trace, output, evidence['hermit'])
            if network:
                require('READY Hermit RDMA' in host.data, 'remote Hermit server actually runs outside the Guest')
            evidence.update(status='PASS', kernel_diagnostics='Guest and Host clean')
    except Exception as error:
        evidence.update(status='FAIL', error=repr(error))
        # Let a kernel Oops finish writing its stack before stopping the VM.
        if console is not None and re.search(BAD, console.data):
            time.sleep(3)
        if console is not None and re.search(r'^CHAMELEON_HERMIT_EXIT=\d+$', console.data, re.MULTILINE):
            try:
                diagnostic = console.command('cat /sys/kernel/debug/hermit/stats; cat /sys/kernel/debug/chameleon_shadow/stats; '
                    'cat /sys/kernel/debug/chameleon_shadow/states; cat /sys/kernel/debug/chameleon_policy/stats; dmesg', timeout=10)
                (output / 'failure-diagnostics.log').write_text(diagnostic)
            except Exception as problem:
                evidence['diagnostic_error'] = repr(problem)
        if qmp is not None:
            try:
                evidence['failure_qmp'] = qmp.execute('query-llfree-balloon')
                qmp.execute('quit')
                host.wait('^' + end + '$', timeout=20)
                process.wait(timeout=20)
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
        print(json.dumps({'status': evidence['status'], 'backend': args.backend,
                          'error': evidence.get('error'), 'report': str(output / 'report.json')}, indent=2), flush=True)

if __name__ == '__main__':
    main()
