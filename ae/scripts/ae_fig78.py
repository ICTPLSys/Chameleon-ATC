#!/usr/bin/env python3
"""Reproduce Figure 7/8 from frozen points, three fresh-VM runs per point.

No archived measurements are used as denominators. Figure 7 and Figure 8
share the same Memcached/Cassandra executions and use P95 for latency.
"""
import argparse
import copy
import fcntl
import importlib.util
import json
import math
import os
from pathlib import Path
import signal
import statistics
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / 'benchmarks/scripts'
sys.path.insert(0, str(SCRIPTS))
DEFAULT_POINTS = ROOT / 'ae/config/fig78-points.json'
ROLES = ('all_local', 'low', 'medium', 'high')


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + '\n')
    temporary.replace(path)


def positive(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError('Missing/nonpositive/nonfinite ' + label)
    return float(value)


def load_points(path=DEFAULT_POINTS):
    points = json.loads(Path(path).read_text())
    if points.get('repeats') != 3 or not points.get('applications'):
        raise ValueError('Figure 7/8 configuration requires three repeats and applications')
    for case, app in points['applications'].items():
        memory = app['vm_memory_mib']
        positive(memory, case + ' VM memory')
        if type(memory) is not int or memory % 2 or app.get('remote_pool_mib') != 8192:
            raise ValueError('Invalid VM/RDMA memory configuration: ' + case)
        if [p['name'] for p in app['points']] != list(ROLES[1:]):
            raise ValueError('Expected exactly low/medium/high points: ' + case)
        for key in ('sample_period', 'cooling_samples', 'hhh_interval_ms'):
            positive(app['all_local'][key], case + ' tracked all-local ' + key)
        for point in app['points']:
            cfg = point['configuration']
            if not 0 < cfg['minimum_local_mib'] <= memory:
                raise ValueError('Local floor outside VM capacity: ' + case)
            if cfg['minimum_local_mib'] + cfg['pre_reclaim_headroom_mib'] > memory:
                raise ValueError('Pre-reclaim target outside VM capacity: ' + case)
            if any(type(value) is not int or value < 0 for value in cfg.values()):
                raise ValueError('Nonintegral/negative knob: ' + case)
    return points


def run_name(case, role, repetition):
    return f'{case}-{role}-r{repetition:02d}'


def schedule(config, apps):
    jobs = []
    for case in apps:
        app = config['applications'][case]
        for role in ROLES:
            knobs = app['all_local'] if role == 'all_local' else next(p['configuration'] for p in app['points'] if p['name'] == role)
            for repetition in range(1, 4):
                jobs.append({'application': case, 'point': role, 'repeat': repetition,
                             'name': run_name(case, role, repetition),
                             'vm_memory_mib': app['vm_memory_mib'], 'configuration': copy.deepcopy(knobs)})
    return jobs


def single_cpu_plan(app, inventory, topology=None, allowed=None):
    """Choose unique physical cores for VM vCPUs, emulator and Host generator."""
    import chameleon_affinity as affinity
    topology = affinity.topology() if topology is None else topology
    allowed = set(os.sched_getaffinity(0) if allowed is None else allowed)
    unique = {}
    for row in sorted(topology, key=lambda item: item['cpu']):
        if row['cpu'] in allowed:
            unique.setdefault((row['socket'], row['core']), row)
    pools = {}
    for row in unique.values():
        pools.setdefault(row['node'], []).append(row['cpu'])
    host_node = inventory.get('single_host_numa_node', 0)
    client_node = inventory['client_numa_node']
    vcpus = app['workload_configuration']['vcpus']
    case = app['application']
    args = app['workload_configuration']['args']
    def arg(key, default):
        return int(args[args.index(key) + 1]) if key in args else default
    client_count = (arg('--workers', 8) + arg('--rx-threads', 2) + arg('--producer-shards', 2)
                    if case == 'memcached' else arg('--threads', 16) if case == 'cassandra' else 0)
    if len(pools.get(client_node, [])) < client_count:
        raise ValueError('Insufficient physical Host client cores')
    clients = pools[client_node][-client_count:] if client_count else []
    available = allowed - set(clients)
    profile = affinity.select_cores(topology, available, host_node, vcpus)
    groups = {'vcpu': profile['vcpu_host_cpus'], 'qemu_service': profile['qemu_service_cpus']}
    if clients:
        groups['host_generator'] = clients
    physical = affinity.validate_isolation(groups, topology)
    return {'profile': profile, 'client_cpus': clients, 'client_numa_node': client_node,
            'physical_isolation': physical}


def leaf_command(job, app, slot, cpu, output, timeout):
    output = Path(output)
    directory = output / 'runs' / job['name']
    command = [sys.executable, str(SCRIPTS / 'run-chameleon-apps.py'),
               '--name', job['name'], '--results-root', str(output / 'raw'),
               '--vm', slot['name'], '--cases', job['application'],
               '--mode', 'all-local' if job['point'] == 'all_local' else 'chameleon',
               '--workload-config', str(directory / 'workload.json'),
               '--memory-plan', str(directory / 'memory.json'),
               '--cpu-affinity-profile', str(directory / 'affinity.json'), '--cpu-pinning',
               '--all-local-tracking', '--tracking-components', 'both',
               '--performance-mode', '--sample-seconds', '1', '--timeout', str(timeout),
               '--client-numa-node', str(cpu['client_numa_node']),
               '--rdma-interface', slot['rdma_interface']]
    if cpu['client_cpus']:
        command += ['--client-cpus', ','.join(map(str, cpu['client_cpus']))]
    for key, value in job['configuration'].items():
        command += ['--' + key.replace('_', '-'), str(value)]
    return command


def extract_run(directory, case):
    """Read a complete real leaf result. Counter increases remain raw evidence."""
    directory = Path(directory)
    report = json.loads((directory / 'report.json').read_text())
    row = report['cases'][case]
    if report.get('status') != 'PASS' or row.get('status') != 'PASS' or row.get('exit_code') != 0:
        raise ValueError('Application/infrastructure execution did not complete: ' + str(directory))
    summary = row['summary']
    if not summary.get('window_complete'):
        raise ValueError('Incomplete measurement window: ' + str(directory))
    reclaim = summary['mean_reclaim_percent']
    if not isinstance(reclaim, (int, float)) or not math.isfinite(reclaim):
        raise ValueError('Missing/nonfinite measured reclamation ratio')
    metrics = load_module('ae_metrics', SCRIPTS / 'chameleon-tuning-metrics.py')
    perf = metrics.performance(case, directory / case)
    positive(perf.get('cost'), case + ' cost')
    if case in ('memcached', 'cassandra'):
        positive(perf.get('throughput_ops'), case + ' throughput')
        positive(perf.get('p95_us'), case + ' P95')
    return {'status': 'PASS', 'application': case, 'report': str(directory / 'report.json'),
            'performance': perf, 'reclamation_pct': reclaim,
            'vm_memory_mib': summary['configured_memory_bytes'] // 2**20,
            'counters': row.get('counter_delta', {}), 'tracking': report.get('settings', {}),
            'checks': row.get('checks', {}), 'physical_rdma': report.get('physical_rdma'),
            'configuration': report.get('configuration', {})}


def mean_optional(rows, key):
    values = [row['performance'].get(key) for row in rows]
    if all(value is None for value in values):
        return None
    return statistics.mean(positive(value, key) for value in values)


def summarize(results, apps=None):
    results = Path(results).resolve()
    manifest = json.loads((results / 'manifest.json').read_text())
    config = manifest['points_configuration']
    apps = manifest['applications'] if apps is None else apps
    result = {'schema_version': 1, 'figure': 'fig78', 'status': 'PASS', 'repeats': 3,
              'normalization': 'fixed mean of three newly measured tracked all-local executions',
              'applications': {}, 'errors': []}
    for case in apps:
        app = config['applications'][case]
        try:
            rows = {role: [] for role in ROLES}
            for role in ROLES:
                for repetition in range(1, 4):
                    name = run_name(case, role, repetition)
                    execution = json.loads((results / 'runs' / name / 'execution.json').read_text())
                    if execution.get('status') != 'PASS' or execution.get('cleanup_errors'):
                        raise ValueError('Incomplete run/cleanup: ' + name)
                    row = extract_run(results / 'raw' / name, case)
                    if row['vm_memory_mib'] != app['vm_memory_mib']:
                        raise ValueError('VM capacity differs across fixed-capacity curve')
                    if role == 'all_local':
                        checks = row['checks']
                        if not checks.get('all_local_tracking_active') or not checks.get('tracking_parameters_match'):
                            raise ValueError('All-local PEBS+HHH readback is missing or disabled')
                        if not checks.get('no_reclamation'):
                            raise ValueError('All-local run performed reclamation')
                    row.update(repeat=repetition, report=str(Path('raw') / name / 'report.json'))
                    rows[role].append(row)
            denominator = {'mean_cost': mean_optional(rows['all_local'], 'cost'),
                           'mean_runtime_seconds': mean_optional(rows['all_local'], 'runtime_seconds'),
                           'mean_throughput_ops_sec': mean_optional(rows['all_local'], 'throughput_ops'),
                           'mean_p95_us': mean_optional(rows['all_local'], 'p95_us'),
                           'sample_period': app['all_local']['sample_period'],
                           'cooling_samples': app['all_local']['cooling_samples'],
                           'hhh_interval_ms': app['all_local']['hhh_interval_ms']}
            points = []
            for role in ROLES:
                for row in rows[role]:
                    row['slowdown_pct'] = 100 * (row['performance']['cost'] / denominator['mean_cost'] - 1)
                    if denominator['mean_p95_us'] is not None:
                        row['p95_slowdown_pct'] = 100 * (row['performance']['p95_us'] / denominator['mean_p95_us'] - 1)
                origin = role == 'all_local'
                points.append({'name': role, 'reclamation_pct': 0.0 if origin else statistics.mean(r['reclamation_pct'] for r in rows[role]),
                               'slowdown_pct': 0.0 if origin else statistics.mean(r['slowdown_pct'] for r in rows[role]),
                               'p95_slowdown_pct': (0.0 if origin else statistics.mean(r['p95_slowdown_pct'] for r in rows[role])) if denominator['mean_p95_us'] is not None else None,
                               'configuration': app['all_local'] if origin else next(p['configuration'] for p in app['points'] if p['name'] == role),
                               'runs': rows[role]})
            result['applications'][case] = {'display_name': app['display_name'], 'vm_memory_mib': app['vm_memory_mib'],
                                             'all_local': denominator, 'points': points}
        except (OSError, ValueError, KeyError) as error:
            result['errors'].append({'application': case, 'error': str(error)})
            result['status'] = 'INCOMPLETE'
    save(results / 'summary.json', result)
    save(results / 'all-local.json', {
        'schema_version': 1, 'figure': 'fig78', 'status': result['status'], 'repeats': 3,
        'normalization': result['normalization'],
        'applications': {case: {key: app[key] for key in ('vm_memory_mib', 'all_local')}
                         for case, app in result['applications'].items()},
        'errors': result['errors']})
    return result


def start_guard(rt, access, output, timeout):
    if not Path('/run/numad.pid').exists():
        return None, None, None
    stop = output / 'STOP_GUARD'
    stream = (output / 'numad-guard.log').open('w')
    prefix = [] if os.geteuid() == 0 else ['sudo', '-n']
    guard = subprocess.Popen([*prefix, sys.executable, str(SCRIPTS / 'guard-qemu-numad.py'),
                             '--run-dir', str(rt.guest.run_dir(access)), '--stop-file', str(stop),
                             '--duration', str(timeout + 3600)], stdout=stream, stderr=subprocess.STDOUT)
    deadline = time.monotonic() + 15
    while 'READY' not in Path(stream.name).read_text():
        if guard.poll() is not None or time.monotonic() > deadline:
            stop.write_text('guard setup failed\n')
            rt.stop_process(guard)
            stream.close()
            raise RuntimeError('NUMA guard did not start; see ' + str(output))
        time.sleep(.1)
    return guard, stop, stream


def run_one(rt, job, app, slot, inventory, template, cpu, results, timeout, servers):
    """Boot, connect, execute, disconnect and stop one isolated Guest."""
    output = results / 'runs' / job['name']
    output.mkdir(parents=True, exist_ok=False)
    access = rt.access(slot['name'])
    if rt.guest.active(access) or not rt.guest.launcher_idle(access):
        raise ValueError('Owned experiment slot is already running: ' + slot['name'])
    cfgpath = Path(access['vm_config'])
    original = cfgpath.read_text()
    guard = stop = guard_log = child = None
    boot_owned = False
    execution = {'status': 'RUNNING', **job, 'cpu': cpu, 'started_unix_seconds': time.time()}
    save(output / 'execution.json', execution)
    try:
        config = rt.slot_config(template, slot, app, cpu['profile'])
        with rt.guest.control_lock(access):
            save(cfgpath, config)
            guard, stop, guard_log = start_guard(rt, access, output, timeout)
            boot_owned = True
            rt.guest.start(access, timeout=300)
            execution['hermit_connection'] = rt.setup_guest(access, slot, inventory['server'])
            execution['pin_at_boot'] = rt.pin_guest_cpus(access, cpu['profile'])
        save(output / 'affinity.json', cpu['profile'])
        save(output / 'workload.json', {'applications': {job['application']: app['workload_configuration']}})
        save(output / 'memory.json', {'case': job['application'], 'memory_mib': app['vm_memory_mib'],
              'workload_args': app['workload_configuration']['args'],
              'workload_service_args': app['workload_configuration'].get('service_args', []),
              'allocation_mode': 'frozen-ae-curve'})
        command = leaf_command(job, app, slot, cpu, results, timeout)
        execution['command'] = command
        save(output / 'execution.json', execution)
        with (output / 'application.log').open('w') as log, (output / 'cpu-samples.jsonl').open('w') as samples:
            child = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            deadline = time.monotonic() + timeout + 1800
            while child.poll() is None:
                rt.assert_servers(servers)
                pid = int((rt.guest.run_dir(access) / 'qemu.pid').read_text())
                observation = rt.affinity.verify(pid, rt.guest.qmp(access, 'query-cpus-fast'), cpu['profile'])
                samples.write(json.dumps({'unix_seconds': time.time(), **observation}) + '\n')
                samples.flush()
                if guard is not None and guard.poll() is not None:
                    raise RuntimeError('NUMA guard exited during execution')
                if time.monotonic() > deadline:
                    raise TimeoutError('Application/cleanup deadline exceeded')
                time.sleep(1)
        execution['returncode'] = child.returncode
        if child.returncode:
            raise RuntimeError('Application failed; see ' + str(output / 'application.log'))
        extracted = extract_run(results / 'raw' / job['name'], job['application'])
        execution['performance'] = extracted['performance']
        execution['status'] = 'PASS'
    except BaseException as error:
        execution.update(status='CANCELLED' if isinstance(error, KeyboardInterrupt) else 'FAIL', error=str(error))
        raise
    finally:
        errors = []
        if child is not None and child.poll() is None:
            child.send_signal(signal.SIGINT)
            try:
                child.wait(timeout=360)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
                errors.append('Application cleanup timed out')
        if boot_owned:
            try:
                diagnostic = rt.guest.run_dir(access) / 'hermit-connection.json'
                if diagnostic.is_file():
                    (output / 'hermit-connection.json').write_text(diagnostic.read_text())
                with rt.guest.control_lock(access):
                    if rt.guest.active(access):
                        rt.stop_guest(access, timeout=180)
            except Exception as error:
                errors.append(str(error))
        cfgpath.write_text(original)
        if stop is not None:
            stop.write_text('done\n')
        if guard is not None:
            try:
                guard.wait(timeout=20)
            except subprocess.TimeoutExpired:
                rt.stop_process(guard)
                errors.append('NUMA guard cleanup timed out')
        if guard_log is not None:
            guard_log.close()
        execution['cleanup_errors'] = errors
        if errors:
            execution['status'] = 'FAIL'
        execution['finished_unix_seconds'] = time.time()
        save(output / 'execution.json', execution)
    if execution['status'] != 'PASS':
        raise RuntimeError('Run cleanup failed: ' + job['name'])
    print('FINISHED ' + job['name'] + ' ' + json.dumps(execution['performance']), flush=True)
    return execution


def run(config, inventory, apps, results, timeout):
    import chameleon_fig9_runtime as rt
    rt.validate_inventory(inventory, hardware=True)
    template_access = rt.access(inventory['template_vm'])
    # This same lock is held by Figure 9 and guestctl. Keep the immutable
    # backing VM stopped throughout the experiment, including pool cleanup.
    with rt.guest.control_lock(template_access):
        return run_locked(rt, config, inventory, apps, results, timeout, template_access)


def run_locked(rt, config, inventory, apps, results, timeout, template_access):
    slot = copy.deepcopy(inventory.get('single_slot', inventory['slots'][0]))
    single = dict(inventory, slots=[slot])
    if rt.guest.active(template_access) or not rt.guest.launcher_idle(template_access):
        raise ValueError('Template must remain stopped while overlays exist')
    source = json.loads(Path(template_access['vm_config']).read_text())
    placements = {case: single_cpu_plan(dict(config['applications'][case], application=case), inventory) for case in apps}
    first = config['applications'][apps[0]]
    rt.prepare_slots(single, template_access, [rt.slot_config(source, slot, first, placements[apps[0]]['profile'])])
    results.mkdir(parents=True, exist_ok=False)
    manifest = {'schema_version': 1, 'figure': 'fig78', 'repeats': 3, 'applications': apps,
                'points_configuration': config, 'inventory': single, 'cpu_placements': placements,
                'jobs': schedule(config, apps), 'started_unix_seconds': time.time(), 'status': 'RUNNING'}
    save(results / 'manifest.json', manifest)
    try:
        with rt.servers(single, results) as servers:
            for job in manifest['jobs']:
                app = dict(config['applications'][job['application']], application=job['application'])
                print('START ' + job['name'], flush=True)
                run_one(rt, job, app, slot, single, source, placements[job['application']], results, timeout, servers)
        manifest['status'] = 'PASS'
    except BaseException as error:
        manifest.update(status='CANCELLED' if isinstance(error, KeyboardInterrupt) else 'FAIL', error=str(error))
        raise
    finally:
        manifest['finished_unix_seconds'] = time.time()
        save(results / 'manifest.json', manifest)
    return summarize(results)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--points', type=Path, default=DEFAULT_POINTS)
    parser.add_argument('--inventory', type=Path, default=ROOT / 'ae/config/host.json')
    parser.add_argument('--results', type=Path, default=ROOT / 'ae/results/fig78')
    parser.add_argument('--apps', nargs='+', help='Subset of application keys in the fixed configuration')
    parser.add_argument('--timeout', type=int, default=14400)
    parser.add_argument('--repeats', type=int, choices=[3], default=3,
                        help='AE protocol uses exactly three observations per point')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--plan', action='store_true')
    mode.add_argument('--run', action='store_true')
    mode.add_argument('--parse-only', action='store_true')
    args = parser.parse_args(argv)
    def draw(result):
        if result['status'] == 'PASS':
            subprocess.run([sys.executable, str(ROOT / 'ae/scripts/plot_figures.py'),
                            '--figure', 'fig78', '--input', str(args.results / 'summary.json'),
                            '--output-dir', str(args.results)], check=True)
    if args.parse_only:
        result = summarize(args.results, args.apps)
        draw(result)
        print(json.dumps({'status': result['status'], 'summary': str(args.results / 'summary.json'), 'errors': result['errors']}, indent=2))
        return 0 if result['status'] == 'PASS' else 1
    config = load_points(args.points)
    apps = args.apps or list(config['applications'])
    if len(apps) != len(set(apps)) or any(case not in config['applications'] for case in apps):
        parser.error('Choose unique applications from: ' + ', '.join(config['applications']))
    if not args.run:
        print(json.dumps({'figure': 'fig78', 'repeats': 3, 'results': str(args.results.resolve()),
                          'applications': apps, 'jobs': schedule(config, apps)}, indent=2))
        return 0
    if args.results.exists():
        parser.error('Results directory exists; choose a new --results path or use --parse-only')
    inventory = json.loads(args.inventory.read_text())
    lockpath = ROOT / 'hyperalloc-6.18/build/ae-experiment.lock'
    lockpath.parent.mkdir(parents=True, exist_ok=True)
    with lockpath.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        def stop(signum, frame):
            raise KeyboardInterrupt('signal ' + str(signum))
        previous = signal.signal(signal.SIGTERM, stop)
        try:
            result = run(config, inventory, apps, args.results.resolve(), args.timeout)
        finally:
            signal.signal(signal.SIGTERM, previous)
    draw(result)
    return 0 if result['status'] == 'PASS' else 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        print('Figure 7/8 stopped: ' + str(error), file=sys.stderr)
        sys.exit(1)
