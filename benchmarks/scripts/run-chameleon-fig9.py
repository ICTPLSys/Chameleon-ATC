#!/usr/bin/env python3
"""Run Mix1–4 with three independent QEMU Guests and saved high-point knobs.

Default is a read-only plan. --run starts three RDMA pools and three VMs for
each mix, releases the workload barrier, records real results, and cleans up.
"""
import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

import chameleon_fig9 as data
import chameleon_fig9_runtime as rt

HERE = Path(__file__).resolve().parent


def build_plan(inventory, mixes, qualified, plot_source):
    rt.validate_inventory(inventory)
    plans = {}
    for mix in mixes:
        apps = [data.resolve_high(case, qualified) for case in data.MIXES[mix]]
        if any(a['remote_pool_mib'] != 8192 for a in apps):
            raise ValueError('Current Fig9 runner expects one 8192MiB RDMA pool per VM')
        cpus = rt.allocate_cpus(apps, inventory['client_numa_node'])
        plans[mix] = {'applications': apps, 'cpus': cpus,
                      'total_vm_memory_mib': sum(a['vm_memory_mib'] for a in apps)}
    return {'protocol': 'fig9-three-independent-vms-saved-high-v1', 'mixes': plans,
            'reference': data.reference_data(plot_source),
            'metric': 'Arithmetic mean of three application slowdown percentages relative to saved isolated tracked all-local',
            'inventory': inventory}


def leaf_command(name, slot, high, affinity_file, workload_file, memory_file,
                 barrier, placement, timeout, results_root=None, performance_mode=False):
    c = high['configuration']
    argv = [sys.executable, str(HERE / 'run-chameleon-apps.py'), '--name', name,
            '--vm', slot['name'], '--mode', 'chameleon', '--cases', high['application'],
            '--workload-config', str(workload_file), '--memory-plan', str(memory_file),
            '--cpu-affinity-profile', str(affinity_file), '--cpu-pinning',
            '--start-barrier-dir', str(barrier), '--barrier-member', high['application'],
            '--barrier-timeout', str(timeout), '--timeout', str(timeout),
            '--sample-seconds', '1', '--all-local-tracking',
            '--client-numa-node', str(placement['client_numa_node']),
            '--client-cpus', ','.join(map(str, placement['client_cpus'])),
            '--rdma-interface', slot['rdma_interface']]
    if results_root is not None:
        argv += ['--results-root', str(results_root)]
    if performance_mode:
        argv.append('--performance-mode')
    for key in ('minimum_local_mib', 'psi_ppm', 'epoch_us', 'cold_folios', 'sample_period',
                'cooling_samples', 'hhh_interval_ms', 'free_pages',
                'pre_reclaim_headroom_mib', 'pre_reclaim_epoch_us'):
        value = c[key]
        if int(value) != value:
            raise ValueError('Nonintegral saved parameter: ' + key)
        argv += ['--' + key.replace('_', '-'), str(int(value))]
    return argv


def wait_ready(barrier, children, timeout, stopped=lambda: False, server_processes=(), cpu_check=None):
    deadline = time.monotonic() + timeout
    last = None
    while True:
        rt.assert_servers(server_processes)
        ready = {case: barrier / (case + '.ready.json') for case in children}
        found = [case for case, path in ready.items() if path.exists()]
        if stopped():
            raise KeyboardInterrupt('Experiment interrupted')
        failed = {case: child.poll() for case, child in children.items() if child.poll() is not None}
        if failed:
            raise RuntimeError('Application exited before synchronized start: ' + str(failed))
        if cpu_check is not None:
            cpu_check()
        if len(found) == 3:
            if cpu_check is not None:
                cpu_check(force=True)
            evidence = {case: json.loads(path.read_text()) for case, path in ready.items()}
            release = {'released_unix_seconds': time.time(), 'members': list(children)}
            rt.save(barrier / 'RELEASE', release)
            print('RELEASE ' + json.dumps(release), flush=True)
            return {'ready': evidence, **release}
        if time.monotonic() > deadline:
            raise TimeoutError('Waiting for workload readiness: ' + str(found))
        if found != last:
            print('READY ' + json.dumps(found), flush=True)
            last = found
        time.sleep(.2)


def overlap(records, release):
    # Clients load/warm up before readiness. Batch applications wait before
    # launch. Endpoints bound their measured work; launcher startup/cleanup
    # remain included in the existing application metric.
    starts = [max(release, r.get('timed_phase_start_unix_seconds', r['workload_start_unix_seconds'])) for r in records]
    ends = [r.get('timed_phase_end_unix_seconds', r['workload_end_unix_seconds']) for r in records]
    seconds = max(0, min(ends) - max(starts))
    if seconds <= 0:
        raise ValueError('No common post-barrier workload interval among the three VMs')
    return {'common_seconds': seconds, 'start_unix_seconds': max(starts),
            'end_unix_seconds': min(ends),
            'per_application_post_release_seconds': [e - s for s, e in zip(starts, ends)],
            'scope': 'Client READ/generator intervals and batch launcher intervals after readiness; shorter applications finish first. Does not assert full-duration three-way interference.'}


def hardware_check(inventory, plan, template):
    rt.validate_inventory(inventory, hardware=True)
    if rt.guest.active(template) or not rt.guest.launcher_idle(template):
        raise ValueError('Stop the template VM before using its installed disk as an overlay backing')
    source = json.loads(Path(template['vm_config']).read_text())
    if inventory['server'].get('manage_remote'):
        evidence=rt.check_remote_server(inventory)
        print('REMOTE_SERVER_CHECK '+json.dumps(evidence),flush=True)
    if inventory['server'].get('manage_local'):
        addresses = json.loads(subprocess.run(['ip', '-j', 'address', 'show'], check=True,
                                              capture_output=True, text=True).stdout)
        if not any(info.get('local') == inventory['server']['address']
                   for dev in addresses for info in dev.get('addr_info', [])):
            raise ValueError('Configure Host RDMA address first: ' + inventory['server']['address'])
        if not rt.path(inventory['server']['binary']).is_file():
            raise ValueError('Run benchmarks/scripts/build-fig9-rdma-server.sh first')
    for slot in inventory['slots']:
        directory = rt.HA / 'build/guests' / slot['name']
        if (directory / 'access.json').exists():
            a = rt.access(slot['name'])
            if rt.guest.active(a) or not rt.guest.launcher_idle(a):
                raise ValueError('Experiment slot already in use: ' + slot['name'])
        rt.free_tcp_port(slot['ssh_port'])
    available = int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines()
                         if line.startswith('MemAvailable:'))) // 1024
    for mix in plan['mixes'].values():
        required = mix['total_vm_memory_mib'] + (3 * 8192 if inventory['server'].get('manage_local') else 0) + 8192
        if available < required:
            raise ValueError(f'Host needs at least {required}MiB available before booting this mix; has {available}')
        node_required = {}
        for high in mix['applications']:
            node = mix['cpus']['applications'][high['application']]['host_numa_node']
            node_required[node] = node_required.get(node, 0) + high['vm_memory_mib'] + 1024
        for node, need in node_required.items():
            text = Path(f'/sys/devices/system/node/node{node}/meminfo').read_text()
            fields = {k: int(v) for k, v in re.findall(r'Node \d+ (\w+):\s+(\d+) kB', text)}
            reclaimable = (fields['MemFree'] + fields.get('FilePages', 0) +
                           fields.get('SReclaimable', 0) - fields.get('Shmem', 0)) // 1024
            if reclaimable < need:
                raise ValueError(f'NUMA node{node} needs {need}MiB for bound VM memory; estimated free/reclaimable {reclaimable}MiB')
        for slot, high in zip(inventory['slots'], mix['applications']):
            cpu = mix['cpus']['applications'][high['application']]
            config = rt.slot_config(source, slot, high, cpu)
            # Validate the actual source disk until the independent overlay is created.
            config['disk'] = source['disk']; config['disk_format'] = source['disk_format']
            rt.deploy.preflight(rt.deploy.config(config))
    return source


def run_mix(mix, repetition, spec, inventory, source, directory, name, timeout, server_processes=(),
            results_root=None, performance_mode=False):
    output = directory / mix / f'repeat-{repetition:02d}'
    output.mkdir(parents=True)
    barrier = output / 'barrier'; barrier.mkdir()
    leaves, logs, guards, started, original = {}, [], [], [], {}
    stop_guard = output / 'STOP_GUARDS'
    record = {'status': 'RUNNING', 'applications': [], 'commands': {}, 'mix': mix,
              'repetition': repetition, 'started_unix_seconds': time.time()}
    cpu_guests = {}
    cpu_topology = rt.affinity.topology()
    cpu_last_check = 0.0
    def audit_cpus(force=False):
        nonlocal cpu_last_check
        if not force and time.monotonic() - cpu_last_check < 1:
            return
        observed = rt.verify_vm_cpu_isolation(spec['cpus'], cpu_guests, cpu_topology)
        cpu_last_check = time.monotonic()
        with (output / 'cpu-isolation-samples.jsonl').open('a') as stream:
            stream.write(json.dumps({'unix_seconds': time.time(), **observed}) + '\n')
        previous = record.get('cpu_isolation', {})
        record['cpu_isolation'] = {'status': 'PASS', 'sample_count': previous.get('sample_count', 0) + 1,
                                   'latest': observed, 'samples_file': str(output / 'cpu-isolation-samples.jsonl')}
    rt.save(output / 'report.json', record)
    try:
        for slot, high in zip(inventory['slots'], spec['applications']):
            rt.assert_servers(server_processes)
            a = rt.access(slot['name'])
            original[a['name']] = Path(a['vm_config']).read_text()
            cpu = spec['cpus']['applications'][high['application']]
            config = rt.slot_config(source, slot, high, cpu)
            with rt.guest.control_lock(a):
                rt.save(Path(a['vm_config']), config)
                if Path('/run/numad.pid').exists():
                    stream = (output / (a['name'] + '-numad.log')).open('w'); logs.append(stream)
                    prefix = [] if os.geteuid() == 0 else ['sudo', '-n']
                    guard = subprocess.Popen([*prefix, sys.executable, str(HERE / 'guard-qemu-numad.py'),
                         '--run-dir', str(rt.guest.run_dir(a)), '--stop-file', str(stop_guard),
                         '--duration', str(timeout + 3600)], stdout=stream, stderr=subprocess.STDOUT)
                    guards.append(guard)
                    limit = time.monotonic() + 15
                    while 'READY' not in Path(stream.name).read_text():
                        if guard.poll() is not None or time.monotonic() > limit:
                            raise RuntimeError('NUMA guard did not start: ' + stream.name)
                        time.sleep(.1)
                # Record ownership before start so partial boot failures are cleaned up.
                started.append(a)
                rt.guest.start(a, timeout=300)
                connection = rt.setup_guest(a, slot, inventory['server'])
                record.setdefault('hermit_connections', {})[a['name']] = connection
                rt.save(output / (a['name'] + '-hermit-connection.json'), connection)
                record.setdefault('cpu_pinned_at_boot', {})[high['application']] = rt.pin_guest_cpus(a, cpu)
                cpu_guests[high['application']] = a
            case = high['application']
            prefix = output / case
            cpu_file = prefix.with_suffix('.affinity.json')
            work_file = prefix.with_suffix('.workload.json')
            memory_file = prefix.with_suffix('.memory.json')
            rt.save(cpu_file, cpu)
            rt.save(work_file, {'applications': {case: high['workload_configuration']}})
            rt.save(memory_file, {'case': case, 'memory_mib': high['vm_memory_mib'],
                                 'workload_args': high['workload_configuration']['args'],
                                 'workload_service_args': high['workload_configuration'].get('service_args', []),
                                 'allocation_mode': 'saved-high-point', 'source': high['provenance']})
            leaf_name = f'{name}-{mix}-r{repetition}-{case}'
            command = leaf_command(leaf_name, slot, high, cpu_file, work_file,
                                   memory_file, barrier, spec['cpus'], timeout, results_root, performance_mode)
            record['commands'][case] = command
            record.setdefault('leaf_directories', {})[case] = str((Path(results_root) if results_root else rt.ROOT / 'benchmarks/results/chameleon') / leaf_name)
        # Every VM and RDMA connection is ready before preparing the concurrent workloads.
        audit_cpus(force=True)
        for case, command in record['commands'].items():
            stream = (output / (case + '.log')).open('w'); logs.append(stream)
            leaves[case] = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT,
                                           cwd=rt.ROOT, start_new_session=True)
        record['barrier'] = wait_ready(barrier, leaves, timeout,server_processes=server_processes,cpu_check=audit_cpus)
        rt.save(output / 'report.json', record)
        deadline = time.monotonic() + timeout + 600
        while any(p.poll() is None for p in leaves.values()):
            rt.assert_servers(server_processes)
            audit_cpus()
            failed = {case: p.returncode for case, p in leaves.items() if p.poll() not in (None, 0)}
            if failed:
                raise RuntimeError('Application failed: ' + str(failed))
            if any(p.poll() is not None for p in guards):
                raise RuntimeError('A VM NUMA guard exited during measurement')
            if time.monotonic() > deadline:
                raise TimeoutError('Mix workload/cleanup exceeded timeout')
            time.sleep(1)
        audit_cpus(force=True)
        records = []
        metrics = rt.module('fig9_metrics', HERE / 'chameleon-tuning-metrics.py')
        for high in spec['applications']:
            case = high['application']; leaf = Path(record['leaf_directories'][case])
            if performance_mode:
                ae = rt.module('fig9_ae_metrics', rt.ROOT / 'ae/scripts/ae_fig78.py')
                measured = ae.extract_run(leaf, case)
            else:
                measured = metrics.extract(leaf, case)
            if measured['status'] != 'PASS':
                raise RuntimeError(case + ': ' + str(measured))
            values = data.evaluate_application(case, leaf / case, high)
            values.update(status='PASS', report=measured.get('report', str(leaf / 'report.json')),
                          checks=measured.get('checks', {}),
                          reclaim_percent=measured.get('reclaim_percent', measured.get('reclamation_pct')),
                          counters=measured.get('counters', {}), physical_rdma=measured.get('physical_rdma'))
            record['applications'].append(values)
            sample = json.loads((leaf / 'report.json').read_text())['cases'][case]
            if case in ('cassandra', 'memcached'):
                expected = ['generator-affinity.json'] if case == 'memcached' else ['load-affinity.json', 'run-affinity.json']
                client_evidence = {}
                for filename in expected:
                    paths = list((leaf / case / 'application').rglob(filename))
                    if len(paths) != 1:
                        raise ValueError('Missing/ambiguous live Host client affinity evidence: ' + case + '/' + filename)
                    evidence = json.loads(paths[0].read_text())
                    if evidence.get('status') != 'PASS' or evidence.get('allowed_cpus') != spec['cpus']['client_cpus'] or not evidence.get('observed_pids'):
                        raise ValueError('Host client live thread affinity verification failed: ' + case)
                    client_evidence[filename] = evidence
                record.setdefault('host_client_affinity', {})[case] = client_evidence
                files = list((leaf / case / 'application').rglob('metadata.tsv'))
                if len(files) != 1:
                    raise ValueError('Missing/ambiguous client timing metadata: ' + case)
                meta = dict(line.split('\t', 1) for line in files[0].read_text().splitlines() if '\t' in line)
                for key in ('timed_phase_start_unix_seconds', 'timed_phase_end_unix_seconds'):
                    sample[key] = float(meta[key])
            records.append(sample)
        record['overlap'] = overlap(records, record['barrier']['released_unix_seconds'])
        record.update(data.aggregate_mix(mix, record['applications']))
        record['status'] = 'PASS'
    except BaseException as error:
        record.update(status='CANCELLED' if isinstance(error, KeyboardInterrupt) else 'FAIL', error=repr(error))
        raise
    finally:
        (barrier / 'ABORT').write_text('experiment ending\n')
        for child in leaves.values():
            if child.poll() is None:
                child.send_signal(signal.SIGINT)
        cleanup_errors = []
        for case, child in leaves.items():
            try:
                child.wait(timeout=360)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL); child.wait()
                cleanup_errors.append(case + ': leaf cleanup timed out')
        for a in reversed(started):
            try:
                diagnostic = rt.guest.run_dir(a) / 'hermit-connection.json'
                if diagnostic.is_file():
                    (output / (a['name'] + '-hermit-connection.json')).write_text(diagnostic.read_text())
            except Exception as error:
                cleanup_errors.append(a['name'] + ' connection evidence: ' + repr(error))
            try:
                with rt.guest.control_lock(a):
                    if rt.guest.active(a):
                        rt.stop_guest(a, timeout=180)
            except Exception as error:
                cleanup_errors.append(a['name'] + ': ' + repr(error))
        for slot in inventory['slots']:
            if slot['name'] in original:
                Path(rt.access(slot['name'])['vm_config']).write_text(original[slot['name']])
        stop_guard.write_text('done\n')
        for guard in guards:
            try: guard.wait(timeout=20)
            except subprocess.TimeoutExpired: cleanup_errors.append('NUMA guard did not stop')
        for stream in logs: stream.close()
        record['cleanup_errors'] = cleanup_errors
        if cleanup_errors: record['status'] = 'FAIL'
        record['finished_unix_seconds'] = time.time()
        rt.save(output / 'report.json', record)
    return record


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--name', default='fig9-' + time.strftime('%Y%m%d-%H%M%S'))
    p.add_argument('--inventory', type=Path, default=rt.ROOT / 'ae/config/host.json')
    p.add_argument('--qualified', type=Path, default=rt.ROOT / 'ae/config/fig9-highs.json')
    p.add_argument('--reference-plot', type=Path, default=rt.ROOT / 'ae/results_baselines/fig9.json')
    p.add_argument('--output-dir', type=Path)
    p.add_argument('--results-root', type=Path)
    p.add_argument('--performance-mode', action='store_true')
    p.add_argument('--no-plot', action='store_true')
    p.add_argument('--mixes', choices=list(data.MIXES), nargs='+', default=list(data.MIXES))
    p.add_argument('--repeats', type=int, default=3)
    p.add_argument('--timeout', type=int, default=14400)
    p.add_argument('--baseline-level', choices=['50', '75'], default='75', help='Select provided baseline numbers for plotting only')
    mode = p.add_mutually_exclusive_group()
    mode.add_argument('--plan', action='store_true')
    mode.add_argument('--check', action='store_true')
    mode.add_argument('--prepare', action='store_true')
    mode.add_argument('--run', action='store_true')
    a = p.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,32}', a.name) or a.repeats < 1 or a.timeout < 60:
        p.error('Invalid run name/repeats/timeout')
    if len(set(a.mixes)) != len(a.mixes): p.error('Duplicate mixes')
    inventory = json.loads(a.inventory.read_text())
    plan = build_plan(inventory, a.mixes, a.qualified, a.reference_plot)
    if not (a.check or a.prepare or a.run):
        print(json.dumps(plan, indent=2)); return
    template = rt.access(inventory['template_vm'])
    # Holding the template lock prevents guestctl from booting the backing disk.
    with rt.guest.control_lock(template):
        source = hardware_check(inventory, plan, template)
        if a.check:
            print(json.dumps({'status': 'PASS', 'mixes': a.mixes})); return
        first = plan['mixes'][a.mixes[0]]
        configs = [rt.slot_config(source, slot, high, first['cpus']['applications'][high['application']])
                   for slot, high in zip(inventory['slots'], first['applications'])]
        rt.prepare_slots(inventory, template, configs)
        if a.prepare:
            print(json.dumps({'status': 'PREPARED', 'slots': [s['name'] for s in inventory['slots']]})); return
        out = a.output_dir.resolve() if a.output_dir else rt.ROOT / 'benchmarks/results/chameleon' / a.name
        out.mkdir(parents=True, exist_ok=False)
        rt.save(out / 'plan.json', plan)
        report = {'status': 'RUNNING', 'configuration': vars(a).copy(), 'mixes': {},
                  'reference': plan['reference'], 'plan': str(out / 'plan.json')}
        report['configuration'] = {k: str(v) if isinstance(v, Path) else v for k, v in report['configuration'].items()}
        def stop(signum, frame): raise KeyboardInterrupt('signal ' + str(signum))
        signal.signal(signal.SIGTERM, stop)
        try:
            with rt.servers(inventory, out) as server_processes:
                for mix, spec in plan['mixes'].items():
                    group = report['mixes'][mix] = {'status': 'RUNNING', 'repetitions': []}
                    for rep in range(1, a.repeats + 1):
                        try:
                            row = run_mix(mix, rep, spec, inventory, source, out, a.name, a.timeout,server_processes,
                                          a.results_root, a.performance_mode)
                        except BaseException:
                            saved = out / mix / f'repeat-{rep:02d}' / 'report.json'
                            if saved.exists(): group['repetitions'].append(json.loads(saved.read_text()))
                            group['status'] = 'FAIL'
                            raise
                        group['repetitions'].append(row)
                        if row['status'] != 'PASS': raise RuntimeError('Mix cleanup failed')
                        rt.save(out / 'report.json', report)
                    group['status'] = 'PASS'
            report['status'] = 'PASS'
        except BaseException as error:
            report.update(status='CANCELLED' if isinstance(error, KeyboardInterrupt) else 'FAIL', error=repr(error))
            raise
        finally:
            rt.save(out / 'report.json', report)
            if not a.no_plot:
                subprocess.run([sys.executable, str(HERE / 'plot-chameleon-fig9.py'), '--directory', str(out),
                                '--baseline-level', a.baseline_level, '--errorbars', 'none'], check=False)
        print('RESULT ' + str(out / 'report.json'))


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('Figure 9 interrupted; cleanup recorded in the result directory.', file=sys.stderr)
        raise SystemExit(130)
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        print('fig9: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
