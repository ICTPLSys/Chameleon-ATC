#!/usr/bin/env python3
"""AE entry points reject impossible NUMA layouts before creating runs."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'ae/scripts'))
import ae_fig9
import evaluate


class EntryPoints(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name) / 'results'
        self.inventory = ROOT / 'ae/config/host.example.json'
        rows = [{'cpu': n * 24 + c, 'node': n, 'socket': n, 'core': c}
                for n in (0, 1) for c in range(24)]
        for patcher in (mock.patch.object(ae_fig9.rt.affinity, 'topology', return_value=rows),
                        mock.patch.object(ae_fig9.rt.os, 'sched_getaffinity', return_value=set(range(48))),
                        mock.patch.object(ae_fig9.rt, 'numa_memory_snapshot', return_value={
                            n: {'total_mib': 125 * 1024} for n in (0, 1)})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_plan_reserves_each_pair_of_vms_without_writing(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            ae_fig9.main(['--plan', '--historical-all-local', '--inventory', str(self.inventory),
                          '--output-dir', str(self.out)])
        plan = json.loads(output.getvalue())
        self.assertEqual(plan['resource_status'], 'PASS')
        self.assertEqual(plan['resource_errors'], {})
        for mix in ('mix1', 'mix2', 'mix3', 'mix4'):
            self.assertEqual(plan['mixes'][mix]['execution_mode'], 'sequential')
            self.assertEqual(plan['mixes'][mix]['simultaneous_vms'], 2)
            self.assertEqual(plan['mixes'][mix]['simultaneous_applications'], 1)
            self.assertEqual(plan['mixes'][mix]['vm_lifecycle'], 'overlapping-pairs')
            self.assertEqual([len(r['applications']) for r in plan['mixes'][mix]['residency']], [2, 2])
            self.assertEqual([len(p['applications']) for p in plan['mixes'][mix]['phases']], [1, 1, 1])
            for phase in plan['mixes'][mix]['phases']:
                self.assertEqual(phase['cpus']['client_numa_node'], 1)
                self.assertIn('numa_memory', phase)
        self.assertFalse(self.out.exists())

    def test_fig9_run_fails_without_creating_results_or_launching_backend(self):
        with mock.patch.object(ae_fig9.subprocess, 'run') as launch, \
             mock.patch.object(ae_fig9.rt, 'numa_memory_snapshot', return_value={n: {'total_mib': 16 * 1024} for n in (0, 1)}):
            with self.assertRaisesRegex(ValueError, 'No feasible whole-VM placement'):
                ae_fig9.main(['--run', '--historical-all-local', '--inventory', str(self.inventory),
                              '--output-dir', str(self.out)])
        launch.assert_not_called()
        self.assertFalse(self.out.exists())

    def test_run_all_checks_fig9_before_starting_fig78(self):
        with mock.patch.object(evaluate.subprocess, 'run') as launch, \
             mock.patch.object(ae_fig9.rt, 'numa_memory_snapshot', return_value={n: {'total_mib': 16 * 1024} for n in (0, 1)}):
            with self.assertRaisesRegex(ValueError, 'No feasible whole-VM placement'):
                evaluate.main(['all', '--inventory', str(self.inventory), '--results', str(self.out)])
        launch.assert_not_called()
        self.assertFalse(self.out.exists())

    def test_all_default_mixes_pass_preflight_and_launch_both_figures(self):
        with mock.patch.object(evaluate.subprocess, 'run') as launch, contextlib.redirect_stdout(io.StringIO()):
            evaluate.main(['all', '--inventory', str(self.inventory),
                           '--results', str(self.out)])
        self.assertEqual(launch.call_count, 2)
        self.assertIn('ae_fig78.py', launch.call_args_list[0].args[0][1])
        self.assertIn('ae_fig9.py', launch.call_args_list[1].args[0][1])


if __name__ == '__main__':
    unittest.main()
