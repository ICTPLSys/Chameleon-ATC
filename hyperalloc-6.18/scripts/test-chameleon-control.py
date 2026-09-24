#!/usr/bin/env python3
"""Validate the real C4 Guest/QEMU/Host transaction in nested KVM."""
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


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / filename)
    obj = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obj)
    return obj


nested = module('nested_tests', 'test-nested-vm.py')
vm = nested.vm
milestones = module('milestones', 'test-chameleon.py')
PAGE = 4096
RAM = 2048 * 1024 * 1024
PHASES = ('registered', 'cancelled', 'below_threshold', 'mixed_retired',
          'install_failed', 'mixed_installed', 'watermark_registered',
          'watermark_retired', 'watermark_installed', 'orders_registered',
          'orders_retired', 'orders_installed', 'partial_registered',
          'partial_result', 'exit_registered', 'exit_retired', 'exit_clean')


def require(good, message):
    if not good:
        raise RuntimeError(message)


def run_control(console, qmp, output, ram=RAM, vfio=False, binary='/tests/chameleon_control'):
    RAM = ram
    evidence, observed, last_tokens = {}, {}, set()
    groups = {}
    qmp.execute('chameleon-configure', {'config': {'batch-pages': 529, 'watermark-bytes': 0,
                                                'ept-mode': 'deferred'}})
    initial = qmp.execute('query-llfree-balloon')
    baseline = initial['chameleon']
    require(initial['actual'] == RAM and initial['chameleon']['pool-pages'] == RAM // PAGE // 200,
            'Host pool must be exactly 0.5 percent of VM RAM, independent of batch threshold')
    start = len(console.data)
    cursor = start
    os.write(console.write_fd, (shlex.quote(binary) + '; echo CHAMELEON_C4_EXIT=$?\n').encode())
    try:
        for phase in PHASES:
            found, chunk = console.wait(r'^C4_WAIT ' + phase + r'\s*$|^CHAMELEON_C4_EXIT=\d+\s*$', start=cursor, timeout=90)
            require(found.group(0).startswith('C4_WAIT '), 'C4 guest exited before checkpoint ' + phase)
            cursor = len(console.data)
            for fields in re.findall(r'^C4_OBJECT token=(\d+) order=(\d+) pfn=(\d+) addr=0x([0-9a-fA-F]+) pages=(\d+) case=\w+ pid=\d+$',
                                     console.data[start:], re.MULTILINE):
                token, order, pfn, address, pages = fields
                observed[int(token)] = {'order': int(order), 'pfn': int(pfn),
                                        'address': int(address, 16), 'pages': int(pages)}
            current = qmp.execute('query-llfree-balloon')
            ch = current['chameleon']
            ranges = {r['token']: r for r in ch['ranges']}
            require(all(r['residency-status'] == 0 for r in ranges.values()),
                    'actual mincore residency queries must succeed')
            for token, obj in observed.items():
                if token in ranges:
                    require(ranges[token]['gpa'] == obj['pfn'] * PAGE and
                            ranges[token]['pages'] == obj['pages'] and ranges[token]['order'] == obj['order'],
                            'Host range identity must match the actual guest PFN and physical folio')
            evidence[phase] = current
            if phase in ('registered', 'watermark_registered', 'orders_registered', 'partial_registered', 'exit_registered'):
                groups[phase] = set(observed) - last_tokens
                last_tokens = set(observed)
            if phase == 'registered':
                require(len(groups[phase]) == 4 and ch['registered-pages'] == 533 and
                        not ch['ready-pages'] and ch['begin-batches'] == baseline['begin-batches'], 'pending objects must not commit')
                for token in groups[phase]:
                    r = ranges[token]
                    require(r['state'] == 1 and r['resident-pages'] == r['pages'], 'REGISTER retains actual backing')
                groups['mixed'] = {t for t in groups[phase] if observed[t]['order'] != 2}
            elif phase == 'cancelled':
                require(ch['registered-pages'] == 529 and ch['begin-batches'] == baseline['begin-batches'], 'fault-restored object is removed before commit')
                cancelled = groups['registered'] - groups['mixed']
                require(all(ranges[t]['state'] == 7 for t in cancelled), 'actual restore produces Host cancellation')
            elif phase == 'below_threshold':
                require(ch['ready-pages'] == 17 and ch['batch-pages'] == 529 and ch['begin-batches'] == baseline['begin-batches'],
                        'ready pages below independent batch threshold remain resident')
            elif phase == 'mixed_retired':
                before = evidence['below_threshold']['chameleon']
                require(ch['begin-batches'] == before['begin-batches'] + 1 and
                        ch['begin-flushes'] == before['begin-flushes'] + 1, 'one real KVM batch and flush for mixed ranges')
                require(ch['remote-tlb-flush-requests'] == before['remote-tlb-flush-requests'] + 1,
                        'mixed backing discard must not cause a hidden second EPT invalidation')
                require(ch['retired-pages'] == ch['blocked-pages'] == 529 and current['actual'] == RAM - 529 * PAGE,
                        'real reclaimed capacity matches mixed-order base pages exactly once')
                require(len({ranges[t]['transaction'] for t in groups['mixed']}) == 1,
                        'one actual Host transaction owns all mixed-order members')
                for token in groups['mixed']:
                    r = ranges[token]
                    require(r['state'] == 5 and not r['resident-pages'] and r['no-huge'],
                            'every retired backing page is actually absent and protected from Host collapse')
                qmp.execute('chameleon-configure', {'config': {'install-fail-count': 1}})
            elif phase == 'install_failed':
                before = evidence['mixed_retired']['chameleon']
                require(ch['install-failures'] == before['install-failures'] + 1 and
                        ch['retired-pages'] == ch['blocked-pages'] == 529 and current['actual'] == RAM - 529 * PAGE,
                        'failed install retains retired ownership and capacity')
            elif phase == 'mixed_installed':
                require(not ch['registered-pages'] and not ch['retired-pages'] and not ch['blocked-pages'] and
                        current['actual'] == RAM and ch['installed-pages'] == baseline['installed-pages'] + 529,
                        'successful installs restore capacity once and release the pool')
                require(all(ranges[t]['no-huge'] == vfio for t in groups['mixed']), 'successful installs preserve the selected Host hugepage policy')
            elif phase == 'watermark_registered':
                require(len(groups[phase]) == 1 and ch['registered-pages'] == 8 and not ch['ready-pages'],
                        'watermark test registers a genuinely pending folio')
                qmp.execute('chameleon-configure', {'config': {'watermark-bytes': 1 << 63}})
                time.sleep(0.15)
                pending = qmp.execute('query-llfree-balloon')['chameleon']
                require(pending['begin-batches'] == ch['begin-batches'] and pending['host-available-pages'] > 0,
                        'actual Host watermark does not override data readiness')
                evidence['watermark_pending'] = pending
            elif phase == 'watermark_retired':
                before = evidence['watermark_registered']['chameleon']
                require(ch['watermark-triggers'] == before['watermark-triggers'] + 1 and
                        ch['batch-triggers'] == before['batch-triggers'] and ch['retired-pages'] == 8,
                        'Host available-memory watermark independently triggers a below-threshold batch')
                token = next(iter(groups['watermark_registered']))
                require(not ranges[token]['resident-pages'] and ranges[token]['no-huge'], 'watermark path actually discards backing')
            elif phase == 'watermark_installed':
                require(not ch['retired-pages'] and not ch['registered-pages'] and current['actual'] == RAM,
                        'watermark round-trip control releases its reservation')
                qmp.execute('chameleon-configure', {'config': {'watermark-bytes': 0}})
            elif phase == 'orders_registered':
                members = [ranges[t] for t in groups[phase]]
                require(len(members) == 5 and sorted(r['order'] for r in members) == [2, 5, 6, 7, 8] and
                        ch['registered-pages'] == 484 and not ch['ready-pages'] and
                        all(r['resident-pages'] == r['pages'] for r in members),
                        'native 16/128/256/512/1024 KiB folios register as five resident ranges')
                qmp.execute('chameleon-configure', {'config': {'batch-pages': 484}})
            elif phase == 'orders_retired':
                members = [ranges[t] for t in groups['orders_registered']]
                require(ch['retired-pages'] == ch['blocked-pages'] == 484 and
                        current['actual'] == RAM - 484 * PAGE and
                        len({r['transaction'] for r in members}) == 1 and
                        all(r['state'] == 5 and not r['resident-pages'] and r['no-huge'] for r in members),
                        'all remaining native mTHP sizes have actually missing, protected backing')
            elif phase == 'orders_installed':
                require(not ch['retired-pages'] and not ch['blocked-pages'] and not ch['registered-pages'] and
                        current['actual'] == RAM and
                        all(ranges[t]['no-huge'] == vfio for t in groups['orders_registered']),
                        'all native mTHP ranges reinstall, restore Host hugepage policy, and release reservations')
            elif phase == 'partial_registered':
                require(len(groups[phase]) == 2 and ch['registered-pages'] == 12 and not ch['ready-pages'],
                        'partial completion starts with independent pending ranges')
                qmp.execute('chameleon-configure', {'config': {'batch-pages': 12, 'discard-fail-index': 1}})
            elif phase == 'partial_result':
                result = [ranges[t] for t in groups['partial_registered']]
                require(sorted(r['state'] for r in result) == [5, 6], 'partial discard preserves honest per-range outcomes')
                retired = next(r for r in result if r['state'] == 5)
                untouched = next(r for r in result if r['state'] == 6)
                require(not retired['resident-pages'] and retired['no-huge'] and
                        untouched['resident-pages'] == untouched['pages'] and not untouched['no-huge'],
                        'skipped discard retains its backing while the successful range is absent')
                require(current['actual'] == RAM - retired['pages'] * PAGE and ch['blocked-pages'] == retired['pages'],
                        'partial completion accounts only actually discarded pages')
            elif phase == 'exit_registered':
                require(len(groups[phase]) == 1 and ch['registered-pages'] == 16 and not ch['retired-pages'],
                        'child-owned exit fixture begins without prior reservations')
                qmp.execute('chameleon-configure', {'config': {'batch-pages': 16}})
            elif phase == 'exit_retired':
                token = next(iter(groups['exit_registered']))
                require(ch['retired-pages'] == 16 and not ranges[token]['resident-pages'], 'child source is genuinely retired before exit')
                qmp.execute('chameleon-configure', {'config': {'install-fail-count': 1}})
            elif phase == 'exit_clean':
                require(not ch['retired-pages'] and not ch['blocked-pages'] and not ch['registered-pages'] and
                        not ch['error-pages'] and current['actual'] == RAM, 'owner exit retries install and releases every reservation')
            os.write(console.write_fd, b'go\n')
        match, text = console.wait(r'^CHAMELEON_C4_EXIT=(\d+)\s*$', start=start, timeout=90)
        require(int(match.group(1)) == 0, 'C4 guest test process failed')
        passed = re.search(r'^PASS CHAMELEON_C4 checks=(\d+)$', text, re.MULTILINE)
        require(passed is not None, 'C4 final guest PASS marker missing')
        evidence['checks'] = int(passed.group(1))
        return evidence
    finally:
        (output / 'c4.log').write_text(console.data[start:])
        (output / 'c4-phases.json').write_text(json.dumps(evidence, indent=2) + '\n')


def check_trace(text, output, evidence):
    match = re.search(r'CHAMELEON_TRACE_BEGIN\n(.*?)CHAMELEON_TRACE_END', text, re.DOTALL)
    require(match is not None, 'actual L1 trace capture missing')
    (output / 'host-trace.log').write_text(match.group(1))
    events = re.findall(r'kvm_chameleon: transaction=(\d+) phase=(\w+) ranges=(\d+) pages=(\d+) nativeflush_seq=(\d+)', match.group(1))
    groups = {}
    for token, phase, nr, pages, seq in events:
        groups.setdefault(token, []).append({'phase': phase, 'ranges': int(nr), 'pages': int(pages), 'nativeflush_seq': int(seq)})
    require(len(groups) == 5, 'five actual integrated Host transactions must be captured')
    expected = {}
    for phase, pages, nr in (('mixed_retired', 529, 3), ('watermark_retired', 8, 1), ('orders_retired', 484, 5),
                             ('partial_result', 12, 2), ('exit_retired', 16, 1)):
        active = [r for r in evidence[phase]['chameleon']['ranges'] if r['state'] == 5]
        tokens = {str(r['transaction']) for r in active}
        require(len(tokens) == 1, phase + ': one current retired transaction')
        token = tokens.pop()
        expected[token] = (pages, nr, sum(r['pages'] for r in active), len(active))
        for r in active:
            require(re.search(r'chameleon retired token=' + str(r['token']) + r' pages=' +
                              str(r['pages']) + r' mincore_resident=0 rc=0\b', text),
                    'QEMU confirms real successful discard for token ' + str(r['token']))
    require(set(groups) == set(expected), 'Host trace transactions must match actual QMP transaction identities')
    for token, sequence in groups.items():
        phases = [e['phase'] for e in sequence]
        pages, nr, retired_pages, retired_nr = expected[token]
        require(phases[:3] == ['BEGIN_ZAP', 'FLUSH_DONE', 'REPORT'] and
                phases[3:] == ['INSTALL'] * retired_nr,
                f'transaction {token}: actual zap/flush/report/install order')
        require(all(e['ranges'] == nr and e['pages'] == pages for e in sequence[:2]) and
                sequence[2]['pages'] == retired_pages and
                sum(e['pages'] for e in sequence[3:]) == retired_pages,
                f'transaction {token}: exact target and installed page counts')
        require(sequence[1]['nativeflush_seq'] == sequence[0]['nativeflush_seq'] + 1 and
                sequence[2]['nativeflush_seq'] == sequence[1]['nativeflush_seq'],
                f'transaction {token}: exactly one actual remote flush before discard completion')
    return groups


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--name', default='chameleon-c4-integrated')
    parser.add_argument('--initramfs', type=Path, default=ROOT / 'build/chameleon-host-integrated.cpio.gz')
    parser.add_argument('--regression', action='store_true')
    args = parser.parse_args()
    output = ROOT / 'results' / ('vm-' + args.name)
    output.mkdir(parents=True, exist_ok=True)
    qmp_port, serial_port = nested.reserve_ports()
    kernel = ROOT / 'build/host/arch/x86/boot/bzImage'
    command = ['/usr/bin/qemu-system-x86_64', '-accel', 'kvm', '-cpu', 'host', '-m', '4096', '-smp', '8',
               '-nodefaults', '-display', 'none', '-serial', 'stdio', '-monitor', 'none', '-no-reboot',
               '-kernel', str(kernel), '-initrd', str(args.initramfs),
               '-append', 'console=ttyS0 rdinit=/init panic=1 nokaslr',
               '-netdev', f'user,id=net0,hostfwd=tcp:127.0.0.1:{qmp_port}-:4444,hostfwd=tcp:127.0.0.1:{serial_port}-:4445',
               '-device', 'virtio-net-pci,netdev=net0']
    (output / 'command.json').write_text(json.dumps(command, indent=2) + '\n')
    inputs = [kernel, args.initramfs, ROOT / 'build/guest/arch/x86/boot/bzImage',
              ROOT / 'build/qemu/qemu-system-x86_64', ROOT / 'tests/chameleon_control.c', ROOT / 'tests/chameleon_control',
              ROOT / 'linux/mm/chameleon_shadow.c', ROOT / 'linux/arch/x86/kvm/chameleon.c',
              ROOT / 'linux/drivers/virtio/virtio_llfree_balloon.c', ROOT / 'qemu/hw/virtio/virtio-llfree-chameleon.c.inc']
    evidence = {'status': 'RUNNING', 'scope': 'actual L1 KVM/QEMU and L2 Linux 6.18; explicit discard-only source pages',
                'input_sha256': {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs},
                'source_head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT.parent, text=True).strip(),
                'physical_host': subprocess.check_output(['uname', '-r'], text=True).strip()}
    process = qmp = serial = console = None
    try:
        with (output / 'outer-qemu.log').open('w') as log:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log)
            host = vm.Console(process.stdout.fileno(), process.stdin.fileno(), output / 'host-serial.log')
            host.wait(r'^NESTED_QEMU_READY .*$', timeout=180)
            require('NESTED_HOST_KERNEL 6.18.0-hyperalloc-host' in host.data, 'modified L1 Host is required')
            require('CHAMELEON_TRACE_READY' in host.data, 'L1 trace must be enabled before transactions')
            serial = socket.create_connection(('127.0.0.1', serial_port), timeout=30)
            serial.settimeout(None)
            console = vm.Console(serial.fileno(), serial.fileno(), output / 'serial.log')
            qmp = vm.QMP(('127.0.0.1', qmp_port))
            evidence['nested_kvm'] = qmp.execute('query-kvm')
            require(evidence['nested_kvm']['enabled'], 'L2 must use real KVM')
            qmp.execute('cont')
            console.wait(r'^HYPERALLOC_READY$', timeout=180)
            console.command('dmesg -n 5')
            evidence['c3_regression'] = milestones.run_milestone(console, output, 'c3')
            evidence['c4'] = run_control(console, qmp, output)
            if args.regression:
                evidence['hyperalloc_regression'] = {}
                vm.run_tests(console, qmp, 'nested', 2048, evidence['hyperalloc_regression'])
            dmesg = console.command('dmesg')
            (output / 'guest-dmesg.log').write_text(dmesg)
            require(not re.search(r'BUG:|WARNING:|kernel BUG|Kernel panic|Oops:|Bad page state|general protection fault', dmesg),
                    'guest kernel diagnostics must be clean')
            try:
                qmp.execute('quit')
            except RuntimeError as error:
                if str(error) != 'QMP disconnected':
                    raise
            require(process.wait(timeout=40) == 0, 'outer QEMU must exit cleanly')
            host.wait(r'^NESTED_QEMU_EXIT status=0$', timeout=10)
            host.wait(r'^CHAMELEON_HOST_DMESG_END$', timeout=10)
            evidence['host_trace'] = check_trace(host.data, output, evidence['c4'])
            require(not re.search(r'BUG:|WARNING:|kernel BUG|Kernel panic|Oops:|Bad page state|general protection fault', host.data),
                    'Host kernel diagnostics must be clean')
            for label in ('finalize-ack', 'begin-complete', 'discard', 'retired', 'installed'):
                require('chameleon ' + label in host.data, f'QEMU cross-layer {label} evidence missing')
            evidence.update(status='PASS', kernel_diagnostics='guest and Host clean')
    except Exception as error:
        evidence.update(status='FAIL', error=repr(error))
        if console is not None and re.search(r'^CHAMELEON_C4_EXIT=\d+$', console.data, re.MULTILINE):
            try:
                diagnostics = console.command('cat /sys/kernel/debug/chameleon_mm/stats; '
                    'cat /sys/kernel/debug/chameleon_shadow/stats; '
                    'cat /sys/kernel/debug/chameleon_shadow/states; dmesg', timeout=10)
                (output / 'failure-diagnostics.log').write_text(diagnostics)
            except Exception as diagnostic_error:
                evidence['guest_diagnostic_error'] = repr(diagnostic_error)
        if qmp is not None:
            try:
                evidence['failure_qmp'] = qmp.execute('query-llfree-balloon')
            except Exception as diagnostic_error:
                evidence['failure_qmp_error'] = repr(diagnostic_error)
            try:
                qmp.execute('quit')
            except Exception as diagnostic_error:
                evidence['quit_diagnostic'] = repr(diagnostic_error)
            try:
                process.wait(timeout=30)
                host.wait(r'^CHAMELEON_HOST_DMESG_END$', timeout=5)
            except Exception as diagnostic_error:
                evidence['host_diagnostic_error'] = repr(diagnostic_error)
        raise
    finally:
        if process and process.poll() is None:
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
