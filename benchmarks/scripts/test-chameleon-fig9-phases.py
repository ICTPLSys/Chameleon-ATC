#!/usr/bin/env python3
"""Phased execution tests. Synthetic timings/metrics are never AE measurements."""
import contextlib
import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import chameleon_fig9 as data
import chameleon_fig9_runtime as rt

ROOT = Path(__file__).resolve().parents[2]
runner = rt.module('phase_runner_test', Path(__file__).with_name('run-chameleon-fig9.py'))
sys.path.insert(0, str(ROOT / 'ae/scripts'))
import ae_fig9
import plot_figures


def topology():
    return [{'cpu': n * 24 + c, 'node': n, 'socket': n, 'core': c}
            for n in (0, 1) for c in range(24)]


def inventory():
    return json.loads((ROOT / 'ae/config/host.example.json').read_text())


def plan(mix, mode=None, config=None, capacity_gib=125):
    apps = [data.resolve_high(case, ROOT / 'ae/config/fig9-highs.json') for case in data.MIXES[mix]]
    config = copy.deepcopy(config if config is not None else inventory())
    if mode is not None:
        config.setdefault('fig9_execution', {})[mix] = mode
    resources = rt.plan_mix_resources(mix, apps, config,
                                      {n: {'total_mib': capacity_gib * 1024} for n in (0, 1)}, topology(), range(48))
    return {'applications': apps, **resources}


def measured_phase(apps, start, end):
    return {'status': 'PASS', 'cleanup_errors': [], 'started_unix_seconds': start, 'finished_unix_seconds': end,
            'applications': [{'application': case, 'status': 'PASS', 'slowdown_percent': 9.0} for case in apps]}


def phased_row(mix='mix4', mode='sequential'):
    groups = [[p['applications'][0]['application']] for p in plan(mix)['phases']] if mode == 'sequential' else [
        ['cassandra', 'xsbench'], ['graph500']]
    phases = [measured_phase(group, i * 4 + 1, i * 4 + 4) for i, group in enumerate(groups)]
    handoff = {}
    if mode == 'sequential':
        spec = plan(mix); transition = spec['residency'][1]
        handoff = {'stopped_application': spec['applications'][transition['stopped_slot_index']]['application'],
                   'retained_application': spec['applications'][transition['retained_slot_index']]['application'],
                   'started_application': spec['applications'][transition['started_slot_index']]['application'],
                   'stop_completed_unix_seconds': 8.1, 'boot_completed_unix_seconds': 8.9}
    return {'execution_mode': mode, 'phases': phases, 'repetition': 1,
            **({'vm_lifecycle': 'overlapping-pairs', 'simultaneous_applications': 1, 'handoff': handoff}
               if mode == 'sequential' else {}),
            **data.aggregate_mix(mix, [a for p in phases for a in p['applications']])}


class Planning(unittest.TestCase):
    def test_all_default_mixes_keep_two_feasible_vms_and_preserve_highs(self):
        for configured in (True, False):
            config = inventory()
            if not configured:
                config.pop('fig9_execution', None)
            for mix, cases in data.MIXES.items():
                with self.subTest(mix=mix, configured=configured):
                    spec = plan(mix, config=config)
                    self.assertEqual(spec['execution_mode'], 'sequential')
                    self.assertEqual(spec['vm_lifecycle'], 'overlapping-pairs')
                    self.assertEqual(spec['simultaneous_vms'], 2)
                    self.assertEqual(spec['simultaneous_applications'], 1)
                    self.assertEqual([len(p['applications']) for p in spec['phases']], [1, 1, 1])
                    self.assertEqual({p['applications'][0]['application'] for p in spec['phases']}, set(cases))
                    residency = spec['residency']
                    self.assertEqual([len(r['applications']) for r in residency], [2, 2])
                    app_sets = [{a['application'] for a in r['applications']} for r in residency]
                    self.assertEqual(app_sets[0] | app_sets[1], set(cases))
                    retained, = app_sets[0] & app_sets[1]
                    self.assertEqual(residency[0]['cpus']['applications'][retained]['host_numa_node'],
                                     residency[1]['cpus']['applications'][retained]['host_numa_node'])
                    self.assertEqual(spec['peak_vm_memory_mib'], max(
                        sum(a['vm_memory_mib'] for a in r['applications']) for r in residency))
                    for resident in residency:
                        self.assertEqual(set(resident['cpus']['applications']),
                                         {a['application'] for a in resident['applications']})
                        nodes = resident['numa_memory']['nodes'].values()
                        self.assertTrue(all(r['spare_mib'] >= 0 for r in nodes))
                        self.assertEqual(sum(r['vm_memory_mib'] for r in nodes),
                                         sum(a['vm_memory_mib'] for a in resident['applications']))
                        for i, app in zip(resident['slot_indices'], resident['applications']):
                            self.assertEqual(app, spec['applications'][i])
                            if app['application'] in ('cassandra', 'memcached'):
                                cpus = resident['cpus']
                                self.assertTrue(cpus['client_cpus'])
                                self.assertNotEqual(cpus['client_numa_node'],
                                                    cpus['applications'][app['application']]['host_numa_node'])
                    for index, phase in enumerate(spec['phases']):
                        resident = residency[0 if index < 2 else 1]
                        self.assertEqual(phase['cpus'], resident['cpus'])
                        self.assertEqual(phase['numa_memory'], resident['numa_memory'])
                        app = phase['applications'][0]
                        self.assertEqual(app, spec['applications'][phase['slot_indices'][0]])
                        self.assertIn(app['application'], {a['application'] for a in resident['applications']})

    def test_current_host_selects_feasible_pairs_and_keeps_slots_and_sizes(self):
        for mix, expected in (('mix1', [['memcached', 'graphchi'], ['xsbench']]),
                              ('mix4', [['cassandra', 'xsbench'], ['graph500']])):
            spec = plan(mix, 'two-then-one', capacity_gib=125)
            self.assertEqual([[a['application'] for a in p['applications']] for p in spec['phases']], expected)
            for phase in spec['phases']:
                self.assertTrue(all(r['spare_mib'] >= 0 for r in phase['numa_memory']['nodes'].values()))
                for i, app in zip(phase['slot_indices'], phase['applications']):
                    self.assertEqual(app, spec['applications'][i])
            single = spec['phases'][1]
            self.assertEqual(single['cpus']['client_cpus'], [])
            self.assertTrue(all(r['client_reserve_mib'] == 0 for r in single['numa_memory']['nodes'].values()))

    def test_explicit_concurrent_mode_is_still_checked_and_small_host_rejected(self):
        config = inventory(); config.setdefault('fig9_execution', {})['mix1'] = 'three-concurrent'
        with self.assertRaisesRegex(ValueError, 'No feasible three-concurrent'):
            rt.plan_mix_resources('mix1', plan('mix1')['applications'], config,
                                  {n: {'total_mib': 125 * 1024} for n in (0, 1)}, topology(), range(48))
        for mix in ('mix2', 'mix3'):
            self.assertEqual(plan(mix, 'three-concurrent')['simultaneous_vms'], 3)
        with self.assertRaisesRegex(ValueError, 'No feasible sequential'):
            rt.plan_mix_resources('mix1', plan('mix1')['applications'], inventory(),
                                  {n: {'total_mib': 60 * 1024} for n in (0, 1)}, topology(), range(48))

    def test_single_batch_leaf_omits_empty_client_cpu_argument(self):
        phase = plan('mix4', 'two-then-one', capacity_gib=125)['phases'][1]
        command = runner.leaf_command('phase-test', inventory()['slots'][1], phase['applications'][0],
                                      'cpu.json', 'work.json', 'mem.json', 'barrier', phase['cpus'], 60)
        self.assertNotIn('--client-cpus', command)


class BarriersAndLifecycle(unittest.TestCase):
    def test_plan_order_and_residency_mismatch_are_rejected_before_vm_launch(self):
        for defect in ('order', 'retained'):
            spec = plan('mix4')
            if defect == 'order':
                spec['phases'][0], spec['phases'][1] = spec['phases'][1], spec['phases'][0]
            else:
                spec['residency'][1]['retained_slot_index'] = spec['residency'][1]['started_slot_index']
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as tmp, \
                 mock.patch.object(runner, 'run_phase') as launch:
                with self.assertRaisesRegex(ValueError, 'mix application order or VM residency'):
                    runner.run_mix('mix4', 1, spec, inventory(), {}, Path(tmp), 'test', 60)
                launch.assert_not_called()
                self.assertFalse((Path(tmp) / 'mix4/repeat-01').exists())

    def test_pair_and_single_barriers_release_only_their_members(self):
        for members in (['memcached', 'graphchi'], ['xsbench']):
            with self.subTest(members=members), tempfile.TemporaryDirectory() as tmp:
                barrier = Path(tmp)
                children = {c: mock.Mock(poll=mock.Mock(return_value=None)) for c in members}
                for c in members:
                    (barrier / (c + '.ready.json')).write_text('{}')
                record = runner.wait_ready(barrier, children, 1)
                self.assertEqual(record['members'], members)
                self.assertEqual(set(record['ready']), set(members))

    def run_synthetic_mix(self, *, failed_case=None, failed_boot=None, failed_stop=None):
        spec = plan('mix4'); config = inventory()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); accesses = {}; active = set(); active_clients = set(); events = []
            barriers = []; pinned = {}
            names = [slot['name'] for slot in config['slots']]
            cases = [phase['applications'][0]['application'] for phase in spec['phases']]
            case_slots = {case: names[i] for i, case in enumerate(data.MIXES['mix4'])}
            active_sets = [{names[i] for i in resident['slot_indices']} for resident in spec['residency']]
            stop_failed = False
            for slot in config['slots']:
                directory = root / slot['name']; directory.mkdir()
                vm = directory / 'vm.json'; vm.write_text('{"original": true}')
                accesses[slot['name']] = {'name': slot['name'], 'vm_config': str(vm), 'directory': directory}

            def boot(a, **kwargs):
                self.assertFalse(active_clients)
                self.assertLess(len(active), 2, 'A VM must stop before the third VM boots')
                active.add(a['name']); events.append(('boot', a['name']))
                if a['name'] == failed_boot:
                    raise RuntimeError('synthetic boot failure')

            def stop(a, **kwargs):
                nonlocal stop_failed
                self.assertFalse(active_clients, 'Clients must finish cleanup before VM shutdown')
                if a['name'] in active:
                    active.remove(a['name']); events.append(('stop', a['name']))
                if a['name'] == failed_stop and not stop_failed:
                    stop_failed = True
                    raise RuntimeError('synthetic VM cleanup failure')

            def child(command, **kwargs):
                case = command[command.index('--cases') + 1]
                name = command[command.index('--name') + 1]
                phase_index = cases.index(case)
                self.assertEqual(active, active_sets[0 if phase_index < 2 else 1],
                                 'Exactly the planned pair of VMs must remain active during each workload')
                self.assertFalse(active_clients, 'The previous client must exit before the next workload starts')
                self.assertEqual(command[command.index('--vm') + 1], case_slots[case])
                active_clients.add(case); events.append(('client-start', case))
                barriers.append(command[command.index('--start-barrier-dir') + 1])
                leaf = root / 'leaves' / name
                app = leaf / case / 'application'; app.mkdir(parents=True)
                (leaf / 'report.json').write_text(json.dumps({'cases': {case: {
                    'workload_start_unix_seconds': 100, 'workload_end_unix_seconds': 110}}}))
                if case == 'cassandra':
                    cpus = spec['phases'][phase_index]['cpus']['client_cpus']
                    for filename in ('load-affinity.json', 'run-affinity.json'):
                        (app / filename).write_text(json.dumps({'status': 'PASS', 'allowed_cpus': cpus, 'observed_pids': [1]}))
                    (app / 'metadata.tsv').write_text('timed_phase_start_unix_seconds\t100\ntimed_phase_end_unix_seconds\t110\n')
                code = 1 if case == failed_case else 0
                def wait(**kwargs):
                    if case in active_clients:
                        active_clients.remove(case); events.append(('client-exit', case))
                    return code
                return mock.Mock(poll=mock.Mock(return_value=code), returncode=code,
                                 wait=mock.Mock(side_effect=wait))

            def pin(a, cpus):
                self.assertIn(a['name'], active)
                if a['name'] in pinned:
                    self.assertEqual(cpus['host_numa_node'], pinned[a['name']]['host_numa_node'])
                pinned[a['name']] = copy.deepcopy(cpus)
                return {'status': 'PASS', 'host_numa_node': cpus['host_numa_node']}

            def audit(placement, guests, topology):
                self.assertEqual(active, {guest['name'] for guest in guests.values()})
                self.assertEqual(set(placement['applications']), set(guests))
                for case, guest in guests.items():
                    self.assertEqual(pinned[guest['name']], placement['applications'][case])
                return {'status': 'PASS'}

            metrics = mock.Mock(extract=mock.Mock(return_value={'status': 'PASS'}))
            caught = None
            with contextlib.ExitStack() as stack:
                for obj, name, kwargs in (
                    (rt, 'access', {'side_effect': lambda n: accesses[n]}),
                    (rt.guest, 'control_lock', {'side_effect': lambda a: contextlib.nullcontext()}),
                    (rt.guest, 'run_dir', {'side_effect': lambda a: a['directory']}),
                    (rt.guest, 'start', {'side_effect': boot}), (rt, 'stop_guest', {'side_effect': stop}),
                    (rt, 'setup_guest', {'return_value': {}}), (rt, 'pin_guest_cpus', {'side_effect': pin}),
                    (rt, 'check_memory_available', {'return_value': {}}),
                    (rt, 'verify_vm_cpu_isolation', {'side_effect': audit}),
                    (rt, 'module', {'return_value': metrics}),
                    (runner, 'wait_ready', {'return_value': {'released_unix_seconds': 100}}),
                    (runner.subprocess, 'Popen', {'side_effect': child}),
                    (data, 'evaluate_application', {'side_effect': lambda case, *_: {'application': case, 'slowdown_percent': 9.0}})):
                    stack.enter_context(mock.patch.object(obj, name, **kwargs))
                try:
                    runner.run_mix('mix4', 1, spec, config, {}, root / 'results', 'test', 60,
                                   results_root=root / 'leaves')
                except RuntimeError as error:
                    caught = error
            self.assertFalse(active)
            self.assertFalse(active_clients)
            record = json.loads((root / 'results/mix4/repeat-01/report.json').read_text())
            for access in accesses.values():
                self.assertEqual(Path(access['vm_config']).read_text(), '{"original": true}')
            return record, events, barriers, caught

    def test_two_vms_boot_then_one_is_replaced_only_after_both_clients_finish(self):
        record, events, barriers, error = self.run_synthetic_mix()
        spec = plan('mix4'); transition = spec['residency'][1]
        names = [slot['name'] for slot in inventory()['slots']]
        first_names = [names[i] for i in spec['residency'][0]['slot_indices']]
        cases = [phase['applications'][0]['application'] for phase in spec['phases']]
        stopped = names[transition['stopped_slot_index']]
        retained = names[transition['retained_slot_index']]
        started = names[transition['started_slot_index']]
        self.assertIsNone(error)
        self.assertEqual(record['status'], 'PASS')
        self.assertEqual(record['slowdown_percent'], 9)
        expected = [('boot', name) for name in first_names]
        expected.extend(event for case in cases[:2]
                        for event in [('client-start', case), ('client-exit', case)])
        expected.extend([('stop', stopped), ('boot', started),
                         ('client-start', cases[2]), ('client-exit', cases[2]),
                         ('stop', started), ('stop', retained)])
        self.assertEqual(events, expected)
        self.assertEqual(len(set(barriers)), 3)
        self.assertEqual(record['execution_mode'], 'sequential')
        self.assertEqual(record['vm_lifecycle'], 'overlapping-pairs')
        self.assertEqual(record['simultaneous_vms'], 2)
        self.assertEqual(record['simultaneous_applications'], 1)
        self.assertNotIn('overlap', record)
        self.assertEqual([p['overlap']['simultaneous_vms'] for p in record['phases']], [1, 1, 1])
        self.assertEqual(data.execution_layout(record)['simultaneous_vms'], 2)

    def test_failed_client_prevents_next_workload_and_all_started_vms_are_cleaned_up(self):
        cases = [phase['applications'][0]['application'] for phase in plan('mix4')['phases']]
        for i, case in enumerate(cases):
            with self.subTest(case=case):
                record, events, _, error = self.run_synthetic_mix(failed_case=case)
                self.assertIsNotNone(error)
                self.assertEqual(record['status'], 'FAIL')
                self.assertEqual([case for action, case in events if action == 'client-start'], cases[:i + 1])
                self.assertEqual({name for action, name in events if action == 'stop'},
                                 {name for action, name in events if action == 'boot'})
                self.assertEqual(sum(action == 'boot' for action, _ in events), 2 if i < 2 else 3)

    def test_partial_boot_failure_launches_no_client_and_cleans_up_started_vms(self):
        names = [slot['name'] for slot in inventory()['slots']]
        first = [names[i] for i in plan('mix4')['residency'][0]['slot_indices']]
        record, events, _, error = self.run_synthetic_mix(failed_boot=first[1])
        self.assertIsNotNone(error)
        self.assertEqual(record['status'], 'FAIL')
        self.assertEqual(events, [('boot', first[0]), ('boot', first[1]),
                                 ('stop', first[1]), ('stop', first[0])])

    def test_third_vm_boot_failure_cleans_up_retained_vm_and_partial_boot(self):
        names = [slot['name'] for slot in inventory()['slots']]
        transition = plan('mix4')['residency'][1]
        record, events, _, error = self.run_synthetic_mix(failed_boot=names[transition['started_slot_index']])
        self.assertIsNotNone(error)
        self.assertEqual(record['status'], 'FAIL')
        self.assertEqual(sum(action == 'client-start' for action, _ in events), 2)
        self.assertEqual({name for action, name in events if action == 'stop'}, set(names))

    def test_handoff_stop_failure_prevents_third_vm_boot(self):
        names = [slot['name'] for slot in inventory()['slots']]
        transition = plan('mix4')['residency'][1]
        record, events, _, error = self.run_synthetic_mix(failed_stop=names[transition['stopped_slot_index']])
        self.assertIsNotNone(error)
        self.assertEqual(record['status'], 'FAIL')
        self.assertEqual(sum(action == 'boot' for action, _ in events), 2)
        self.assertEqual(sum(action == 'client-start' for action, _ in events), 2)

    def test_vm_cleanup_failure_cannot_produce_a_passing_mix(self):
        names = [slot['name'] for slot in inventory()['slots']]
        transition = plan('mix4')['residency'][1]
        record, events, _, _ = self.run_synthetic_mix(failed_stop=names[transition['retained_slot_index']])
        self.assertEqual(record['status'], 'FAIL')
        self.assertTrue(record['cleanup_errors'])
        self.assertEqual({name for action, name in events if action == 'stop'}, set(names))


class Reporting(unittest.TestCase):
    def test_overlapping_pair_measurements_validate_the_handoff(self):
        for defect in ('retained', 'started', 'stop_before_workload_end', 'boot_before_stop', 'boot_after_next_phase'):
            row = phased_row(); handoff = row['handoff']
            if defect == 'retained': handoff['retained_application'] = handoff['stopped_application']
            if defect == 'started': handoff['started_application'] = handoff['retained_application']
            if defect == 'stop_before_workload_end': handoff['stop_completed_unix_seconds'] = 7.9
            if defect == 'boot_before_stop': handoff['boot_completed_unix_seconds'] = 8.0
            if defect == 'boot_after_next_phase': handoff['boot_completed_unix_seconds'] = 9.1
            with self.subTest(defect=defect), self.assertRaises(ValueError):
                data.execution_layout(row)

    def test_rejects_overlapping_phases_or_missing_or_changed_measurements(self):
        for mode in ('sequential', 'two-then-one'):
            for defect in ('overlap', 'missing', 'changed', 'cleanup', 'duplicate'):
                row = phased_row(mode=mode)
                if defect == 'overlap': row['phases'][1]['started_unix_seconds'] = 3
                if defect == 'missing': row['phases'].pop()
                if defect == 'changed': row['applications'][0]['slowdown_percent'] = 100
                if defect == 'cleanup': row['phases'][0]['cleanup_errors'] = ['still running']
                if defect == 'duplicate': row['phases'][-1]['applications'][0] = row['phases'][0]['applications'][0]
                with self.subTest(mode=mode, defect=defect), self.assertRaises(ValueError):
                    data.execution_layout(row)

    def test_resident_vm_results_reject_parent_cleanup_failure(self):
        row = phased_row()
        row['cleanup_errors'] = ['VM shutdown failed']
        with self.assertRaisesRegex(ValueError, 'cleanup'):
            data.execution_layout(row)

    def test_old_per_phase_vms_cannot_be_averaged_with_resident_vms(self):
        rows = [phased_row() for _ in range(3)]
        for i, row in enumerate(rows, 1):
            row['repetition'] = i
        rows[1].pop('vm_lifecycle')
        rows[1].pop('simultaneous_applications')
        rows[1].pop('handoff')
        rows[1]['phases'] = [measured_phase([case], i * 4 + 1, i * 4 + 4)
                             for i, case in enumerate(data.MIXES['mix4'])]
        raw = {'status': 'PASS', 'reference': data.reference_data(ROOT / 'ae/results_baselines/fig9.json'),
               'mixes': {'mix4': {'status': 'PASS', 'repetitions': rows}}}
        self.assertEqual(data.execution_layout(rows[0])['simultaneous_vms'], 2)
        self.assertEqual(data.execution_layout(rows[1])['simultaneous_vms'], 1)
        with self.assertRaisesRegex(ValueError, 'different execution layouts'):
            ae_fig9.summarize(raw)

    def test_legacy_concurrent_results_without_execution_metadata_remain_readable(self):
        row = data.aggregate_mix('mix4', [a for p in phased_row()['phases'] for a in p['applications']])
        layout = data.execution_layout(row)
        self.assertEqual(layout['execution_mode'], 'three-concurrent')
        self.assertEqual(layout['simultaneous_vms'], 3)
        self.assertEqual(layout['phase_applications'], [data.MIXES['mix4']])

    def test_summary_and_plotters_preserve_metadata_and_reject_mixed_repeats(self):
        reference = data.reference_data(ROOT / 'ae/results_baselines/fig9.json')
        legacy = rt.module('phase_legacy_plot_test', ROOT / 'benchmarks/scripts/plot-chameleon-fig9.py')
        for mode, simultaneous in (('sequential', 2), ('two-then-one', 2)):
            with self.subTest(mode=mode):
                rows = [phased_row(mode=mode) for _ in range(3)]
                for i, row in enumerate(rows, 1): row['repetition'] = i
                raw = {'status': 'PASS', 'reference': reference, 'mixes': {'mix4': {'status': 'PASS', 'repetitions': rows}}}
                summary = ae_fig9.summarize(raw)
                self.assertEqual(summary['mixes']['mix4']['execution_mode'], mode)
                self.assertEqual(summary['mixes']['mix4']['simultaneous_vms'], simultaneous)
                self.assertEqual(summary['mixes']['mix4']['slowdown_pct'], 9)
                summary['normalization'] = 'synthetic-test-only'
                with tempfile.TemporaryDirectory() as tmp:
                    prepared = plot_figures.prepare('fig9', summary, reference)
                    plot_figures.render(prepared, tmp)
                    prepared_legacy = legacy.plot_data(raw)
                    legacy.render(prepared_legacy, Path(tmp), 'none')
                    for filename in ('fig9.svg', 'fig9-measured.svg'):
                        svg = (Path(tmp) / filename).read_text()
                        self.assertIn('<svg', svg)
                        self.assertNotIn('[2+1]', svg)
                        self.assertNotIn('[1+1+1]', svg)
                        self.assertNotIn('provided baselines:', svg)
                mixed = copy.deepcopy(raw)
                mixed['mixes']['mix4']['repetitions'][1] = phased_row(
                    mode='two-then-one' if mode == 'sequential' else 'sequential')
                mixed['mixes']['mix4']['repetitions'][1]['repetition'] = 2
                with self.assertRaisesRegex(ValueError, 'different execution layouts'):
                    ae_fig9.summarize(mixed)
                rows[1].pop('phases'); rows[1]['execution_mode'] = 'three-concurrent'
                with self.assertRaisesRegex(ValueError, 'different execution layouts'):
                    ae_fig9.summarize(raw)


if __name__ == '__main__':
    unittest.main()
