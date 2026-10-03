#!/usr/bin/env python3
"""Run Mix1–4 with independent QEMU Guests and saved high-point knobs.

Default is a read-only plan. Each repetition owns its Guests and measurements.
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
    plans, errors = {}, []
    snapshot = rt.numa_memory_snapshot()
    for mix in mixes:
        apps = [data.resolve_high(case, qualified) for case in data.MIXES[mix]]
        if any(a['remote_pool_mib'] != 8192 for a in apps):
            raise ValueError('Current Fig9 runner expects one 8192MiB RDMA pool per VM')
        try:
            resources = rt.plan_mix_resources(mix, apps, inventory, snapshot)
        except ValueError as error:
            errors.append(mix + ': ' + str(error))
            continue
        plans[mix] = {'applications': apps, **resources,
                      'total_vm_memory_mib': sum(a['vm_memory_mib'] for a in apps)}
    if errors:
        raise ValueError('No feasible whole-VM placement:\n' + '\n'.join(errors))
    return {'protocol': 'fig9-phased-independent-vms-saved-high-v2', 'mixes': plans,
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
            '--rdma-interface', slot['rdma_interface']]
    if placement['client_cpus']:
        argv += ['--client-cpus', ','.join(map(str, placement['client_cpus']))]
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
        if children and len(found) == len(children):
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
        raise ValueError('No common post-barrier workload interval among phase VMs')
    return {'common_seconds': seconds, 'start_unix_seconds': max(starts),
            'end_unix_seconds': min(ends),
            'per_application_post_release_seconds': [e - s for s, e in zip(starts, ends)],
            'simultaneous_vms': len(records),
            'scope': 'Client READ/generator intervals and batch launcher intervals within this phase; shorter applications finish first.'}


def phase_slots(phase, inventory):
    indices = phase.get('slot_indices', list(range(len(phase['applications']))))
    return [(inventory['slots'][i], high) for i, high in zip(indices, phase['applications'])]


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
    for mix in plan['mixes'].values():
        allocations = mix.get('residency', mix['phases'])
        for phase in allocations:
            rt.check_memory_available(phase['applications'], phase['cpus'], inventory, local_pools_pending=True)
            for slot, high in phase_slots(phase, inventory):
                cpu = phase['cpus']['applications'][high['application']]
                config = rt.slot_config(source, slot, high, cpu)
                # Validate the source disk until the independent overlay exists.
                config['disk'] = source['disk']; config['disk_format'] = source['disk_format']
                rt.deploy.preflight(rt.deploy.config(config))
    return source


def run_phase(mix, repetition, spec, inventory, source, directory, name, timeout, server_processes=(),
              results_root=None, performance_mode=False, output=None):
    output = output if output is not None else directory / mix / f'repeat-{repetition:02d}'
    output.mkdir(parents=True)
    groups = spec.get('workload_groups', [[a['application'] for a in spec['applications']]])
    grouped = 'workload_groups' in spec
    if sorted(case for members in groups for case in members) != sorted(a['application'] for a in spec['applications']):
        raise ValueError('Workload groups must contain each application exactly once')
    barriers = [(output / f'phase-{i + 1:02d}' if grouped else output) / 'barrier'
                for i in range(len(groups))]
    for barrier in barriers:
        barrier.mkdir(parents=True)
    case_barriers = {case: barriers[i] for i, members in enumerate(groups) for case in members}
    leaves, logs, guards, started, original = {}, [], [], [], {}
    stop_guard = output / 'STOP_GUARDS'
    guest_guards = {}
    residencies = spec.get('residency', [spec])
    placement = residencies[0]['cpus']
    record = {'status': 'RUNNING', 'applications': [], 'commands': {}, 'mix': mix,
              'repetition': repetition, 'started_unix_seconds': time.time()}
    if grouped:
        record.update(execution_mode=spec['execution_mode'], vm_lifecycle=spec['vm_lifecycle'],
                      simultaneous_vms=spec['simultaneous_vms'], simultaneous_applications=max(map(len, groups)),
                      phases=[])
    cpu_guests = {}
    cpu_topology = rt.affinity.topology()
    cpu_last_check = 0.0
    def audit_cpus(force=False):
        nonlocal cpu_last_check
        if not force and time.monotonic() - cpu_last_check < 1:
            return
        observed = rt.verify_vm_cpu_isolation(placement, cpu_guests, cpu_topology)
        cpu_last_check = time.monotonic()
        with (output / 'cpu-isolation-samples.jsonl').open('a') as stream:
            stream.write(json.dumps({'unix_seconds': time.time(), **observed}) + '\n')
        previous = record.get('cpu_isolation', {})
        record['cpu_isolation'] = {'status': 'PASS', 'sample_count': previous.get('sample_count', 0) + 1,
                                   'latest': observed, 'samples_file': str(output / 'cpu-isolation-samples.jsonl')}
    def boot_residency(resident):
        pending = [(slot, high) for slot, high in phase_slots(resident, inventory)
                   if high['application'] not in cpu_guests]
        for index, (slot, high) in enumerate(pending):
            rt.assert_servers(server_processes)
            # Pools and earlier VMs already consume RAM. Check the remaining
            # boot demand again so pressure cannot become a QMP startup timeout.
            memory = rt.check_memory_available([high for _, high in pending[index:]], placement, inventory)
            record.setdefault('memory_before_boot', {})[high['application']] = memory
            rt.save(output / 'report.json', record)
            a = rt.access(slot['name'])
            original[a['name']] = Path(a['vm_config']).read_text()
            cpu = placement['applications'][high['application']]
            config = rt.slot_config(source, slot, high, cpu)
            with rt.guest.control_lock(a):
                rt.save(Path(a['vm_config']), config)
                if Path('/run/numad.pid').exists():
                    stream = (output / (a['name'] + '-numad.log')).open('w'); logs.append(stream)
                    prefix = [] if os.geteuid() == 0 else ['sudo', '-n']
                    guest_stop = output / (a['name'] + '.STOP_GUARD')
                    guard = subprocess.Popen([*prefix, sys.executable, str(HERE / 'guard-qemu-numad.py'),
                         '--run-dir', str(rt.guest.run_dir(a)), '--stop-file', str(guest_stop),
                         '--duration', str(len(groups) * (timeout + 600) + 3600)], stdout=stream, stderr=subprocess.STDOUT)
                    guards.append(guard)
                    guest_guards[a['name']] = (guard, guest_stop)
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
                                   memory_file, case_barriers[case], placement, timeout, results_root, performance_mode)
            record['commands'][case] = command
            record.setdefault('leaf_directories', {})[case] = str((Path(results_root) if results_root else rt.ROOT / 'benchmarks/results/chameleon') / leaf_name)
    rt.save(output / 'report.json', record)
    try:
        boot_residency(residencies[0])
        audit_cpus(force=True)
        for index, members in enumerate(groups):
            if spec.get('vm_lifecycle') == 'overlapping-pairs' and index == 2:
                following = residencies[1]
                stopped = spec['applications'][following['stopped_slot_index']]['application']
                retained = spec['applications'][following['retained_slot_index']]['application']
                introduced = spec['applications'][following['started_slot_index']]['application']
                old = cpu_guests[stopped]
                diagnostic = rt.guest.run_dir(old) / 'hermit-connection.json'
                if diagnostic.is_file():
                    (output / (old['name'] + '-hermit-connection.json')).write_text(diagnostic.read_text())
                with rt.guest.control_lock(old):
                    rt.stop_guest(old, timeout=180)
                started.remove(old)
                del cpu_guests[stopped]
                if old['name'] in guest_guards:
                    guard, guest_stop = guest_guards[old['name']]
                    guest_stop.write_text('done\n')
                    guard.wait(timeout=20)
                    guards.remove(guard)
                record['handoff'] = {'stopped_application': stopped, 'retained_application': retained,
                                     'started_application': introduced, 'stop_completed_unix_seconds': time.time()}
                rt.save(output / 'report.json', record)
                placement = following['cpus']
                record['retained_cpu_affinity'] = rt.pin_guest_cpus(cpu_guests[retained], placement['applications'][retained])
                boot_residency(following)
                audit_cpus(force=True)
                record['handoff']['boot_completed_unix_seconds'] = time.time()
            phase = {'status': 'RUNNING', 'applications': [], 'cleanup_errors': [],
                     'started_unix_seconds': time.time(), 'phase': index + 1}
            if grouped:
                record.setdefault('phases', []).append(phase)
            current_barrier = barriers[index]
            children = {}
            for case in members:
                stream = (output / (case + '.log')).open('w'); logs.append(stream)
                children[case] = leaves[case] = subprocess.Popen(
                    record['commands'][case], stdout=stream, stderr=subprocess.STDOUT,
                    cwd=rt.ROOT, start_new_session=True)
            phase['barrier'] = wait_ready(current_barrier, children, timeout,
                                          server_processes=server_processes, cpu_check=audit_cpus)
            if not grouped:
                record['barrier'] = phase['barrier']
            rt.save(output / 'report.json', record)
            deadline = time.monotonic() + timeout + 600
            while True:
                rt.assert_servers(server_processes)
                audit_cpus()
                codes = {case: child.poll() for case, child in children.items()}
                failed = {case: code for case, code in codes.items() if code not in (None, 0)}
                if failed:
                    raise RuntimeError('Application failed: ' + str(failed))
                if any(guard.poll() is not None for guard in guards):
                    raise RuntimeError('A VM NUMA guard exited during measurement')
                if all(code == 0 for code in codes.values()):
                    break
                if time.monotonic() > deadline:
                    raise TimeoutError('Mix workload/cleanup exceeded timeout')
                time.sleep(1)
            for child in children.values():
                child.wait(timeout=360)
            audit_cpus(force=True)
            records = []
            first_result = len(record['applications'])
            metrics = rt.module('fig9_metrics', HERE / 'chameleon-tuning-metrics.py')
            for high in spec['applications']:
                if high['application'] not in members:
                    continue
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
                        if evidence.get('status') != 'PASS' or evidence.get('allowed_cpus') != placement['client_cpus'] or not evidence.get('observed_pids'):
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
            phase['applications'] = record['applications'][first_result:]
            phase['overlap'] = overlap(records, phase['barrier']['released_unix_seconds'])
            phase.update(status='PASS', finished_unix_seconds=time.time())
            if grouped:
                rt.save(current_barrier.parent / 'report.json', phase)
            else:
                record['overlap'] = phase['overlap']
            rt.save(output / 'report.json', record)
        record['status'] = 'PASS'
    except BaseException as error:
        record.update(status='CANCELLED' if isinstance(error, KeyboardInterrupt) else 'FAIL', error=repr(error))
        if grouped and record['phases'] and record['phases'][-1]['status'] == 'RUNNING':
            record['phases'][-1].update(status=record['status'], error=repr(error), finished_unix_seconds=time.time())
            rt.save(barriers[len(record['phases']) - 1].parent / 'report.json', record['phases'][-1])
        raise
    finally:
        for barrier in barriers:
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
                    rt.stop_guest(a, timeout=180)
            except Exception as error:
                cleanup_errors.append(a['name'] + ': ' + repr(error))
        for slot in inventory['slots']:
            if slot['name'] in original:
                Path(rt.access(slot['name'])['vm_config']).write_text(original[slot['name']])
        stop_guard.write_text('done\n')
        for _, guest_stop in guest_guards.values():
            guest_stop.write_text('done\n')
        for guard in guards:
            try: guard.wait(timeout=20)
            except subprocess.TimeoutExpired:
                rt.stop_process(guard)
                cleanup_errors.append('NUMA guard did not stop')
        for stream in logs: stream.close()
        record['cleanup_errors'] = cleanup_errors
        if cleanup_errors: record['status'] = 'FAIL'
        record['finished_unix_seconds'] = time.time()
        rt.save(output / 'report.json', record)
    return record


def run_mix(mix, repetition, spec, inventory, source, directory, name, timeout, server_processes=(),
            results_root=None, performance_mode=False):
    mode = spec.get('execution_mode', 'three-concurrent')
    output = directory / mix / f'repeat-{repetition:02d}'
    if mode == 'three-concurrent':
        phase = spec.get('phases', [spec])[0]
        record = run_phase(mix, repetition, phase, inventory, source, directory, name, timeout,
                           server_processes, results_root, performance_mode)
        if record['status'] == 'PASS':
            record.update(data.aggregate_mix(mix, record['applications']))
        record.update(execution_mode=mode, simultaneous_vms=3)
        rt.save(output / 'report.json', record)
        return record
    sizes = {'two-then-one': [2, 1], 'sequential': [1, 1, 1]}.get(mode)
    if sizes is None or [len(p['applications']) for p in spec['phases']] != sizes:
        raise ValueError('Invalid phase layout for execution mode: ' + mode)
    if mode == 'sequential':
        order = [p['applications'][0]['application'] for p in spec['phases']]
        residencies = spec['residency']
        first, following = residencies
        retained, stopped, introduced = (following[k] for k in
                                         ('retained_slot_index', 'stopped_slot_index', 'started_slot_index'))
        if (sorted(order) != sorted(data.MIXES[mix]) or
                order[:2] != [a['application'] for a in first['applications']] or
                set(first['slot_indices']) != {retained, stopped} or
                set(following['slot_indices']) != {retained, introduced} or
                len({retained, stopped, introduced}) != 3 or
                order[2] != spec['applications'][introduced]['application']):
            raise ValueError('Invalid mix application order or VM residency')
        resident = {**spec, 'workload_groups': [[case] for case in order]}
        record = run_phase(mix, repetition, resident, inventory, source, directory, name, timeout,
                           server_processes, results_root, performance_mode)
        if record['status'] == 'PASS':
            data.execution_layout(record)
            record.update(data.aggregate_mix(mix, record['applications']))
        rt.save(output / 'report.json', record)
        return record
    output.mkdir(parents=True)
    record = {'mix': mix, 'repetition': repetition, 'status': 'RUNNING', 'applications': [],
              'execution_mode': mode, 'simultaneous_vms': max(sizes), 'phases': [],
              'started_unix_seconds': time.time()}
    rt.save(output / 'report.json', record)
    try:
        for index, phase in enumerate(spec['phases'], 1):
            phase_out = output / f'phase-{index:02d}'
            print(f'PHASE {mix} repeat-{repetition} {index}: ' +
                  ', '.join(a['application'] for a in phase['applications']), flush=True)
            try:
                row = run_phase(mix, repetition, phase, inventory, source, directory, name, timeout,
                                server_processes, results_root, performance_mode, output=phase_out)
            except BaseException:
                if (phase_out / 'report.json').exists():
                    record['phases'].append(json.loads((phase_out / 'report.json').read_text()))
                raise
            row['phase'] = index
            record['phases'].append(row)
            if row['status'] != 'PASS' or row.get('cleanup_errors'):
                raise RuntimeError('Phase failed or its VMs were not released; the next phase will not start')
            # run_phase returns only after client exit and VM shutdown/cleanup.
            record['applications'].extend(row['applications'])
            rt.save(output / 'report.json', record)
        data.execution_layout(record)
        record.update(data.aggregate_mix(mix, record['applications']))
    except BaseException as error:
        record.update(status='CANCELLED' if isinstance(error, KeyboardInterrupt) else 'FAIL', error=repr(error))
        raise
    finally:
        record['finished_unix_seconds'] = time.time()
        rt.save(output / 'report.json', record)
    return record


def run_campaign(plan, inventory, source, out, name, repeats, timeout,
                 results_root=None, performance_mode=False, configuration=None):
    """Attempt every repetition once, recovering owned resources after failures."""
    owned = [rt.access(slot['name']) for slot in inventory['slots']]
    if any(rt.guest.active(a) or not rt.guest.launcher_idle(a) for a in owned):
        raise ValueError('All experiment slots must be offline before the campaign')
    originals = {a['name']: Path(a['vm_config']).read_text() for a in owned}
    report = {'status': 'RUNNING', 'configuration': configuration or {}, 'mixes': {},
              'reference': plan['reference'], 'plan': str(out / 'plan.json')}
    rt.save(out / 'report.json', report)
    try:
        for mix, spec in plan['mixes'].items():
            group = report['mixes'][mix] = {'status': 'RUNNING', 'repetitions': []}
            for rep in range(1, repeats + 1):
                saved = out / mix / f'repeat-{rep:02d}' / 'report.json'
                try:
                    recovery = rt.recover_owned_guests(owned, originals)
                    if recovery['errors']:
                        raise RuntimeError('Resources still busy: ' + str(recovery['errors']))
                    pools = out / 'pools' / mix / f'repeat-{rep:02d}'
                    pools.mkdir(parents=True)
                    with rt.servers(inventory, pools) as server_processes:
                        row = run_mix(mix, rep, spec, inventory, source, out, name, timeout,
                                      server_processes, results_root, performance_mode)
                        if row['status'] != 'PASS' or row.get('cleanup_errors'):
                            raise RuntimeError('Mix execution or cleanup failed')
                except Exception as error:
                    recovery = rt.recover_owned_guests(owned, originals)
                    row = json.loads(saved.read_text()) if saved.exists() else {'mix': mix, 'repetition': rep}
                    row.update(status='FAIL', error=str(error), recovery=recovery)
                    saved.parent.mkdir(parents=True, exist_ok=True)
                    rt.save(saved, row)
                    print(f'FAILED {mix} repeat-{rep}; continuing: {error}', flush=True)
                group['repetitions'].append(row)
                rt.save(out / 'report.json', report)
            group['successful_repeats'] = sum(r['status'] == 'PASS' for r in group['repetitions'])
            group['status'] = 'PASS' if group['successful_repeats'] == repeats else 'PARTIAL' if group['successful_repeats'] else 'FAIL'
        report.update(status='PASS' if all(g['status'] == 'PASS' for g in report['mixes'].values()) else 'PARTIAL',
                      completed=True)
    except BaseException as error:
        report.update(status='CANCELLED' if isinstance(error, KeyboardInterrupt) else 'FAIL', error=repr(error))
        raise
    finally:
        rt.save(out / 'report.json', report)
    return report


def main():
    from chameleon_mix.run import main as concurrent_main
    return concurrent_main()


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('Figure 9 interrupted; cleanup recorded in the result directory.', file=sys.stderr)
        raise SystemExit(130)
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        print('fig9: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
