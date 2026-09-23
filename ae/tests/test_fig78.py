#!/usr/bin/env python3
"""Small synthetic fixtures test arithmetic and failure handling, not performance."""
import copy
import contextlib
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('ae_fig78', ROOT / 'ae/scripts/ae_fig78.py')
ae = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ae)


class Figure78(unittest.TestCase):
    def setUp(self):
        self.config = ae.load_points()

    def test_frozen_configuration_and_three_repeats(self):
        jobs = ae.schedule(self.config, list(self.config['applications']))
        self.assertEqual(len(jobs), 8 * 4 * 3)
        self.assertNotIn('gcc', self.config['applications'])
        for case, app in self.config['applications'].items():
            own = [job for job in jobs if job['application'] == case]
            self.assertEqual({j['vm_memory_mib'] for j in own}, {app['vm_memory_mib']})
            for role in ae.ROLES:
                self.assertEqual([j['repeat'] for j in own if j['point'] == role], [1, 2, 3])
        expected = {'xsbench': 'd05', 'graphchi': 'd08', 'graph500': 'd11',
                    'pvc': 'd06', 'cassandra': 'd12'}
        for app, candidate in expected.items():
            self.assertEqual(self.config['applications'][app]['points'][-1]['source_candidate'], candidate)
        graph = self.config['applications']['graph500']['workload_configuration']['args']
        self.assertIn('--require-cache', graph)
        self.assertEqual(graph[graph.index('--bfs-iterations') + 1], '32')

    def test_config_has_no_external_measurement_paths(self):
        text = json.dumps(self.config)
        self.assertNotIn('/home2/', text)
        self.assertNotIn('report.json', text)
        self.assertNotIn('measurements', text)

    def test_cpu_groups_are_physically_disjoint(self):
        topology = [{'cpu': cpu, 'socket': node, 'core': cpu % 24, 'node': node}
                    for node in range(2) for cpu in range(node * 24, (node + 1) * 24)]
        topology += [dict(row, cpu=row['cpu'] + 48) for row in topology[:]]
        app = dict(self.config['applications']['memcached'], application='memcached')
        cpu = ae.single_cpu_plan(app, {'client_numa_node': 1}, topology, range(96))
        self.assertEqual(len(cpu['profile']['vcpu_host_cpus']), 12)
        self.assertEqual(len(cpu['profile']['qemu_service_cpus']), 2)
        self.assertEqual(len(cpu['client_cpus']), 12)
        chosen = cpu['profile']['vcpu_host_cpus'] + cpu['profile']['qemu_service_cpus'] + cpu['client_cpus']
        self.assertEqual(len(chosen), len(set(chosen)))
        self.assertTrue(all(value < 48 for value in chosen))

    def test_not_enough_cores_rejected_even_with_smt(self):
        topology = [{'cpu': cpu, 'socket': 0, 'core': cpu % 8, 'node': 0} for cpu in range(16)]
        app = dict(self.config['applications']['memcached'], application='memcached')
        with self.assertRaisesRegex(ValueError, 'Insufficient physical'):
            ae.single_cpu_plan(app, {'client_numa_node': 0}, topology, range(16))

    def test_leaf_records_real_results_and_enables_tracking(self):
        case = 'xsbench'
        app = self.config['applications'][case]
        slot = {'name': 'single', 'rdma_interface': 'ibp1s0'}
        cpu = {'client_numa_node': 1, 'client_cpus': []}
        for job in ae.schedule(self.config, [case]):
            cmd = ae.leaf_command(job, app, slot, cpu, Path('/tmp/results'), 100)
            self.assertIn('--all-local-tracking', cmd)
            self.assertIn('--cpu-pinning', cmd)
            self.assertIn('--performance-mode', cmd)
            self.assertEqual(cmd[cmd.index('--results-root') + 1], '/tmp/results/raw')
            if job['point'] == 'all_local':
                self.assertEqual(cmd[cmd.index('--mode') + 1], 'all-local')
                self.assertEqual(cmd[cmd.index('--sample-period') + 1], '65536')

    def fixture(self, root, cases=('xsbench', 'memcached')):
        ae.save(root / 'manifest.json', {'applications': list(cases), 'points_configuration': self.config})
        for case in cases:
            memory = self.config['applications'][case]['vm_memory_mib']
            for role in ae.ROLES:
                for repeat in range(1, 4):
                    name = ae.run_name(case, role, repeat)
                    leaf = root / 'raw' / name
                    ae.save(root / 'runs' / name / 'execution.json', {'status': 'PASS', 'cleanup_errors': []})
                    record = {'status': 'PASS', 'exit_code': 0, 'checks': {
                              'all_local_tracking_active': True, 'tracking_parameters_match': True,
                              'no_reclamation': True}, 'summary': {'window_complete': True,
                              'mean_reclaim_percent': repeat * 10,
                              'configured_memory_bytes': memory * 2**20},
                              'counter_delta': {'policy': {'action_errors': 9}, 'rdma': {'write_bytes': 4096}}}
                    ae.save(leaf / 'report.json', {'status': 'PASS', 'cases': {case: record}})
                    application = leaf / case / 'application' / 'sample'
                    application.mkdir(parents=True)
                    if case == 'memcached':
                        mops = (.01, .02, .01)[repeat-1] if role == 'all_local' else .005
                        p95 = repeat * 100 if role == 'all_local' else 300 + repeat * 100
                        (application / 'generator.csv').write_text(
                            'goodput_mops,completed_mean_us,completed_p95_us,completed_p99_us,load_valid,schedule_complete\n'
                            f'{mops},100,{p95},900,1,1\n')
                    else:
                        seconds = (100, 120, 80)[repeat-1] if role == 'all_local' else 100 + repeat * 10
                        (application / 'time.txt').write_text(
                            f'Elapsed (wall clock) time (h:mm:ss or m:ss): {seconds // 60}:{seconds % 60:02d}.00\n')

    def test_three_run_means_fixed_denominator_and_p95(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.fixture(root)
            summary = ae.summarize(root)
            self.assertEqual(summary['status'], 'PASS')
            xs = summary['applications']['xsbench']
            self.assertEqual(xs['all_local']['mean_cost'], 100)
            self.assertAlmostEqual(xs['points'][3]['slowdown_pct'], 20)
            self.assertEqual(xs['points'][3]['reclamation_pct'], 20)
            self.assertEqual(xs['points'][0]['reclamation_pct'], 0)
            self.assertEqual(xs['points'][0]['runs'][0]['reclamation_pct'], 10)
            mem = summary['applications']['memcached']
            self.assertAlmostEqual(mem['all_local']['mean_cost'], 250 / 3)
            self.assertEqual(mem['all_local']['mean_p95_us'], 200)
            self.assertAlmostEqual(mem['points'][3]['p95_slowdown_pct'], 150)
            self.assertAlmostEqual(mem['points'][3]['slowdown_pct'], 140)
            locals = json.loads((root / 'all-local.json').read_text())
            self.assertEqual(locals['applications']['memcached']['all_local'], mem['all_local'])
            self.assertEqual(xs['points'][3]['runs'][0]['counters']['policy']['action_errors'], 9)

    def test_missing_repeat_is_incomplete_not_zero(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.fixture(root, ('xsbench',))
            (root / 'raw/xsbench-high-r03/report.json').unlink()
            result = ae.summarize(root)
            self.assertEqual(result['status'], 'INCOMPLETE')
            self.assertNotIn('xsbench', result['applications'])
            self.assertTrue(result['errors'])

    def test_failed_application_not_converted_to_performance(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.fixture(root, ('xsbench',))
            leaf = root / 'raw/xsbench-high-r01'
            report = json.loads((leaf / 'report.json').read_text())
            report['cases']['xsbench']['exit_code'] = 1
            ae.save(leaf / 'report.json', report)
            with self.assertRaisesRegex(ValueError, 'did not complete'):
                ae.extract_run(leaf, 'xsbench')

    def test_all_local_tracking_must_be_enabled(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.fixture(root, ('xsbench',))
            path = root / 'raw/xsbench-all_local-r01/report.json'
            report = json.loads(path.read_text())
            report['cases']['xsbench']['checks']['all_local_tracking_active'] = False
            ae.save(path, report)
            self.assertEqual(ae.summarize(root)['status'], 'INCOMPLETE')

    def fake_runtime(self, root, fail_connection=False):
        config = root / 'vm.json'
        ae.save(config, {'original': True})
        access = {'name': 'single', 'vm_config': str(config)}
        events, state = [], {'active': False}
        def start(*args, **kwargs):
            self.assertFalse(state['active'])
            state['active'] = True
            events.append('boot')
        def connect(*args):
            self.assertTrue(state['active'])
            events.append('connect')
            if fail_connection:
                raise RuntimeError('Pool did not connect')
            return {'status': 'PASS', 'ready': True}
        def stop(*args, **kwargs):
            self.assertTrue(state['active'])
            events.append('stop')
            state['active'] = False
        guest = SimpleNamespace(active=lambda _: state['active'], launcher_idle=lambda _: True,
                                control_lock=lambda _: contextlib.nullcontext(),
                                start=start, run_dir=lambda _: root / 'running')
        runtime = SimpleNamespace(guest=guest, access=lambda _: access,
                                  slot_config=lambda *args: {'test_config': True},
                                  setup_guest=connect, pin_guest_cpus=lambda *args: {}, stop_guest=stop)
        return runtime, events, config

    def test_three_observations_have_three_boot_connect_stop_cycles(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime, events, config = self.fake_runtime(root)
            jobs = ae.schedule(self.config, ['xsbench'])[:3]
            app = self.config['applications']['xsbench']
            cpu = {'profile': {}, 'client_cpus': [], 'client_numa_node': 1}
            process = SimpleNamespace(poll=lambda: 0, returncode=0)
            with mock.patch.object(ae, 'start_guard', return_value=(None, None, None)), \
                 mock.patch.object(ae.subprocess, 'Popen', return_value=process), \
                 mock.patch.object(ae, 'extract_run', return_value={'performance': {'cost': 1}}):
                for job in jobs:
                    ae.run_one(runtime, job, app, {'name': 'single', 'rdma_interface': 'ibp1s0'},
                               {'server': {}}, {}, cpu, root, 60, [])
            self.assertEqual(events, ['boot', 'connect', 'stop'] * 3)
            self.assertEqual(json.loads(config.read_text()), {'original': True})

    def test_pool_failure_stops_owned_vm_and_never_launches_workload(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime, events, config = self.fake_runtime(root, fail_connection=True)
            job = ae.schedule(self.config, ['xsbench'])[0]
            with mock.patch.object(ae, 'start_guard', return_value=(None, None, None)), \
                 mock.patch.object(ae.subprocess, 'Popen') as launch:
                with self.assertRaisesRegex(RuntimeError, 'Pool did not connect'):
                    ae.run_one(runtime, job, self.config['applications']['xsbench'],
                               {'name': 'single'}, {'server': {}}, {}, {'profile': {}}, root, 60, [])
            launch.assert_not_called()
            self.assertEqual(events, ['boot', 'connect', 'stop'])
            self.assertEqual(json.loads(config.read_text()), {'original': True})
            record = json.loads((root / 'runs' / job['name'] / 'execution.json').read_text())
            self.assertEqual(record['status'], 'FAIL')


if __name__ == '__main__':
    unittest.main()
