import json
import os
from pathlib import Path
import signal
import subprocess
import time

from chameleon_mix.common import ROOT, load
from chameleon_mix.runtime import rt, engine, leaf_command


def memory_observation(access, pin):
    pid = pin['pid']
    return {'pid': pid, 'nodes': rt.numa_memory_snapshot(),
            'numa_maps': Path(f'/proc/{pid}/numa_maps').read_text(),
            'command': json.loads((rt.guest.run_dir(access) / 'command.json').read_text())}


def boot_order(spec, inventory):
    return sorted(zip(inventory['slots'], spec['applications']),
                  key=lambda pair: -len(spec['cpus']['applications'][pair[1]['application']]['host_memory_nodes']))


def start_applications(commands, output, record, children, logs):
    for case, command in commands.items():
        log = (output / (case + '.log')).open('w')
        logs.append(log)
        child = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                 cwd=ROOT, start_new_session=True)
        children[case] = child
        record.setdefault('launches', {})[case] = {'pid': child.pid, 'unix_seconds': time.time()}


def run_mix(mix, repetition, spec, inventory, source, directory, name, timeout,
            server_processes=(), results_root=None, performance_mode=True):
    output = directory / mix / f'repeat-{repetition:02d}'
    output.mkdir(parents=True)
    cpus = spec['cpus']
    record = {'status': 'RUNNING', 'mix': mix, 'repetition': repetition,
              'execution_mode': 'three-concurrent', 'simultaneous_vms': 3,
              'started_unix_seconds': time.time(), 'applications': [], 'commands': {},
              'leaf_directories': {}, 'cleanup_errors': []}
    started, originals, children, logs, guests = [], {}, {}, [], {}
    rows = rt.affinity.topology()
    last_audit = 0.0
    def audit(force=False):
        nonlocal last_audit
        if not force and time.monotonic() - last_audit < 1:
            return
        observation = rt.verify_vm_cpu_isolation(cpus, guests, rows)
        last_audit = time.monotonic()
        with (output / 'cpu-isolation-samples.jsonl').open('a') as stream:
            stream.write(json.dumps({'unix_seconds': time.time(), **observation}) + '\n')
        record['cpu_isolation'] = observation
    rt.save(output / 'report.json', record)
    try:
        ordered = boot_order(spec, inventory)
        for index, (slot, high) in enumerate(ordered):
            rt.assert_servers(server_processes)
            record.setdefault('memory_before_boot', {})[high['application']] = rt.check_memory_available(
                [app for _, app in ordered[index:]], cpus, inventory)
            access = rt.access(slot['name'])
            originals[access['name']] = Path(access['vm_config']).read_text()
            profile = cpus['applications'][high['application']]
            with rt.guest.control_lock(access):
                rt.save(Path(access['vm_config']), rt.slot_config(source, slot, high, profile))
                started.append(access)
                rt.guest.start(access, timeout=300)
                connection = rt.setup_guest(access, slot, inventory['server'])
                rt.save(output / (slot['name'] + '-hermit-connection.json'), connection)
                record.setdefault('pin_at_boot', {})[high['application']] = rt.pin_guest_cpus(access, profile)
                rt.save(output / (slot['name'] + '-numa.json'),
                        memory_observation(access, record['pin_at_boot'][high['application']]))
            case = high['application']
            guests[case] = access
            prefix = output / case
            affinity_file = prefix.with_suffix('.affinity.json')
            work_file = prefix.with_suffix('.workload.json')
            memory_file = prefix.with_suffix('.memory.json')
            rt.save(affinity_file, profile)
            rt.save(work_file, {'applications': {case: high['workload_configuration']}})
            rt.save(memory_file, {'case': case, 'memory_mib': high['vm_memory_mib'],
                'workload_args': high['workload_configuration']['args'],
                'workload_service_args': high['workload_configuration'].get('service_args', []),
                'allocation_mode': 'saved-high-point'})
            leaf_name = f'{name}-{mix}-r{repetition}-{case}'
            record['commands'][case] = leaf_command(leaf_name, slot, high, affinity_file,
                                                    work_file, memory_file, cpus, timeout, results_root)
            record['leaf_directories'][case] = str(Path(results_root) / leaf_name)
        audit(force=True)
        start_applications(record['commands'], output, record, children, logs)
        rt.save(output / 'report.json', record)
        deadline = time.monotonic() + timeout + 1800
        while True:
            rt.assert_servers(server_processes)
            audit()
            codes = {case: child.poll() for case, child in children.items()}
            failed = {case: code for case, code in codes.items() if code not in (None, 0)}
            if failed:
                raise RuntimeError('Application failed: ' + str(failed))
            if all(code == 0 for code in codes.values()):
                break
            if time.monotonic() > deadline:
                raise TimeoutError('Mix application/cleanup deadline exceeded')
            time.sleep(1)
        audit(force=True)
        ae = load('concurrent_metrics', ROOT / 'ae/scripts/ae_fig78.py')
        for high in spec['applications']:
            case = high['application']
            leaf = Path(record['leaf_directories'][case])
            measured = ae.extract_run(leaf, case)
            values = engine.data.evaluate_application(case, leaf / case, high)
            values.update(status='PASS', report=measured['report'], checks=measured['checks'],
                          reclaim_percent=measured['reclamation_pct'])
            record['applications'].append(values)
            if case in ('memcached', 'cassandra'):
                filenames = ['generator-affinity.json'] if case == 'memcached' else ['load-affinity.json', 'run-affinity.json']
                for filename in filenames:
                    found = list((leaf / case / 'application').rglob(filename))
                    if len(found) != 1:
                        raise ValueError('Missing/ambiguous client affinity evidence: ' + case + '/' + filename)
                    evidence = json.loads(found[0].read_text())
                    if (evidence.get('status') != 'PASS' or evidence.get('allowed_cpus') != cpus['client_cpus']
                            or not evidence.get('observed_pids')):
                        raise ValueError('Client CPU affinity check failed: ' + case)
                    record.setdefault('host_client_affinity', {}).setdefault(case, {})[filename] = evidence
        record.update(engine.data.aggregate_mix(mix, record['applications']))
    except BaseException as error:
        record.update(status='CANCELLED' if isinstance(error, KeyboardInterrupt) else 'FAIL', error=repr(error))
        raise
    finally:
        errors = []
        for child in children.values():
            if child.poll() is None:
                child.send_signal(signal.SIGINT)
        for case, child in children.items():
            try:
                child.wait(timeout=360)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL); child.wait()
                errors.append(case + ': application cleanup timed out')
        for access in reversed(started):
            try:
                with rt.guest.control_lock(access):
                    rt.stop_guest(access, timeout=180)
            except Exception as error:
                errors.append(access['name'] + ': ' + repr(error))
            finally:
                Path(access['vm_config']).write_text(originals[access['name']])
        for log in logs:
            log.close()
        record['cleanup_errors'] = errors
        if errors:
            record['status'] = 'FAIL'
        record['finished_unix_seconds'] = time.time()
        rt.save(output / 'report.json', record)
    return record
