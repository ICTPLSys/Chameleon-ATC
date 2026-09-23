#!/usr/bin/env python3
"""Run checkpoint tests on an existing disk Guest through SSH and QMP.

The VM must be idle and keep running throughout the test. C4/C5 require no
registered data backend. Hermit requires an already connected 4 MiB backend.
This runner never reboots QEMU or changes PCI/network/backend connections.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / filename)
    obj = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obj)
    return obj


guest = module('live_guestctl', 'guestctl.py')
control = module('live_control', 'test-chameleon-control.py')
policy = module('live_policy', 'test-chameleon-policy.py')
hermit = module('live_hermit', 'test-chameleon-hermit.py')
require = control.require

SNAPSHOT = r'''
import json, pathlib, platform, re, subprocess
root = pathlib.Path('/sys/kernel/debug')
result = {'kernel': platform.release(), 'stats': {}, 'thp': {}, 'rdma': {}}
result['manager_candidates'] = (root / 'chameleon_mm/candidates').read_text()
result['cleanup_available'] = (root / 'chameleon_control_cleanup/control').exists()
for name in ('chameleon', 'chameleon_mm', 'chameleon_policy', 'chameleon_shadow', 'hermit', 'chameleon_psi_load'):
    path = root / name / 'stats'
    if path.exists():
        result['stats'][name] = path.read_text()
thp = pathlib.Path('/sys/kernel/mm/transparent_hugepage')
for path in [thp / 'enabled', thp / 'defrag', thp / 'khugepaged/defrag',
             thp / 'khugepaged/scan_sleep_millisecs', *thp.glob('hugepages-*kB/enabled')]:
    text = path.read_text().strip()
    selected = re.search(r'\[([^]]+)\]', text)
    result['thp'][str(path)] = selected[1] if selected else text
for directory in ('counters', 'hw_counters'):
    for path in pathlib.Path('/sys/class/infiniband').glob('*/ports/*/' + directory + '/*'):
        try:
            value = path.read_text().strip()
            if value.isdigit(): result['rdma'][str(path)] = int(value)
        except OSError:
            pass
result['dmesg'] = subprocess.check_output(['dmesg'], text=True)
print(json.dumps(result))
'''

RESTORE = r'''
import json, pathlib, re, sys
saved = json.load(sys.stdin)
root = pathlib.Path('/sys/kernel/debug')
def fields(text):
    return dict(re.findall(r'^(\w+) (\S+)$', text, re.M))
def write(name, text):
    (root / name / 'control').write_text(text + '\n')
if (root / 'chameleon_psi_load/control').exists():
    write('chameleon_psi_load', 'stop')
write('chameleon_policy', 'disable')
write('chameleon_policy', 'clear_target')
write('chameleon_mm', 'disable')
write('chameleon_mm', 'putback')
write('chameleon_mm', 'clear_target')
write('chameleon_shadow', 'drain')
for path, value in saved['thp'].items():
    pathlib.Path(path).write_text(value + '\n')
p = fields(saved['stats']['chameleon_policy'])
for key in ('epoch_us', 'threshold_ppm', 'psi_full', 'free_pages', 'cold_folios', 'minimum_local_bytes', 'discard_test'):
    write('chameleon_policy', 'set ' + key + ' ' + p[key])
m = fields(saved['stats']['chameleon_mm'])
write('chameleon_mm', 'batch ' + ' '.join(m[k] for k in ('batch', 'Csync', 'Etrans', 'Nactive', 'Lptw_explicit')))
for order, cost in re.findall(r'^order=(\d+) .* reclaim=(\d+)$', saved['stats']['chameleon_mm'], re.M):
    write('chameleon_mm', 'cost ' + order + ' ' + cost)
write('chameleon_mm', 'split_mode ' + m['split_mode'])
write('chameleon_mm', 'selector ' + m['selector_mode'])
write('chameleon_mm', 'memtis ' + m['memtis_min_bin'] + ' ' + m['memtis_budget'])
t = fields(saved['stats']['chameleon'])
write('chameleon', 'disable')
write('chameleon', 'capacity ' + t['local_bytes'] + ' ' + t['total_bytes'])
for key, fixed in (('sampling', 'fixed_sample_period'), ('cooling', 'fixed_cooling_samples')):
    mode = t[key + '_mode']
    write('chameleon', key + ' fixed ' + t[fixed])
    if mode == 'adaptive': write('chameleon', key + ' adaptive')
if t['enabled'] == '1': write('chameleon', 'enable')
if m['enabled'] == '1': write('chameleon_mm', 'enable')
if 'hermit' in saved['stats']:
    h = fields(saved['stats']['hermit'])
    current = fields((root / 'hermit/stats').read_text())
    write('hermit', 'delay ' + h['delay_ms'])
    write('hermit', 'fail_store 0')
    write('hermit', 'fail_load 0')
    if h['registered'] != current['registered']:
        write('hermit', 'register' if h['registered'] == '1' else 'unregister')
print('Guest settings restored; test counters and tracker sample contents are retained as evidence')
'''


def fields(text):
    return {k: int(v) if re.fullmatch(r'-?\d+', v) else v
            for k, v in re.findall(r'^(\w+) (\S+)$', text, re.MULTILINE)}


def remote_python(access, program, data=None):
    result = subprocess.run(guest.ssh_command(access, ['sudo', '-n', 'python3', '-c', program]),
                            input=data, text=True, capture_output=True, timeout=60)
    if result.returncode:
        raise RuntimeError('Guest Python failed: ' + result.stderr + result.stdout)
    return result.stdout


def snapshot(access):
    return json.loads(remote_python(access, SNAPSHOT))


def forget_c4_records(access, qmp, console):
    """Forget only terminal identities emitted by this invocation's C4 test."""
    owned = {int(token) for token in re.findall(r'^C4_OBJECT token=(\d+) ', console.data, re.MULTILINE)}
    current = qmp.execute('query-llfree-balloon')
    ranges = current['chameleon']['ranges']
    require(all(r['token'] in owned for r in ranges), 'cleanup refuses a record not created by this C4 test')
    terminal = [r for r in ranges if r['state'] in (6, 7)]
    require(all(r['flags'] == 1 for r in terminal), 'cleanup is restricted to disposable C4 test ranges')
    if terminal:
        program = """
import json, pathlib, sys
control = pathlib.Path('/sys/kernel/debug/chameleon_control_cleanup/control')
for r in json.load(sys.stdin):
    control.write_text('forget {token} {gpa} {pages} {order} {flags}\\n'.format(**r))
print('Explicitly forgot terminal C4 test records')
"""
        remote_python(access, program, json.dumps(terminal))
    return terminal


def preflight(before, current, stage, backend):
    stats = before['stats']
    for name in ('chameleon', 'chameleon_mm', 'chameleon_policy', 'chameleon_shadow'):
        require(name in stats, 'missing Guest debugfs ' + name)
    p, m, s = (fields(stats[n]) for n in ('chameleon_policy', 'chameleon_mm', 'chameleon_shadow'))
    require(not p['enabled'] and not p['lease_active'] and not p['target_pid'],
            'disable and clear the existing policy target before testing')
    require(not m['enabled'] and not m['target_pid'] and not before['manager_candidates'].strip(),
            'stop the manager and release its target/candidates before testing')
    require(m['cost_valid'] and all(m[k] > 0 for k in ('batch', 'Etrans', 'Lptw_explicit')),
            'configure manager batch/cost parameters before testing: the control ABI cannot restore an unset cost model')
    require(not s['live_objects'] and not s['reservation_pages'], 'Guest Shadow must be idle')
    if 'chameleon_psi_load' in stats:
        require(fields(stats['chameleon_psi_load'])['workers'] == 0,
                'stop the existing PSI fixture before testing')
    ch = current['chameleon']
    require(not ch['range-records'] and not ch['registered-pages'] and not ch['retired-pages'] and
            not ch['blocked-pages'] and not ch['policy']['lease'], 'Host Chameleon must be idle')
    require(current['actual'] == ch['policy']['total-bytes'], 'return legacy balloon capacity before testing')
    if stage == 'hermit':
        require('hermit' in stats, 'load the matching Hermit module before testing')
        h = fields(stats['hermit'])
        require(h['registered'] == 1 and h['capacity_pages'] == 1024 and not h['live_slots'] and
                not h['inflight'], 'Hermit test requires an idle registered pool_mb=4 backend')
        require(h['backend'] == backend, 'requested Hermit backend must already be connected')
    else:
        require(not s['backend_present'], 'unregister the Hermit backend before C4/C5 disposable-page tests')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--name', default='chameleon')
    parser.add_argument('--access', type=Path)
    parser.add_argument('--stage', choices=('c4', 'c5', 'hermit'), required=True)
    parser.add_argument('--backend', choices=('dram', 'rdma'), default='rdma')
    parser.add_argument('--remote-dir', default='/home/ubuntu/chameleon-tests')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--psi-module', type=Path,
                        help='optional matching Guest chameleon_psi_load.ko; otherwise it must already be loaded')
    parser.add_argument('--cleanup-module', type=Path,
                        help='C4: matching chameleon_control_cleanup.ko unless already loaded')
    args = parser.parse_args()
    require(args.remote_dir.startswith('/') and not any(x in args.remote_dir for x in '\n\r\0'),
            'remote directory must be an absolute single-line path')
    access = guest.load_access(args.access or ROOT / 'build/guests' / args.name / 'access.json')
    require(access['name'] == args.name, 'access identity differs from requested VM')
    output = (args.output or ROOT / 'results/deployment' /
              ('live-' + args.name + '-' + args.stage + '-' + time.strftime('%Y%m%d-%H%M%S'))).resolve()
    output.mkdir(parents=True, exist_ok=True)
    report = {'status': 'RUNNING', 'vm': args.name, 'stage': args.stage,
              'scope': 'existing disk Guest, real SSH checkpoints and QMP backing observations',
              'host_trace': 'not captured by this runner', 'started': time.strftime('%Y-%m-%dT%H:%M:%S%z')}
    qmp = process = console = before = current = None
    loaded_psi = loaded_cleanup = mutated = False
    lock = None
    started = time.monotonic()
    try:
        # Also excludes guestctl start/stop/setup operations for this same VM.
        candidate_lock = guest.control_lock(access)
        candidate_lock.__enter__()
        lock = candidate_lock
        guest.wait_ssh(access, 30)
        qmp = control.vm.QMP(str(guest.run_dir(access) / 'qmp.sock'))
        require(qmp.execute('query-name').get('name') == args.name, 'QMP VM identity mismatch')
        require(qmp.execute('query-status')['running'], 'VM must already be running')
        require(qmp.execute('query-kvm')['enabled'], 'test requires actual KVM')
        current = qmp.execute('query-llfree-balloon')
        before = snapshot(access)
        report['before'] = {'qmp': current, 'guest': before}
        preflight(before, current, args.stage, args.backend)
        ram = current['chameleon']['policy']['total-bytes']
        vfio = current['chameleon']['vfio-coordinated']
        report.update(ram_bytes=ram, vfio_coordinated=vfio)
        if args.stage == 'c4' and not before['cleanup_available']:
            require(args.cleanup_module and args.cleanup_module.is_file(),
                    'C4 on a persistent VM requires --cleanup-module to forget terminal test records')
        binary_name = {'c4': 'chameleon_control', 'c5': 'chameleon_policy', 'hermit': 'chameleon_hermit'}[args.stage]
        guest.remote(access, ['mkdir', '-p', args.remote_dir])
        binary = args.remote_dir + '/' + binary_name
        guest.transfer(access, 'upload', ROOT / 'tests' / binary_name, binary)
        guest.remote(access, ['chmod', '0755', binary])
        if args.stage == 'c4' and not before['cleanup_available']:
            cleanup_path = args.remote_dir + '/chameleon_control_cleanup.ko'
            guest.transfer(access, 'upload', args.cleanup_module, cleanup_path)
            guest.remote(access, ['sudo', '-n', 'insmod', cleanup_path])
            loaded_cleanup = True
        if args.stage != 'c4' and 'chameleon_psi_load' not in before['stats']:
            require(args.psi_module and args.psi_module.is_file(),
                    'C5/Hermit needs a matching loaded PSI fixture or --psi-module')
            module_path = args.remote_dir + '/chameleon_psi_load.ko'
            guest.transfer(access, 'upload', args.psi_module, module_path)
            guest.remote(access, ['sudo', '-n', 'insmod', module_path])
            loaded_psi = True
        process = subprocess.Popen(guest.ssh_command(access, ['sudo', '-n', 'sh']),
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        console = control.vm.Console(process.stdout.fileno(), process.stdin.fileno(), output / 'ssh-console.log')
        console.command('export CHAMELEON_TEST_RAM_BYTES=' + str(ram))
        mutated = True
        console.command("printf 'sampling adaptive\\n' > /sys/kernel/debug/chameleon/control; "
                        "printf 'cooling adaptive\\n' > /sys/kernel/debug/chameleon/control; "
                        "printf 'split_mode hhh\\n' > /sys/kernel/debug/chameleon_mm/control; "
                        "printf 'selector mixed_cost\\n' > /sys/kernel/debug/chameleon_mm/control")
        if args.stage == 'c4':
            evidence = control.run_control(console, qmp, output, ram=ram, vfio=vfio, binary=binary)
        elif args.stage == 'c5':
            evidence = policy.run_policy(console, qmp, output, False, ram=ram, binary=binary)
        else:
            evidence = hermit.run_hermit(console, qmp, output, args.backend, vfio=vfio, ram=ram, binary=binary)
        report['test'] = evidence
        report['checks'] = evidence['checks']
        final = qmp.execute('query-llfree-balloon')
        report['after_test_qmp'] = final
        if args.stage == 'c4':
            report['c4_forgotten_records'] = forget_c4_records(access, qmp, console)
            final = qmp.execute('query-llfree-balloon')
            report['after_c4_cleanup_qmp'] = final
        require(final['actual'] == ram and not final['chameleon']['registered-pages'] and
                not final['chameleon']['retired-pages'] and not final['chameleon']['blocked-pages'] and
                not final['chameleon']['range-records'], 'test must release every Host reservation')
        report['status'] = 'PASS'
    except Exception as error:
        report.update(status='FAIL', error=repr(error))
    finally:
        cleanup_errors = []
        if qmp and mutated:
            try:
                qmp.execute('chameleon-configure', {'config': {'install-fail-count': 0,
                            'dma-map-fail-count': 0, 'discard-fail-index': -1}})
            except Exception as error:
                cleanup_errors.append('clear fault injections: ' + repr(error))
        if process:
            process.stdin.close()  # Checkpoint fgets gets EOF and the owned test exits.
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                cleanup_errors.append('owned SSH test did not exit after stdin EOF; no VM process killed')
        if mutated and before and (not process or process.poll() is not None):
            try:
                report['restore_guest'] = remote_python(access, RESTORE, json.dumps(before))
            except Exception as error:
                cleanup_errors.append('restore Guest settings: ' + repr(error))
            if args.stage == 'c4' and console:
                try:
                    report['c4_cleanup_after_exit'] = forget_c4_records(access, qmp, console)
                except Exception as error:
                    cleanup_errors.append('forget terminal C4 test records after exit: ' + repr(error))
        if qmp and mutated and current:
            try:
                saved = current['chameleon']
                qmp.execute('chameleon-configure', {'config': {key: saved[key] for key in
                            ('ept-mode', 'batch-pages', 'watermark-bytes')}})
                report['after_qmp'] = qmp.execute('query-llfree-balloon')
                require(qmp.execute('query-status')['running'], 'existing VM must remain running')
            except Exception as error:
                cleanup_errors.append('restore QMP: ' + repr(error))
        if loaded_psi:
            try:
                guest.remote(access, ['sudo', '-n', 'rmmod', 'chameleon_psi_load'])
            except Exception as error:
                cleanup_errors.append('unload test PSI module: ' + repr(error))
        if loaded_cleanup:
            try:
                guest.remote(access, ['sudo', '-n', 'rmmod', 'chameleon_control_cleanup'])
            except Exception as error:
                cleanup_errors.append('unload C4 cleanup module: ' + repr(error))
        if before:
            try:
                after = snapshot(access)
                report['after_guest'] = after
                report['rdma_counter_delta'] = {key: value - before['rdma'].get(key, value)
                    for key, value in after['rdma'].items()}
                old_lines = before['dmesg'].splitlines()
                new_lines = after['dmesg'].splitlines()
                new_dmesg = '\n'.join(new_lines[len(old_lines):]) if new_lines[:len(old_lines)] == old_lines else after['dmesg']
                (output / 'guest-dmesg-new.log').write_text(new_dmesg + '\n')
                if re.search(hermit.BAD, new_dmesg):
                    cleanup_errors.append('new Guest kernel diagnostics contain BUG/WARNING/Oops')
            except Exception as error:
                cleanup_errors.append('capture final Guest state: ' + repr(error))
        if qmp:
            qmp.file.close()
            qmp.sock.close()
        if lock:
            lock.__exit__(None, None, None)
        if cleanup_errors:
            report['cleanup_errors'] = cleanup_errors
            report['status'] = 'FAIL'
        report['duration_seconds'] = round(time.monotonic() - started, 3)
        (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
        summary = {key: value for key, value in report.items() if key in
                   ('status', 'checks', 'error', 'cleanup_errors', 'duration_seconds')}
        summary['report'] = str(output / 'report.json')
        print(json.dumps(summary, indent=2), flush=True)
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
