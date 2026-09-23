#!/usr/bin/env python3
"""Run C5 native PSI control and C6 EPT comparisons through real nested KVM."""
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
spec = importlib.util.spec_from_file_location('control', ROOT / 'scripts/test-chameleon-control.py')
control = importlib.util.module_from_spec(spec)
spec.loader.exec_module(control)
vm, nested, require = control.vm, control.nested, control.require
PAGE, RAM = 4096, 2 << 30
PHASES = (['off'] + [f'free_{metric}_{state}' for metric in ('some', 'full')
          for state in ('low', 'high', 'again', 'off')] +
          ['shadow_low', 'shadow_high', 'shadow_off'] +
          [f'{mode}_{state}' for mode in ('deferred', 'immediate')
           for state in ('ready', 'retired', 'restored', 'off')] +
          ['exit_low', 'exit_off', 'disable_fail_ready', 'disable_fail_retired',
           'disable_failed', 'disable_retried', 'cycles_ready', 'cycles_done'])


def busy_resize(qmp, ram=RAM):
    try:
        qmp.execute('llfree-balloon', {'value': ram})
    except RuntimeError as error:
        require('lease' in str(error).lower(), 'external resize must fail because the policy owns its lease')
        return str(error)
    raise RuntimeError('external resize unexpectedly bypassed the active policy lease')


def run_policy(console, qmp, output, expect_auto, ram=RAM, binary='/tests/chameleon_policy'):
    RAM = ram
    evidence = {}
    start = cursor = len(console.data)
    os.write(console.write_fd, (shlex.quote(binary) + '; echo CHAMELEON_C5_EXIT=$?\n').encode())
    try:
        for phase in PHASES:
            found, chunk = console.wait(r'^C5_WAIT ' + phase + r'\s*$|^CHAMELEON_C5_EXIT=\d+\s*$',
                                        start=cursor, timeout=90)
            require(found.group(0).startswith('C5_WAIT '), 'Guest exited before C5 checkpoint ' + phase)
            cursor = len(console.data)
            current = qmp.execute('query-llfree-balloon')
            ch, policy = current['chameleon'], current['chameleon']['policy']
            match = re.search(r'^C5_STATS_BEGIN ' + phase + r'\n(.*?)^C5_STATS_END ' + phase + r'$',
                              chunk, re.MULTILINE | re.DOTALL)
            require(match is not None, 'Guest policy statistics must accompany every checkpoint')
            guest = {k: int(v) for k, v in re.findall(r'^(\w+) (-?\d+)$', match.group(1), re.MULTILINE)}
            evidence[phase] = {'qmp': current, 'guest': guest}
            require(policy['local-bytes'] == RAM - policy['hard-reclaimed-bytes'] - policy['retired-bytes'] and
                    current['actual'] == policy['local-bytes'], 'actual and capacity snapshot agree exactly')
            require(all(r['residency-status'] == 0 for r in ch['ranges']), 'real mincore queries must succeed')
            if phase == 'off':
                require(not guest['enabled'] and not policy['lease'] and current['actual'] == RAM,
                        'policy and lease must be off by default')
            elif phase.startswith('free_'):
                if phase.endswith(('_low', '_again')):
                    require(policy['lease'] and policy['reclaim-allowed'] and
                            policy['hard-reclaimed-bytes'] == 64 << 20 and current['actual'] == RAM - (64 << 20),
                            'native low PSI reaches its exact configured free-capacity floor')
                    evidence[phase]['external_resize_rejection'] = busy_resize(qmp, RAM)
                    if phase == 'free_some_low' and expect_auto:
                        before = policy['auto-skips']
                        deadline = time.monotonic() + 7
                        while time.monotonic() < deadline:
                            latest = qmp.execute('query-llfree-balloon')
                            if latest['chameleon']['policy']['auto-skips'] > before:
                                evidence[phase]['auto_paused'] = latest
                                break
                            time.sleep(0.1)
                        require('auto_paused' in evidence[phase], 'a real legacy auto timer must skip while policy holds the lease')
                elif phase.endswith('_high'):
                    require(policy['lease'] and not policy['reclaim-allowed'] and current['actual'] == RAM and
                            not policy['hard-reclaimed-bytes'] and guest['high_epochs'] > 0,
                            'native high PSI actively returns capacity and closes reclaim gate')
                    evidence[phase]['external_resize_rejection'] = busy_resize(qmp, RAM)
                else:
                    require(not policy['lease'] and not guest['enabled'] and current['actual'] == RAM,
                            'disable finishes capacity return and hands back ownership')
                    qmp.execute('llfree-balloon', {'value': RAM})
            elif phase.startswith('shadow_') or phase == 'exit_low':
                require(not ch['retired-pages'] and not ch['registered-pages'] and current['actual'] == RAM,
                        'ordinary cold Shadow cannot discard application data without backing')
                if phase == 'shadow_high':
                    require(not policy['reclaim-allowed'] and guest['shadow_restored_pages'] >= 16,
                            'high pressure restores actual resident Shadow pages')
            elif phase.endswith('_ready'):
                mode = 'immediate' if phase == 'immediate_ready' else 'deferred'
                batch = 32 if phase in ('deferred_ready', 'immediate_ready') else 4
                require(not policy['lease'], 'mode changes occur only between controller owners')
                qmp.execute('chameleon-configure', {'config': {'ept-mode': mode,
                    'batch-pages': batch, 'watermark-bytes': 0}})
            elif phase in ('deferred_retired', 'immediate_retired'):
                mode = phase.split('_')[0]
                before = evidence[mode + '_ready']['qmp']['chameleon']
                delta = 1 if mode == 'deferred' else 2
                active = [r for r in ch['ranges'] if r['state'] == 5]
                require(len(active) == 2 and ch['retired-pages'] == ch['blocked-pages'] == 32 and
                        current['actual'] == RAM - 32 * PAGE and ch['ept-mode'] == mode,
                        'both actual source folios are retired in the selected EPT mode')
                require(ch['begin-batches'] == before['begin-batches'] + delta and
                        ch['begin-flushes'] == before['begin-flushes'] + delta and
                        len({r['transaction'] for r in active}) == delta,
                        'deferred uses one real transaction; immediate uses one per folio')
                objects = {int(t): (int(p), int(n), int(o)) for t, p, n, o in re.findall(
                    r'^C5_OBJECT mode=' + mode + r' token=(\d+) pfn=(\d+) pages=(\d+) order=(\d+)$',
                    console.data[start:], re.MULTILINE)}
                require(len(objects) == 2, 'actual Guest source identities are recorded')
                for r in active:
                    require(objects[r['token']] == (r['gpa'] // PAGE, r['pages'], r['order']) and
                            not r['resident-pages'] and r['no-huge'],
                            'Host range exactly matches real Guest PFN and has no remaining backing')
                qmp.execute('chameleon-configure', {'config': {'install-fail-count': 1}})
            elif phase in ('deferred_restored', 'immediate_restored'):
                require(policy['lease'] and not policy['reclaim-allowed'] and current['actual'] == RAM and
                        not ch['blocked-pages'] and not ch['retired-pages'],
                        'automatic high-pressure install really restores capacity after retry')
            elif phase == 'disable_fail_retired':
                require(ch['retired-pages'] == ch['blocked-pages'] == 4 and policy['lease'],
                        'disable failure fixture has actually missing backing')
                qmp.execute('chameleon-configure', {'config': {'install-fail-count': 1}})
            elif phase == 'disable_failed':
                require(not guest['enabled'] and policy['lease'] and not policy['reclaim-allowed'] and
                        ch['retired-pages'] == ch['blocked-pages'] == 4 and current['actual'] == RAM - 4 * PAGE,
                        'failed disable retains ownership and uninstalled backing')
                evidence[phase]['external_resize_rejection'] = busy_resize(qmp, RAM)
            elif phase.endswith('_off') or phase in ('disable_retried', 'cycles_done'):
                require(not policy['lease'] and not guest['enabled'] and current['actual'] == RAM and
                        not ch['retired-pages'] and not ch['blocked-pages'] and not ch['registered-pages'] and
                        ch['range-records'] == 0,
                        'all policy-owned capacity, range records and reservations must be released')
                if phase == 'cycles_done':
                    before = evidence['cycles_ready']['qmp']['chameleon']
                    require(ch['begin-batches'] == before['begin-batches'] + 32 and
                            ch['forgotten-tokens'] == before['forgotten-tokens'] + 32,
                            '32 actual discard/install rounds each forget their completed token')
            os.write(console.write_fd, b'go\n')
        match, chunk = console.wait(r'^CHAMELEON_C5_EXIT=(\d+)\s*$', start=start, timeout=90)
        require(int(match.group(1)) == 0, 'C5 Guest executable failed')
        passed = re.search(r'^PASS CHAMELEON_C5 checks=(\d+)$', chunk, re.MULTILINE)
        require(passed is not None, 'C5 final PASS evidence missing')
        for label in ('pressure', 'shadow', 'discard', 'lifecycle', 'disable_failure', 'repeated'):
            require(re.search(r'^PASS C5 ' + label + r'\b', chunk, re.MULTILINE), 'C5 ' + label + ' evidence missing')
        evidence['checks'] = int(passed.group(1))
        return evidence
    finally:
        (output / 'c5.log').write_text(console.data[start:])
        (output / 'c5-phases.json').write_text(json.dumps(evidence, indent=2) + '\n')


def check_trace(text, output, evidence):
    match = re.search(r'CHAMELEON_TRACE_BEGIN\n(.*?)CHAMELEON_TRACE_END', text, re.DOTALL)
    require(match is not None, 'actual Host trace must be captured')
    (output / 'host-trace.log').write_text(match.group(1))
    groups = {}
    for tx, phase, ranges, pages, seq in re.findall(
        r'kvm_chameleon: transaction=(\d+) phase=(\w+) ranges=(\d+) pages=(\d+) nativeflush_seq=(\d+)', match.group(1)):
        groups.setdefault(int(tx), []).append({'phase': phase, 'ranges': int(ranges),
                                              'pages': int(pages), 'nativeflush_seq': int(seq)})
    require(len(groups) == 36, 'one deferred, two immediate, one failed-disable and 32 lifecycle transactions')
    for tx, records in groups.items():
        phases = [e['phase'] for e in records]
        require(phases[:3] == ['BEGIN_ZAP', 'FLUSH_DONE', 'REPORT'] and
                phases[3:] == ['INSTALL'] * records[0]['ranges'],
                f'Host transaction {tx} must really zap/flush/discard-report/install')
        require(records[1]['nativeflush_seq'] == records[0]['nativeflush_seq'] + 1 and
                records[2]['nativeflush_seq'] == records[1]['nativeflush_seq'],
                f'Host transaction {tx} performs exactly one native remote flush call before discard')
        require(records[0]['pages'] == records[1]['pages'] and
                records[0]['pages'] == sum(e['pages'] for e in records[3:]),
                f'Host transaction {tx} reinstalls exactly its discarded source pages')
    for mode, size in (('deferred', 2), ('immediate', 1)):
        active = [r for r in evidence[mode + '_retired']['qmp']['chameleon']['ranges'] if r['state'] == 5]
        for r in active:
            require(groups[r['transaction']][0]['ranges'] == size,
                    'actual Host trace must agree with the QMP EPT mode comparison')
    return groups



class HostDebugfsQMP:
    """Drive actual EPT transactions using Host files, retaining QMP fault controls."""
    def __init__(self, qmp, host, evidence):
        self.qmp, self.host, self.evidence = qmp, host, evidence
        text = host.command('for d in /sys/kernel/debug/kvm/*/chameleon; do echo TUNING_DIR=$d; done')
        paths = re.findall(r'^TUNING_DIR=(/sys/kernel/debug/kvm/[^ ]+/chameleon)$', text, re.MULTILINE)
        require(len(paths) == 1, 'one actual L2 Host tuning directory')
        self.directory = paths[0]
        self.file, self.sock = qmp.file, qmp.sock
        evidence['host_debugfs_directory'] = self.directory
        self.fields = {'ept-mode':'ept_mode', 'batch-pages':'batch_pages', 'watermark-bytes':'watermark_bytes'}
        # QMP -> the same files, followed by files -> actual QMP state.
        qmp.execute('chameleon-configure', {'config':{'ept-mode':'immediate','batch-pages':1024,'watermark-bytes':4096}})
        for key,want in [('ept-mode','immediate'),('batch-pages','1024'),('watermark-bytes','4096')]:
            got = host.command('cat '+shlex.quote(self.directory+'/'+self.fields[key]))
            require(re.search(r'^'+re.escape(want)+r'$',got,re.MULTILINE) is not None, 'QMP update visible through Host file '+key)
        self.execute('chameleon-configure', {'config':{'ept-mode':'deferred','batch-pages':512,'watermark-bytes':0}})

    def __getattr__(self, name):
        return getattr(self.qmp, name)

    def execute(self, name, args=None):
        if name != 'chameleon-configure' or not any(k in self.fields for k in args['config']):
            return self.qmp.execute(name,args) if args is not None else self.qmp.execute(name)
        config = args['config']
        for key,value in config.items():
            if key in self.fields:
                self.host.command('echo '+shlex.quote(str(value))+' > '+shlex.quote(self.directory+'/'+self.fields[key]))
        remaining = {k:v for k,v in config.items() if k not in self.fields}
        if remaining:
            self.qmp.execute(name, {'config':remaining})
        state = self.qmp.execute('query-llfree-balloon')['chameleon']
        require(all(state[k] == v for k,v in config.items() if k in self.fields), 'Host scalar updates reach QEMU decisions')
        self.evidence.setdefault('host_debugfs_updates',[]).append(config)
        return {}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--name', default='chameleon-c5-c6')
    parser.add_argument('--initramfs', type=Path, default=ROOT / 'build/chameleon-policy-host.cpio.gz')
    parser.add_argument('--regression', action='store_true')
    parser.add_argument('--expect-auto', action='store_true')
    parser.add_argument('--tuning', action='store_true', help='Test live Guest scalars and drive EPT comparisons via Host debugfs (requires --host-shell initramfs)')
    args = parser.parse_args()
    args.initramfs = args.initramfs.resolve()
    output = ROOT / 'results' / ('vm-' + args.name)
    output.mkdir(parents=True, exist_ok=True)
    qmp_port, serial_port = nested.reserve_ports()
    kernel = ROOT / 'build/host/arch/x86/boot/bzImage'
    command = ['/usr/bin/qemu-system-x86_64', '-accel', 'kvm', '-cpu', 'host', '-m', '4096', '-smp', '8',
        '-nodefaults', '-display', 'none', '-serial', 'stdio', '-monitor', 'none', '-no-reboot',
        '-kernel', str(kernel), '-initrd', str(args.initramfs), '-append', 'console=ttyS0 rdinit=/init panic=1 nokaslr',
        '-netdev', f'user,id=net0,hostfwd=tcp:127.0.0.1:{qmp_port}-:4444,hostfwd=tcp:127.0.0.1:{serial_port}-:4445',
        '-device', 'virtio-net-pci,netdev=net0']
    (output / 'command.json').write_text(json.dumps(command, indent=2) + '\n')
    inputs = [kernel, args.initramfs, ROOT / 'build/guest/arch/x86/boot/bzImage', ROOT / 'build/qemu/qemu-system-x86_64']
    inputs += [ROOT / path for path in ('tests/chameleon_policy.c', 'tests/chameleon_policy', 'tests/chameleon_psi_load.ko',
        'tests/chameleon_psi_load.c', 'linux/kernel/sched/psi.c', 'linux/mm/chameleon_policy.c',
        'linux/mm/chameleon_shadow.c', 'linux/mm/chameleon.c', 'linux/mm/chameleon_mm.c',
        'linux/drivers/virtio/virtio_llfree_balloon.c', 'qemu/hw/virtio/virtio-llfree-chameleon.c.inc')]
    if args.tuning:
        inputs += [ROOT/'tests/chameleon_tuning', ROOT/'tests/chameleon_tuning.c', ROOT/'tests/kvm-chameleon-tuning.c']
    evidence = {'status': 'RUNNING', 'scope': 'native PSI accounting, actual HyperAlloc capacity and C4 backing transactions; Hermit deferred',
        'expect_auto': args.expect_auto,
        'source_head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT.parent, text=True).strip(),
        'input_sha256': {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs}}
    process = qmp = serial = console = None
    try:
        with (output / 'outer-qemu.log').open('w') as log:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log)
            host = vm.Console(process.stdout.fileno(), process.stdin.fileno(), output / 'host-serial.log')
            host.wait(r'^NESTED_QEMU_READY .*$', timeout=180)
            require('NESTED_HOST_KERNEL 6.18.0-hyperalloc-host' in host.data and 'CHAMELEON_TRACE_READY' in host.data,
                    'actual modified Host and trace must be active')
            serial = socket.create_connection(('127.0.0.1', serial_port), timeout=30)
            serial.settimeout(None)
            console = vm.Console(serial.fileno(), serial.fileno(), output / 'serial.log')
            qmp = vm.QMP(('127.0.0.1', qmp_port))
            require(qmp.execute('query-kvm')['enabled'], 'L2 must execute under actual KVM')
            if args.tuning:
                host.wait(r'^NESTED_TUNING_SHELL_READY$', timeout=30)
                qmp = HostDebugfsQMP(qmp, host, evidence)
            qmp.execute('cont')
            console.wait(r'^HYPERALLOC_READY$', timeout=180)
            if args.tuning:
                text = console.command('/tests/chameleon_tuning --policy-only', timeout=180)
                (output/'tuning-policy.log').write_text(text)
                match = re.search(r'PASS CHAMELEON_TUNING checks=(\d+)',text)
                require(match is not None, 'Guest live parameter test passes')
                evidence['guest_tuning_checks'] = int(match.group(1))
            console.command('dmesg -n 5; insmod /tests/chameleon_psi_load.ko')
            evidence['c5'] = run_policy(console, qmp, output, args.expect_auto)
            console.command('rmmod chameleon_psi_load')
            if args.regression:
                evidence['hyperalloc_regression'] = {}
                vm.run_tests(console, qmp, 'nested', 2048, evidence['hyperalloc_regression'])
            dmesg = console.command('dmesg')
            (output / 'guest-dmesg.log').write_text(dmesg)
            require(not re.search(r'BUG:|WARNING:|Oops:|Bad page state|Kernel panic|general protection fault', dmesg),
                    'Guest kernel diagnostics must be clean')
            qmp.execute('quit')
            if args.tuning: os.write(host.write_fd, b'exit\n')
            require(process.wait(timeout=40) == 0, 'outer QEMU must exit successfully')
            host.wait(r'^NESTED_QEMU_EXIT status=0$', timeout=10)
            host.wait(r'^CHAMELEON_HOST_DMESG_END$', timeout=10)
            evidence['host_trace'] = check_trace(host.data, output, evidence['c5'])
            require(not re.search(r'BUG:|WARNING:|Oops:|Bad page state|Kernel panic|general protection fault', host.data),
                    'Host kernel diagnostics must be clean')
            evidence.update(status='PASS', kernel_diagnostics='Guest and Host clean')
    except Exception as error:
        evidence.update(status='FAIL', error=repr(error))
        if console is not None and re.search(r'^CHAMELEON_C5_EXIT=\d+$', console.data, re.MULTILINE):
            try:
                text = console.command('cat /sys/kernel/debug/chameleon_policy/stats; '
                    'cat /sys/kernel/debug/chameleon_shadow/stats; cat /sys/kernel/debug/chameleon_shadow/states; '
                    'cat /sys/kernel/debug/chameleon_mm/stats; dmesg', timeout=10)
                (output / 'failure-diagnostics.log').write_text(text)
            except Exception as diagnostic_error:
                evidence['guest_diagnostic_error'] = repr(diagnostic_error)
        if qmp is not None:
            try:
                evidence['failure_qmp'] = qmp.execute('query-llfree-balloon')
                qmp.execute('quit')
                if args.tuning: os.write(host.write_fd, b'exit\n')
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
