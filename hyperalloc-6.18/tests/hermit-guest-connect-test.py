#!/usr/bin/env python3
"""Hermit connection classification and ownership using fake sysfs/processes."""
import importlib.util
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('hermit_guest_connect', ROOT / 'scripts/hermit-guest.py')
guest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guest)


class FakeGuest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.addCleanup(self.directory.cleanup)
        for target, value in [('SYS', self.root / 'sys'), ('CONTROL_LOCK', self.root / 'control.lock')]:
            own = patch.object(guest, target, value)
            own.start(); self.addCleanup(own.stop)
        self.args = SimpleNamespace(server='192.0.2.1', port=9400, pool_mib=128,
                                    interface='ib0', connect_attempts=3, retry_delay_seconds=1)
        self.log = '[    1.000000] boot\n'
        self.outcomes = []
        self.calls = []
        self.dmesg_fails = False
        own = patch.object(guest, 'check', return_value={'status': 'PASS', 'connection_tested': False,
                     'module_parameters': guest.parameters(self.args)})
        own.start(); self.addCleanup(own.stop)
        own = patch.object(guest.subprocess, 'run', side_effect=self.run_command)
        own.start(); self.addCleanup(own.stop)
        own = patch.object(guest.time, 'sleep')
        self.sleep = own.start(); self.addCleanup(own.stop)

    def load_module(self, **overrides):
        module = guest.SYS / 'module/rswap_client'
        (module / 'parameters').mkdir(parents=True)
        for name, value in {'backend': 'rdma', 'sip': '192.0.2.1', 'sport': '9400', 'pool_mb': '128'}.items():
            (module / 'parameters' / name).write_text(value + '\n')
        (module / 'refcnt').write_text('0\n')
        stats = {'backend': 'rdma', 'registered': 1, 'capacity_pages': 128 * 256,
                 'live_slots': 0, 'allocated_pages': 0, 'inflight': 0, **overrides}
        backend = guest.SYS / 'kernel/debug/hermit/stats'
        backend.parent.mkdir(parents=True, exist_ok=True)
        backend.write_text(''.join(f'{k} {v}\n' for k, v in stats.items()))
        transport = guest.SYS / 'kernel/debug/hermit_rdma/stats'
        transport.parent.mkdir(parents=True, exist_ok=True)
        transport.write_text('broken 0\nmax_transfer_bytes 1048576\n')

    def run_command(self, argv, **kwargs):
        self.calls.append(argv)
        if argv[0] == 'dmesg':
            return subprocess.CompletedProcess(argv, int(self.dmesg_fails),
                                               '' if self.dmesg_fails else self.log,
                                               'permission denied' if self.dmesg_fails else '')
        if argv[:3] == ['modprobe', '-r', '--first-time']:
            shutil.rmtree(guest.SYS / 'module/rswap_client')
            return subprocess.CompletedProcess(argv, 0, '', '')
        if argv[:3] == ['modprobe', '--first-time', 'rswap_client']:
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, dict):
                self.load_module(**outcome)
                return subprocess.CompletedProcess(argv, 0, '', '')
            if outcome == 'concurrent_module':
                self.load_module()
            elif outcome != 'no_log':
                self.log += f'[    {len(self.calls)}.000000] hermit: RDMA connect failed error={outcome} state=2\n'
            return subprocess.CompletedProcess(argv, 1, 'modprobe stdout preserved', 'modprobe error preserved')
        if argv[0] == 'modprobe':
            return subprocess.CompletedProcess(argv, 0, '', '')
        self.fail('Unexpected external command: ' + str(argv))

    def attempt_commands(self):
        return [c for c in self.calls if c[:3] == ['modprobe', '--first-time', 'rswap_client']]

    def unload_commands(self):
        return [c for c in self.calls if '-r' in c]

    def test_fresh_stale_rejection_retries_and_verifies_success(self):
        self.outcomes = [-10, {}]
        result = guest.start(self.args)
        self.assertTrue(result['ready'])
        self.assertEqual(result['connected_attempt'], 2)
        self.assertEqual(result['attempts'][0]['classification'], 'IB_CM_REJ_STALE_CONN')
        self.assertEqual(result['attempts'][0]['stderr'], 'modprobe error preserved')
        self.assertIn('error=-10 state=2', result['attempts'][0]['dmesg']['text'])
        self.assertEqual(result['verification']['backend_stats']['registered'], 1)
        self.sleep.assert_called_once_with(1)
        self.assertFalse(self.unload_commands())

    def test_old_stale_line_cannot_authorize_retry(self):
        self.log += '[    2.000000] hermit: RDMA connect failed error=-10 state=2\n'
        self.outcomes = ['no_log', {}]
        with self.assertRaises(guest.ConnectionFailure) as failure:
            guest.start(self.args)
        self.assertEqual(failure.exception.report['attempts'][0]['dmesg']['text'], '')
        self.assertEqual(len(self.attempt_commands()), 1)
        self.sleep.assert_not_called()

    def test_permanent_failures_and_consumer_rejection_are_not_retried(self):
        for error in [-28, -12, -110, -22]:
            with self.subTest(error=error):
                self.outcomes = [error, {}]
                before = len(self.attempt_commands())
                with self.assertRaises(guest.ConnectionFailure): guest.start(self.args)
                self.assertEqual(len(self.attempt_commands()), before + 1)
        self.sleep.assert_not_called()

    def test_missing_dmesg_permission_never_guesses_from_modprobe_errno(self):
        self.dmesg_fails = True
        self.outcomes = [-10, {}]
        with self.assertRaises(guest.ConnectionFailure) as failure: guest.start(self.args)
        attempt = failure.exception.report['attempts'][0]
        self.assertFalse(attempt['dmesg']['available'])
        self.assertIn('permission denied', attempt['dmesg']['read_errors'])
        self.assertEqual(len(self.attempt_commands()), 1)

    def test_stale_retry_budget_is_bounded_and_no_module_is_unloaded(self):
        self.outcomes = [-10] * 4
        with self.assertRaisesRegex(guest.ConnectionFailure, 'attempt limit reached') as failure:
            guest.start(self.args)
        self.assertEqual(len(failure.exception.report['attempts']), 3)
        self.assertEqual([c.args for c in self.sleep.call_args_list], [(1,), (2,)])
        self.assertFalse(self.unload_commands())

    def test_existing_backend_is_never_reconfigured_or_unloaded(self):
        self.load_module()
        with self.assertRaisesRegex(guest.ConnectionFailure, 'already loaded'):
            guest.start(self.args)
        self.assertEqual(self.calls, [])

    def test_another_helper_lock_prevents_any_loading(self):
        with guest.control_lock():
            with self.assertRaisesRegex(ValueError, 'Another Hermit helper'):
                guest.start(self.args)
        self.assertEqual(self.calls, [])

    def test_interruption_after_own_load_cleans_idle_backend(self):
        self.outcomes = [{}]
        with patch.object(guest, 'verify_connected', side_effect=KeyboardInterrupt):
            with self.assertRaises(guest.ConnectionFailure) as failure:
                guest.start(self.args)
        self.assertFalse(failure.exception.report['ready'])
        self.assertEqual(failure.exception.report['cleanup']['status'], 'UNLOADED')
        self.assertFalse((guest.SYS / 'module/rswap_client').exists())

    def test_invalid_retry_budget_loads_nothing(self):
        for changes in ({'connect_attempts': 0}, {'connect_attempts': 6},
                        {'retry_delay_seconds': float('nan')},
                        {'retry_delay_seconds': -1}):
            with self.subTest(changes=changes):
                args = SimpleNamespace(**{**vars(self.args), **changes})
                with self.assertRaises(ValueError): guest.start(args)
        self.assertEqual(self.calls, [])

    def test_racing_first_time_load_failure_does_not_unload_other_module(self):
        self.outcomes = ['concurrent_module']
        with self.assertRaisesRegex(guest.ConnectionFailure, 'module is present'):
            guest.start(self.args)
        self.assertTrue((guest.SYS / 'module/rswap_client').exists())
        self.assertFalse(self.unload_commands())

    def test_wrong_pool_is_not_ready_and_owned_idle_module_is_cleaned(self):
        self.outcomes = [{'capacity_pages': 64 * 256}]
        with self.assertRaises(guest.ConnectionFailure) as failure: guest.start(self.args)
        result = failure.exception.report
        self.assertFalse(result['ready'])
        self.assertEqual(result['cleanup']['status'], 'UNLOADED')
        self.assertFalse((guest.SYS / 'module/rswap_client').exists())
        self.assertEqual(len(self.unload_commands()), 1)

    def test_active_resources_prevent_cleanup_even_for_owned_module(self):
        self.outcomes = [{'capacity_pages': 64 * 256, 'live_slots': 1, 'allocated_pages': 16}]
        with self.assertRaises(guest.ConnectionFailure) as failure: guest.start(self.args)
        self.assertEqual(failure.exception.report['cleanup']['status'], 'PRESERVED')
        self.assertTrue((guest.SYS / 'module/rswap_client').exists())
        self.assertFalse(self.unload_commands())

    def test_different_module_identity_is_not_removed(self):
        self.load_module()
        identity = guest.module_identity()
        with patch.object(guest, 'module_identity', return_value=(identity[0] + 1, identity[1])):
            result = guest.cleanup_owned(identity)
        self.assertEqual(result['status'], 'PRESERVED')
        self.assertFalse(self.unload_commands())

    def test_wrong_endpoint_or_broken_transport_cannot_report_ready(self):
        self.load_module()
        endpoint = guest.SYS / 'module/rswap_client/parameters/sport'
        endpoint.write_text('9401\n')
        with self.assertRaisesRegex(ValueError, 'parameters differ'): guest.verify_connected(self.args)
        endpoint.write_text('9400\n')
        (guest.SYS / 'kernel/debug/hermit_rdma/stats').write_text('broken 1\nmax_transfer_bytes 4096\n')
        with self.assertRaisesRegex(ValueError, 'not healthy'): guest.verify_connected(self.args)


class FreshEvidence(unittest.TestCase):
    def snapshot(self, text): return {'returncode': 0, 'stdout': text, 'stderr': ''}

    def test_rotating_ring_uses_retained_boundary(self):
        evidence = guest.fresh_dmesg(self.snapshot('old\n[1] anchor\n'),
            self.snapshot('[1] anchor\n[2] hermit: RDMA connect failed error=-10 state=2\n'))
        self.assertTrue(guest.stale_connection(evidence))
        self.assertNotIn('anchor', evidence['text'])

    def test_no_overlap_or_wrong_phase_does_not_establish_stale_connect(self):
        evidence = guest.fresh_dmesg(self.snapshot('[1] old\n'),
            self.snapshot('[2] hermit: RDMA connect failed error=-10 state=2\n'))
        self.assertFalse(guest.stale_connection(evidence))
        self.assertFalse(guest.stale_connection({'available': True,
            'text': 'hermit: RDMA connect failed error=-10 state=1'}))


if __name__ == '__main__': unittest.main()
