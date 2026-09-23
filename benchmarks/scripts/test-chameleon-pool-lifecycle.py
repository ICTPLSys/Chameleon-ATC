#!/usr/bin/env python3
"""Local regression checks: connection diagnostics and owned-VM teardown."""
import subprocess
import unittest
from unittest import mock

import chameleon_fig9_runtime as rt


class PoolLifecycle(unittest.TestCase):
    def test_remote_error_retains_stderr_and_remains_called_process_error(self):
        result = subprocess.CompletedProcess(['ssh', 'test'], 1, 'attempt 1', 'stale connection diagnosis')
        with mock.patch.object(rt.guest, 'ssh_command', return_value=result.args), \
             mock.patch.object(rt.subprocess, 'run', return_value=result):
            with self.assertRaises(subprocess.CalledProcessError) as caught:
                rt.remote({}, ['example'])
        self.assertIn('stale connection diagnosis', str(caught.exception))
        self.assertEqual(caught.exception.stdout, 'attempt 1')

    def test_disconnect_happens_before_vm_shutdown(self):
        events = []
        def remote(a, args):
            events.append(args)
            return 'loaded' if args[0] == 'sh' else '{}'
        with mock.patch.object(rt.guest, 'active', return_value=True), \
             mock.patch.object(rt, 'remote', side_effect=remote), \
             mock.patch.object(rt.guest, 'stop', side_effect=lambda *a, **kw: events.append('shutdown')):
            rt.stop_guest({'user': 'ubuntu'})
        self.assertEqual(events[-1], 'shutdown')
        self.assertEqual(events[-2], ['sudo', '-n', 'python3', '/home/ubuntu/chameleon-tools/hermit-guest.py', 'stop'])

    def test_failure_still_stops_owned_vm_and_is_reported(self):
        with mock.patch.object(rt.guest, 'active', return_value=True), \
             mock.patch.object(rt, 'remote', side_effect=subprocess.CalledProcessError(255, ['ssh'])), \
             mock.patch.object(rt.guest, 'stop') as stop:
            with self.assertRaisesRegex(RuntimeError, 'disconnect was not confirmed'):
                rt.stop_guest({'user': 'ubuntu'})
            stop.assert_called_once()

    def test_already_offline_guest_never_uses_ssh(self):
        with mock.patch.object(rt.guest, 'active', return_value=False), \
             mock.patch.object(rt, 'remote') as remote, mock.patch.object(rt.guest, 'stop'):
            rt.stop_guest({'user': 'ubuntu'})
            remote.assert_not_called()


if __name__ == '__main__':
    unittest.main()
