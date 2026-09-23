#!/usr/bin/env python3
"""Check asynchronous full-data rollback after an injected QEMU BEGIN refusal.

Boot an isolated 2 GiB KVM Guest with Hermit's DRAM backend. No pressure
generator or application access is used to complete the two rollback cases.
The final case must really discard Host backing and restore it on demand.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('rollback_vm', ROOT / 'scripts/test-vm.py')
vm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vm)
PAGE, MIB = 4096, 1 << 20
BAD = r'BUG:|WARNING:|kernel BUG|Kernel panic|Oops:|Bad page state|general protection fault'
PHASES = (
    'initial', 'rollback_ready_order_9', 'rollback_done_order_9',
    'rollback_ready_order_4', 'rollback_load_failed_order_4',
    'rollback_done_order_4', 'retired_retry', 'finished',
)
DEMAND = ('demand_faults', 'demand_fault_successes', 'demand_fault_failures',
          'demand_major_faults', 'demand_waiters', 'data_fault_restores', 'load_demand_attempts',
          'psi_fault_enter', 'psi_fault_leave', 'psi_fault_ns')
EXIT = r'^CHAMELEON_COMMIT_ROLLBACK_EXIT=(\d+)\s*$'


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def fields(text):
    return {key: int(value) if re.fullmatch(r'-?\d+', value) else value
            for key, value in re.findall(r'^(\w+) (\S+)$', text, re.M)}


def stats(text, tag, phase):
    match = re.search(r'^H_' + tag + '_BEGIN ' + re.escape(phase) +
                      r'\n(.*?)^H_' + tag + '_END ' + re.escape(phase) + r'$',
                      text, re.M | re.S)
    require(match, f'{phase}: missing {tag} statistics')
    return fields(match[1])


def source(text, phase):
    match = re.search(r'^H_OBJECT phase=' + re.escape(phase) +
                      r' token=(\d+) pfn=(\d+) pages=(\d+) order=(\d+)$', text, re.M)
    require(match, f'{phase}: missing exact source identity')
    return dict(zip(('token', 'pfn', 'pages', 'order'), map(int, match.groups())))


def empty_host(current, memory_bytes, phase):
    ch = current['chameleon']
    require(current['actual'] == memory_bytes and not ch['ranges'] and
            all(ch[key] == 0 for key in ('range-records', 'registered-pages',
                'ready-pages', 'retired-pages', 'blocked-pages', 'error-pages')),
            f'{phase}: all Host ranges and capacity charges must be released')


def empty_guest(backend, shadow, phase):
    require(all(backend[key] == 0 for key in ('live_slots', 'allocated_pages', 'inflight')),
            f'{phase}: backend objects and outstanding IO must drain')
    require(all(shadow[key] == 0 for key in ('live_objects', 'live_slots',
                'shadow_pages', 'owned_pages', 'reservation_pages',
                'host_reclaimed_pages', 'reclaimed_slots')),
            f'{phase}: all Guest Shadow ownership and reservations must drain')


def exact_range(current, obj, phase, state, resident):
    ch = current['chameleon']
    require(len(ch['ranges']) == ch['range-records'] == 1,
            f'{phase}: exactly one Host source must remain')
    entry = ch['ranges'][0]
    require((entry['token'], entry['gpa'], entry['pages'], entry['order'],
             entry['state'], entry['flags']) ==
            (obj['token'], obj['pfn'] * PAGE, obj['pages'], obj['order'], state, 2),
            f'{phase}: exact data-saved token, GPA, size, order and Host state')
    require(entry['residency-status'] == 0 and entry['resident-pages'] == resident,
            f'{phase}: real Host mincore must report {resident} resident pages')
    return entry


def run_rollback(console, qmp, output, memory_bytes, result):
    start = cursor = len(console.data)
    os.write(console.write_fd, b'/tests/chameleon_commit_rollback; '
             b'echo CHAMELEON_COMMIT_ROLLBACK_EXIT=$?\n')
    try:
        for phase in PHASES:
            found, chunk = console.wait(r'^H_WAIT ' + phase + r'\s*$|' + EXIT,
                                        start=cursor, timeout=120)
            require(found[0].startswith('H_WAIT '), f'Guest exited before {phase}')
            cursor = len(console.data)
            current = qmp.execute('query-llfree-balloon')
            backend, shadow = stats(chunk, 'BACKEND', phase), stats(chunk, 'SHADOW', phase)
            ch = current['chameleon']
            row = {'qmp': current, 'backend': backend, 'shadow': shadow}
            result[phase] = row
            require(backend['backend'] == 'dram' and backend['registered'] == 1,
                    f'{phase}: real Hermit DRAM backend must remain registered')
            require(current['actual'] == memory_bytes - ch['retired-pages'] * PAGE,
                    f'{phase}: actual capacity must agree with retired backing')
            require(ch['error-pages'] == 0 and
                    all(r['residency-status'] == 0 for r in ch['ranges']),
                    f'{phase}: no uncertain discard or failed residency inspection')
            initial = result['initial']
            base_ch = initial['qmp']['chameleon']
            base_b, base_s = initial['backend'], initial['shadow']

            if phase not in ('retired_retry', 'finished'):
                for key in ('begin-batches', 'begin-flushes', 'discarded-pages'):
                    require(ch[key] == base_ch[key],
                            f'{phase}: injected BEGIN refusal must not increment {key}')
                require(all(shadow[key] == base_s[key] for key in DEMAND),
                        f'{phase}: automatic rollback must not run a demand/PSI fault path')

            if phase == 'initial':
                empty_host(current, memory_bytes, phase)
                empty_guest(backend, shadow, phase)
                qmp.execute('chameleon-configure', {'config': {
                    'batch-pages': 1, 'watermark-bytes': 0,
                    'ept-mode': 'deferred', 'begin-fail-count': 0}})
            elif phase.startswith('rollback_ready_'):
                obj = row['source'] = source(chunk, phase)
                order = int(phase.rsplit('_', 1)[1])
                require(obj['order'] == order and obj['pages'] == 1 << order,
                        f'{phase}: test must use the requested native folio order')
                empty_host(current, memory_bytes, phase)
                require(backend['live_slots'] == backend['allocated_pages'] == 0,
                        f'{phase}: inject before save and finalization, without racing commit')
                row['injection'] = {'begin-fail-count': 1}
                qmp.execute('chameleon-configure', {'config': row['injection']})
            elif phase == 'rollback_load_failed_order_4':
                obj = row['source'] = source(chunk, phase)
                require(obj == result['rollback_ready_order_4']['source'],
                        'failed load must preserve the original exact token and source')
                entry = exact_range(current, obj, phase, state=6, resident=16)
                require(entry['transaction'] == 0 and entry['host-installed'] and
                        not ch['registered-pages'] and not ch['retired-pages'] and
                        not ch['blocked-pages'],
                        'refused BEGIN retains installed backing without a KVM transaction')
                require(backend['live_slots'] == 1 and backend['allocated_pages'] == 16 and
                        backend['load_failures'] > base_b['load_failures'],
                        'failed asynchronous load must retain the complete saved object')
                require(all(shadow[key] == 16 for key in
                        ('shadow_pages', 'owned_pages', 'reservation_pages', 'reclaimed_slots')) and
                        shadow['commit_rollback_failures'] > base_s['commit_rollback_failures'],
                        'failed background rollback must retain all pending page credits')
                require(backend['load_success'] == base_b['load_success'] + 1,
                        'failed second rollback must not publish a successful load')
            elif phase in ('rollback_done_order_9', 'rollback_done_order_4'):
                count = 1 if phase.endswith('_9') else 2
                pages = 512 if count == 1 else 528
                empty_host(current, memory_bytes, phase)
                empty_guest(backend, shadow, phase)
                require(backend['load_success'] == base_b['load_success'] + count and
                        backend['bytes_read'] == base_b['bytes_read'] + pages * PAGE,
                        f'{phase}: rollback must read every saved byte exactly once successfully')
                require(shadow['commit_rollback_success'] == base_s['commit_rollback_success'] + count and
                        shadow['data_restored_pages'] == base_s['data_restored_pages'] + pages and
                        shadow['load_background_attempts'] >= base_s['load_background_attempts'] + count,
                        f'{phase}: background worker must restore every page before the checkpoint')
            elif phase == 'retired_retry':
                obj = row['source'] = source(chunk, phase)
                require(obj['order'] == 9 and obj['pages'] == 512,
                        'successful retry must use a complete 2 MiB folio')
                entry = exact_range(current, obj, phase, state=5, resident=0)
                require(entry['transaction'] > 0 and entry['no-huge'] and
                        ch['retired-pages'] == ch['blocked-pages'] == 512,
                        'successful retry must actually retire all 512 Host backing pages')
                require(ch['begin-batches'] == base_ch['begin-batches'] + 1 and
                        ch['begin-flushes'] == base_ch['begin-flushes'] + 1 and
                        ch['discarded-pages'] == base_ch['discarded-pages'] + 512,
                        'successful retry must execute one real KVM BEGIN/flush/discard')
                require(backend['live_slots'] == 1 and backend['allocated_pages'] == 512 and
                        backend['load_success'] == base_b['load_success'] + 2 and
                        shadow['reservation_pages'] == shadow['owned_pages'] == 512,
                        'successful retirement keeps the complete remote object until demand')
                require(all(shadow[key] == base_s[key] for key in DEMAND),
                        'application must not touch the retired object before Host inspection')
            elif phase == 'finished':
                empty_host(current, memory_bytes, phase)
                empty_guest(backend, shadow, phase)
                retired = result['retired_retry']
                require(backend['load_success'] == base_b['load_success'] + 3 and
                        backend['bytes_read'] == base_b['bytes_read'] + 1040 * PAGE,
                        'two background restores and one demand restore read all exact bytes')
                require(ch['begin-batches'] == base_ch['begin-batches'] + 1 and
                        ch['discarded-pages'] == base_ch['discarded-pages'] + 512 and
                        ch['installed-pages'] == base_ch['installed-pages'] + 512,
                        'finished case must install all and only the truly retired backing')
                for key in ('load_demand_attempts', 'demand_major_faults', 'data_fault_restores',
                            'psi_fault_enter', 'psi_fault_leave'):
                    require(shadow[key] == retired['shadow'][key] + 1,
                            f'final application read must execute one complete demand path: {key}')
                require(shadow['commit_rollback_success'] == base_s['commit_rollback_success'] + 2,
                        'final demand restore must not count as a background commit rollback')

            print(f'ROLLBACK {phase}: retired={ch["retired-pages"]} '
                  f'loads={backend["load_success"]} '
                  f'background={shadow["commit_rollback_success"]}', flush=True)
            (output / 'phases.json').write_text(json.dumps(result, indent=2) + '\n')
            os.write(console.write_fd, b'go\n')

        found, chunk = console.wait(EXIT, start=start, timeout=120)
        require(int(found[1]) == 0, 'Guest rollback executable must exit successfully')
        passed = re.search(r'^PASS CHAMELEON_COMMIT_ROLLBACK checks=(\d+)$', chunk, re.M)
        require(passed, 'full data/PFN/ownership checks require the final Guest PASS marker')
        result['checks'] = int(passed[1])
    finally:
        (output / 'rollback.log').write_text(console.data[start:])
        (output / 'phases.json').write_text(json.dumps(result, indent=2) + '\n')


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--name', default='commit-rollback')
    parser.add_argument('--kernel', type=Path, default=ROOT / 'build/guest/arch/x86/boot/bzImage')
    parser.add_argument('--initramfs', type=Path, default=ROOT / 'build/guest-initramfs.cpio.gz')
    parser.add_argument('--qemu', type=Path, default=ROOT / 'build/qemu/qemu-system-x86_64')
    parser.add_argument('--memory-mib', type=int, default=2048)
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', args.name):
        parser.error('--name must be a simple result directory name (at most 64 characters)')
    if args.memory_mib < 1024:
        parser.error('--memory-mib must be at least 1024')
    paths = [args.kernel.resolve(), args.initramfs.resolve(), args.qemu.resolve()]
    for path in paths:
        if not path.is_file():
            parser.error(f'Build the missing input first: {path}')
    kernel, initrd, qemu_binary = paths
    output = ROOT / 'results' / ('vm-' + args.name)
    if output.exists():
        parser.error(f'Results already exist; choose a new --name: {output}')
    output.mkdir(parents=True)
    qmp_path = output / 'qmp.sock'
    device = {'driver': 'virtio-llfree-balloon', 'id': 'ha', 'auto-mode': False,
              'chameleon': True, 'chameleon-policy': True,
              'chameleon-batch-pages': 1, 'chameleon-watermark-bytes': 0,
              'auto-mode-iothread': 'auto',
              'iothread-vq-mapping': [{'iothread': f'install{i}'} for i in range(4)]}
    command = [str(qemu_binary), '-L', '/usr/share/qemu', '-machine', 'pc,mem-merge=off',
               '-accel', 'kvm', '-cpu', 'host', '-m', str(args.memory_mib), '-smp', '4',
               '-nodefaults', '-display', 'none', '-serial', 'stdio', '-monitor', 'none',
               '-no-reboot', '-kernel', str(kernel), '-initrd', str(initrd),
               '-append', 'console=ttyS0 rdinit=/init panic=1 nokaslr',
               '-qmp', f'unix:{qmp_path},server=on,wait=off']
    for name in ['auto'] + [f'install{i}' for i in range(4)]:
        command += ['-object', f'iothread,id={name}']
    command += ['-device', json.dumps(device)]
    evidence = {
        'status': 'RUNNING', 'backend': 'dram', 'memory_mib': args.memory_mib,
        'scope': 'QMP-injected pre-BEGIN refusal, asynchronous full-data rollback, '
                 'real KVM backing retirement and application demand restoration',
        'physical_host': subprocess.check_output(['uname', '-r'], text=True).strip(),
        'input_sha256': {str(path): sha256(path) for path in paths}, 'rollback': {},
    }
    (output / 'command.json').write_text(json.dumps(command, indent=2) + '\n')
    (output / 'report.json').write_text(json.dumps(evidence, indent=2) + '\n')
    process = qmp = console = None
    try:
        with (output / 'qemu.log').open('w') as log:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log)
            console = vm.Console(process.stdout.fileno(), process.stdin.fileno(), output / 'guest-serial.log')
            qmp = vm.QMP(str(qmp_path))
            evidence['qemu'] = qmp.greeting
            require(qmp.execute('query-kvm')['enabled'], 'actual KVM acceleration is required')
            console.wait(r'^HYPERALLOC_READY$', timeout=180)
            console.command('dmesg -n 5; test -x /tests/chameleon_commit_rollback && '
                            'insmod /tests/rswap-client.ko backend=dram pool_mb=4')
            run_rollback(console, qmp, output, args.memory_mib * MIB, evidence['rollback'])
            console.command('rmmod rswap_client && test ! -d /sys/module/rswap_client')
            dmesg = console.command('dmesg')
            (output / 'guest-dmesg.log').write_text(dmesg)
            require(not re.search(BAD, dmesg), 'Guest kernel diagnostics must remain clean')
            qmp.execute('quit')
            require(process.wait(timeout=20) == 0, 'QEMU must exit successfully')
        qlog = (output / 'qemu.log').read_text()
        refused = re.findall(r'chameleon begin-refused batch=(\d+) ranges=(\d+) status=(-?\d+)', qlog)
        require(len(refused) == 2 and all(ranges == '1' and status == '-11'
                                       for _, ranges, status in refused),
                'QEMU must report both exact injected EAGAIN refusals before KVM BEGIN')
        evidence.update(status='PASS', begin_refusals=refused,
                        kernel_diagnostics='Guest clean; physical Host trace not captured')
    except Exception as error:
        evidence.update(status='FAIL', error=repr(error))
        if qmp is not None and process.poll() is None:
            try:
                evidence['failure_qmp'] = qmp.execute('query-llfree-balloon')
            except Exception as diagnostic_error:
                evidence['qmp_diagnostic_error'] = repr(diagnostic_error)
        if console is not None and re.search(EXIT, console.data, re.M):
            try:
                diagnostics = console.command('cat /sys/kernel/debug/hermit/stats; '
                    'cat /sys/kernel/debug/chameleon_shadow/stats; '
                    'cat /sys/kernel/debug/chameleon_shadow/states; dmesg', timeout=15)
                (output / 'failure-diagnostics.log').write_text(diagnostics)
            except Exception as diagnostic_error:
                evidence['guest_diagnostic_error'] = repr(diagnostic_error)
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
        (output / 'report.json').write_text(json.dumps(evidence, indent=2) + '\n')
        print(json.dumps({'status': evidence['status'], 'error': evidence.get('error'),
                          'report': str(output / 'report.json')}, indent=2), flush=True)
    return 0 if evidence['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
