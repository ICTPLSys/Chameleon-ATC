#!/usr/bin/env python3
"""Exercise R1/R2 on a new nested RDMA VM and R3 with RDMA or real PEBS.

Only VMs launched by this process are stopped. The existing disk Guest,
physical VF bindings and physical Host modules are never changed.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('r123_hermit', ROOT / 'scripts/test-chameleon-hermit.py')
hermit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hermit)
vm, nested, require = hermit.vm, hermit.nested, hermit.require
PAGE, RAM = 4096, 2 << 30
BINARIES = {'r1': 'chameleon_scale', 'r2': 'chameleon_fault_accounting',
            'r3': 'chameleon_swap_tracking'}
PHASES = {
    'r1': ('initial', 'retired_scale', 'restored_scale', 'finished'),
    'r2': ('initial', 'retired_demand', 'demand_done', 'retired_background',
           'background_done', 'retired_failure', 'failure_done',
           'retired_concurrent', 'concurrent_done', 'retired_policy',
           'policy_done', 'finished'),
}


def checkpoint_test(console, qmp, stage, output):
    tag = stage.upper()
    evidence = {'guest': {}}
    start = cursor = len(console.data)
    os.write(console.write_fd, ('/tests/' + BINARIES[stage] + '; echo ' + tag + '_EXIT=$?\n').encode())
    try:
        for phase in PHASES[stage]:
            found, chunk = console.wait(r'^' + tag + '_WAIT ' + phase + r'\s*$|^' + tag + r'_EXIT=\d+\s*$',
                                        start=cursor, timeout=240)
            require(found[0].startswith(tag + '_WAIT'), 'Guest exited before ' + phase)
            cursor = len(console.data)
            guest = {}
            for label in ('SHADOW', 'BACKEND', 'POLICY'):
                block = re.search(tag + '_' + label + '_BEGIN ' + phase + r'\n(.*?)' +
                                  tag + '_' + label + '_END ' + phase, chunk, re.S)
                require(block, 'Guest ' + label + ' statistics exist at ' + phase)
                guest[label.lower()] = {key: int(value) for key, value in
                    re.findall(r'^(\w+) (\d+)$', block[1], re.MULTILINE)}
            evidence['guest'][phase] = guest
            current = qmp.execute('query-llfree-balloon')
            ch = current['chameleon']
            evidence[phase] = current
            print(tag + ' ' + phase + ': retired=' + str(ch['retired-pages']) +
                  ' records=' + str(ch['range-records']), flush=True)
            require(current['actual'] == RAM - ch['retired-pages'] * PAGE,
                    'actual capacity matches real retirement')
            require(all(r['residency-status'] == 0 for r in ch['ranges']), 'Host mincore must succeed')
            if phase == 'initial':
                qmp.execute('chameleon-configure', {'config': {'batch-pages': 1,
                            'watermark-bytes': 0, 'ept-mode': 'deferred'}})
            elif phase == 'retired_scale':
                rows = ch['ranges']
                require(ch['retired-pages'] == ch['blocked-pages'] == (48 << 20) // PAGE,
                        '48 MiB of actual backing remains simultaneously absent')
                require(ch['retired-pages'] > 4 * ch['pool-pages'] and not ch['registered-pages'],
                        'pending credit is reusable beyond four times the 0.5 percent window')
                require(sum(r['order'] == 0 for r in rows) > 4096 and
                        {r['order'] for r in rows} == {0, 2, 3, 4, 5, 6, 7, 8, 9},
                        'more than 4096 base ranges coexist with every native mTHP order')
                require(all(r['state'] == 5 and r['flags'] == 2 and not r['resident-pages']
                            and r['no-huge'] for r in rows), 'all saved source backing is absent')
                sources = {int(t): (int(p) * PAGE, int(n), int(o)) for t, p, n, o in
                           re.findall(r'^R1_OBJECT token=(\d+) pfn=(\d+) pages=(\d+) order=(\d+)$',
                                      chunk, re.MULTILINE)}
                require(len(sources) == len(rows) and all(sources.get(r['token']) ==
                        (r['gpa'], r['pages'], r['order']) for r in rows),
                        'every Host range matches the original actual Guest PFN and token')
                require(ch['policy']['local-bytes'] == RAM - (48 << 20) and ch['policy']['lease'],
                        'policy reaches the configured floor without double charging retirement')
            elif phase.startswith('retired_'):
                objects = re.findall(r'^R2_OBJECT phase=' + phase +
                    r' token=(\d+) pfn=(\d+) pages=(\d+) order=(\d+)$', chunk, re.MULTILINE)
                if phase == 'retired_policy':
                    require(ch['retired-pages'] == 1024 and len(ch['ranges']) == 2 and ch['policy']['lease'],
                            'two ordinary policy-owned 2 MiB folios await real demand pressure')
                else:
                    require(len(objects) == 1, 'record the actual source identity')
                    token, pfn, pages, order = map(int, objects[0])
                    require(len(ch['ranges']) == 1 and (ch['ranges'][0]['token'],
                        ch['ranges'][0]['gpa'], ch['ranges'][0]['pages'], ch['ranges'][0]['order']) ==
                        (token, pfn * PAGE, pages, order), 'Host retirement matches actual source PFN')
                require(all(r['state'] == 5 and r['flags'] == 2 and not r['resident-pages']
                            for r in ch['ranges']), 'data is genuinely remote before actual fault')
            if phase == 'initial' or phase.endswith('_done') or phase in ('restored_scale', 'finished'):
                require(current['actual'] == RAM and not ch['range-records'] and not ch['retired-pages']
                        and not ch['blocked-pages'] and not ch['registered-pages'],
                        'completed phase releases every Host record and physical reservation')
            os.write(console.write_fd, b'go\n')
        found, chunk = console.wait(r'^' + tag + r'_EXIT=(\d+)\s*$', start=start, timeout=240)
        require(int(found[1]) == 0, 'Guest executable must complete successfully')
        passed = re.search(r'^PASS CHAMELEON_' + tag + r' checks=(\d+)$', chunk, re.MULTILINE)
        require(passed, 'Guest final PASS marker is required')
        evidence['checks'] = int(passed[1])
        return evidence
    finally:
        (output / (stage + '.log')).write_text(console.data[start:])
        (output / (stage + '-phases.json')).write_text(json.dumps(evidence, indent=2) + '\n')


def check_trace(trace, after):
    groups = {}
    for tx, phase, ranges, pages, seq in re.findall(
            r'kvm_chameleon: transaction=(\d+) phase=(\w+) ranges=(\d+) pages=(\d+) nativeflush_seq=(\d+)', trace):
        groups.setdefault(int(tx), []).append({'phase': phase, 'ranges': int(ranges),
                                               'pages': int(pages), 'seq': int(seq)})
    require(len(groups) == after['chameleon']['begin-batches'] and groups,
            'trace contains every actual Host discard transaction')
    for tx, rows in groups.items():
        require([r['phase'] for r in rows] == ['BEGIN_ZAP', 'FLUSH_DONE', 'REPORT'] +
                ['INSTALL'] * rows[0]['ranges'], 'complete zap/flush/discard/install order for ' + str(tx))
        require(rows[1]['seq'] == rows[0]['seq'] + 1 and rows[2]['seq'] == rows[1]['seq'],
                'exactly one native flush completes before backing discard')
        require(rows[0]['pages'] == rows[1]['pages'] == sum(r['pages'] for r in rows[3:]),
                'every actual discarded page is installed again')
    return {'transactions': len(groups), 'discarded_pages': sum(rows[0]['pages'] for rows in groups.values()),
            'complete_flush_and_install_order': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', choices=BINARIES, required=True)
    parser.add_argument('--name')
    parser.add_argument('--initramfs', type=Path, default=ROOT / 'build/r1-r3-rdma-host.cpio.gz')
    parser.add_argument('--direct-pebs', action='store_true', help='R3: new direct KVM Guest with DRAM backend and genuine PEBS')
    args = parser.parse_args()
    require(not args.direct_pebs or args.stage == 'r3', 'direct hardware mode is an R3 test')
    name = args.name or ('r123-' + args.stage + ('-pebs' if args.direct_pebs else '-rdma'))
    output = ROOT / 'results' / ('vm-' + name)
    output.mkdir(parents=True, exist_ok=True)
    binary = ROOT / 'tests' / BINARIES[args.stage]
    kernel = ROOT / ('build/guest/arch/x86/boot/bzImage' if args.direct_pebs else 'build/host/arch/x86/boot/bzImage')
    image = ROOT / 'build/guest-initramfs.cpio.gz' if args.direct_pebs else args.initramfs.resolve()
    qmp_port, serial_port = nested.reserve_ports()
    qmp_path = output / 'qmp.sock'
    if args.direct_pebs:
        require(not qmp_path.exists(), 'choose a fresh output name if a QMP socket exists')
        command = [str(ROOT / 'build/qemu/qemu-system-x86_64'), '-L', '/usr/share/qemu',
            '-machine', 'pc,mem-merge=off',
            '-accel', 'kvm,hyperalloc-pebs-meminfo=on', '-cpu', 'host,migratable=off,pmu=on',
            '-m', '2048', '-smp', '4', '-nodefaults', '-display', 'none', '-serial', 'stdio',
            '-no-reboot', '-kernel', str(kernel), '-initrd', str(image),
            '-append', 'console=ttyS0 rdinit=/init panic=1 nokaslr', '-qmp',
            'unix:' + str(qmp_path) + ',server=on,wait=off']
        for thread in ['auto'] + ['install' + str(i) for i in range(4)]:
            command += ['-object', 'iothread,id=' + thread]
        command += ['-device', json.dumps({'driver': 'virtio-llfree-balloon', 'id': 'ha',
            'auto-mode': False, 'chameleon': True, 'chameleon-policy': True,
            'chameleon-batch-pages': 1, 'chameleon-watermark-bytes': 0,
            'auto-mode-iothread': 'auto', 'iothread-vq-mapping':
            [{'iothread': 'install' + str(i)} for i in range(4)]})]
    else:
        command = ['/usr/bin/qemu-system-x86_64', '-accel', 'kvm', '-cpu', 'host',
            '-m', '4096', '-smp', '8', '-nodefaults', '-display', 'none', '-serial', 'stdio',
            '-monitor', 'none', '-no-reboot', '-kernel', str(kernel), '-initrd', str(image),
            '-append', 'console=ttyS0 rdinit=/init panic=1 nokaslr', '-netdev',
            f'user,id=outer,hostfwd=tcp:127.0.0.1:{qmp_port}-:4444,hostfwd=tcp:127.0.0.1:{serial_port}-:4445',
            '-device', 'virtio-net-pci,netdev=outer']
    (output / 'command.json').write_text(json.dumps(command, indent=2) + '\n')
    inputs = [kernel, image, binary, ROOT / 'build/guest/arch/x86/boot/bzImage',
              ROOT / 'build/qemu/qemu-system-x86_64', ROOT / 'hermit/client/rswap-client.ko']
    report = {'status': 'RUNNING', 'stage': args.stage, 'backend': 'dram' if args.direct_pebs else 'rdma',
        'pebs_required': args.direct_pebs, 'scope': 'new isolated VM; existing physical deployment untouched',
        'input_sha256': {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs}}
    process = qmp = serial = console = None
    started = time.monotonic()
    try:
        with (output / 'qemu.log').open('w') as log:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log)
            host = vm.Console(process.stdout.fileno(), process.stdin.fileno(), output / 'host-serial.log')
            if args.direct_pebs:
                host.wait(r'^HYPERALLOC_READY$', timeout=180)
                console = host
                qmp = vm.QMP(str(qmp_path))
            else:
                host.wait(r'^HERMIT_NESTED_READY .*$', timeout=180)
                serial = socket.create_connection(('127.0.0.1', serial_port), timeout=30)
                serial.settimeout(None)
                console = vm.Console(serial.fileno(), serial.fileno(), output / 'guest-serial.log')
                qmp = vm.QMP(('127.0.0.1', qmp_port))
                qmp.execute('cont')
                console.wait(r'^HYPERALLOC_READY$', timeout=180)
            require(qmp.execute('query-kvm')['enabled'], 'actual KVM is required')
            # Large R1 identity dumps span many UART writes. Routine kernel
            # warnings must not splice into a token/PFN line; the complete
            # ring buffer is still collected and checked after the test.
            console.command('dmesg -n 1; test ! -d /sys/module/chameleon_psi_load')
            if not args.direct_pebs:
                console.command('export PATH=/opt/hermit-rdma/bin:/usr/bin:/usr/sbin:/sbin:/bin; '
                    'export LD_LIBRARY_PATH=/opt/hermit-rdma/lib; ulimit -l unlimited; '
                    '/sbin/hermit-network-setup eth0 192.0.2.2 rxe', timeout=40)
            console.command('insmod /tests/rswap-client.ko backend=' + report['backend'] +
                            ' pool_mb=128 sip=192.0.2.1 sport=9400', timeout=40)
            report['before'] = qmp.execute('query-llfree-balloon')
            if args.stage in PHASES:
                report['test'] = checkpoint_test(console, qmp, args.stage, output)
            else:
                qmp.execute('chameleon-configure', {'config': {'batch-pages': 1,
                            'watermark-bytes': 0, 'ept-mode': 'deferred'}})
                text = console.command('/tests/' + BINARIES[args.stage] +
                    ('' if args.direct_pebs else ' --without-pebs'), timeout=240)
                (output / 'r3.log').write_text(text)
                passed = re.search(r'^PASS CHAMELEON_R3 checks=(\d+)', text, re.MULTILINE)
                require(passed, 'R3 final PASS marker required')
                report['test'] = {'checks': int(passed[1])}
            report['after'] = qmp.execute('query-llfree-balloon')
            ch = report['after']['chameleon']
            require(report['after']['actual'] == RAM and not ch['range-records'] and
                    not ch['retired-pages'] and not ch['blocked-pages'], 'all Host resources drain')
            report['guest_stats'] = console.command('cat /sys/kernel/debug/chameleon_shadow/stats; '
                'cat /sys/kernel/debug/chameleon/stats; cat /sys/kernel/debug/hermit/stats')
            console.command('rmmod rswap_client; test ! -d /sys/module/rswap_client')
            diagnostic = console.command('dmesg')
            (output / 'guest-dmesg.log').write_text(diagnostic)
            require(not re.search(hermit.BAD, diagnostic), 'Guest kernel diagnostics remain clean')
            qmp.execute('quit')
            if not args.direct_pebs:
                host.wait(r'^HERMIT_HOST_DMESG_END$', timeout=60)
            require(process.wait(timeout=60) == 0, 'owned VM exits successfully')
            if not args.direct_pebs:
                require(not re.search(hermit.BAD, host.data), 'Host kernel diagnostics remain clean')
                match = re.search(r'HERMIT_HOST_TRACE_BEGIN\n(.*?)HERMIT_HOST_TRACE_END', host.data, re.S)
                require(match, 'actual new Host trace is present')
                (output / 'host-trace.log').write_text(match[1])
                report['host_trace'] = check_trace(match[1], report['after'])
            report.update(status='PASS', checks=report['test']['checks'])
    except Exception as error:
        report.update(status='FAIL', error=repr(error))
        if console:
            try:
                (output / 'failure-diagnostics.log').write_text(console.command('dmesg; '
                    'cat /sys/kernel/debug/chameleon_shadow/stats; cat /sys/kernel/debug/chameleon_policy/stats', timeout=10))
            except Exception:
                pass
    finally:
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if serial:
            serial.close()
        if qmp:
            qmp.file.close()
            qmp.sock.close()
        report['duration_seconds'] = round(time.monotonic() - started, 3)
        (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps({k: report[k] for k in ('status', 'stage', 'checks', 'error', 'duration_seconds')
                          if k in report}, indent=2), flush=True)
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
