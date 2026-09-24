#!/usr/bin/env python3
"""Guest update policy and its experiment integration, without changing the Host."""
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'benchmarks/scripts'))
import chameleon_fig9_runtime as rt

spec = importlib.util.spec_from_file_location('guest_update_policy',
    ROOT / 'hyperalloc-6.18/scripts/disable-guest-auto-updates.py')
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)


class Policy(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.config = Path(temp.name) / 'apt.conf'
        self.commands = []
        self.states = {unit: {'ActiveState': 'inactive', 'UnitFileState': 'masked'}
                       for unit in policy.UNITS}
        def run(*args):
            self.commands.append(args)
            return "value='0'\n" if args[0] == 'apt-config' else ''
        for patch in (mock.patch.object(policy.os, 'geteuid', return_value=0),
                      mock.patch.object(policy, 'CONFIG', self.config),
                      mock.patch.object(policy, 'LOCKS', ()),
                      mock.patch.object(policy, 'run', side_effect=run),
                      mock.patch.object(policy, 'state', side_effect=lambda unit: self.states[unit])):
            patch.start()
            self.addCleanup(patch.stop)

    def test_persistent_policy_is_idempotent_and_does_not_stop_package_jobs(self):
        for _ in range(2):
            self.assertEqual(policy.apply()['status'], 'PASS')
        self.assertEqual(len(self.config.read_text().splitlines()), 4)
        for option in policy.OPTIONS:
            self.assertIn('APT::Periodic::' + option + ' "0";', self.config.read_text())
        self.assertIn(('systemctl', 'mask', *policy.UNITS), self.commands)
        for command in self.commands:
            if command[:2] == ('systemctl', 'stop'):
                self.assertFalse(set(policy.JOBS) & set(command[2:]))
        self.assertFalse(any('--now' in command for command in self.commands))

    def test_existing_installer_finishes_before_monitor_stops(self):
        self.states[policy.JOBS[1]]['ActiveState'] = 'active'
        def finish(_):
            self.assertNotIn(('systemctl', 'stop', policy.MONITOR), self.commands)
            self.states[policy.JOBS[1]]['ActiveState'] = 'inactive'
        with mock.patch.object(policy.time, 'sleep', side_effect=finish) as sleep:
            self.assertEqual(policy.apply()['status'], 'PASS')
        sleep.assert_called_once()

    def test_busy_installation_times_out_without_stopping_it(self):
        self.states[policy.JOBS[0]]['ActiveState'] = 'active'
        with mock.patch.object(policy.time, 'monotonic', side_effect=[0, 301]):
            with self.assertRaises(TimeoutError):
                policy.apply()
        self.assertNotIn(('systemctl', 'stop', policy.MONITOR), self.commands)

    def test_busy_package_lock_blocks_measurement(self):
        with mock.patch.object(policy, 'package_locks', side_effect=BlockingIOError), \
             mock.patch.object(policy.time, 'monotonic', side_effect=[0, 301]):
            with self.assertRaisesRegex(TimeoutError, 'package lock'):
                policy.apply()
        self.assertNotIn(('systemctl', 'stop', policy.MONITOR), self.commands)

    def test_periodic_override_is_rejected(self):
        with mock.patch.object(policy, 'run', return_value="value='1'\n"):
            with self.assertRaisesRegex(RuntimeError, 'periodic setting'):
                policy.apply()

    def test_nonroot_invocation_changes_nothing(self):
        with mock.patch.object(policy.os, 'geteuid', return_value=1000):
            with self.assertRaises(PermissionError):
                policy.apply()
        self.assertEqual(self.commands, [])
        self.assertFalse(self.config.exists())


class Integration(unittest.TestCase):
    def test_guest_policy_failure_preserves_diagnosis(self):
        error = subprocess.CalledProcessError(1, ['ssh'], stderr='Existing package update has not finished')
        with mock.patch.object(rt.guest, 'remote', side_effect=error):
            with self.assertRaisesRegex(RuntimeError, 'package update has not finished'):
                rt.guest.disable_auto_updates({'name': 'test'})

    def test_policy_runs_inside_guest_over_ssh(self):
        result = subprocess.CompletedProcess([], 0, stdout='{"status":"PASS"}')
        with mock.patch.object(rt.guest, 'remote', return_value=result) as remote:
            self.assertEqual(rt.guest.disable_auto_updates({'name': 'test'})['status'], 'PASS')
        args, kwargs = remote.call_args
        self.assertEqual(args[1][:4], ['sudo', '-n', 'python3', '-'])
        self.assertIn('Persistently disable', kwargs['input'])

    def test_runtime_does_not_prepare_workload_before_update_policy_passes(self):
        with mock.patch.object(rt.guest, 'wait_ssh'), \
             mock.patch.object(rt.guest, 'disable_auto_updates', side_effect=TimeoutError('updating')) as disable, \
             mock.patch.object(rt, 'grow_guest_root') as grow:
            with self.assertRaisesRegex(TimeoutError, 'updating'):
                rt.setup_guest({'name': 'test'}, {}, {})
        disable.assert_called_once()
        grow.assert_not_called()

    def test_template_policy_precedes_package_setup(self):
        with mock.patch.object(rt.guest, 'wait_cloud'), \
             mock.patch.object(rt.guest, 'disable_auto_updates', side_effect=TimeoutError('updating')), \
             mock.patch.object(rt.guest, 'transfer') as transfer:
            with self.assertRaisesRegex(TimeoutError, 'updating'):
                rt.guest.setup({'user': 'ubuntu'})
        transfer.assert_not_called()


if __name__ == '__main__':
    unittest.main()
