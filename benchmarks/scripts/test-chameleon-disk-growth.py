#!/usr/bin/env python3
"""Disk provisioning regressions using tiny sparse images, never real Guests."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import chameleon_fig9_runtime as rt


GiB = 1024 ** 3


class Configuration(unittest.TestCase):
    def test_capacity_is_optional_but_explicit_value_must_be_positive_integer(self):
        self.assertIsNone(rt.requested_disk_bytes({}))
        self.assertEqual(rt.requested_disk_bytes({'disk_size_gib': 350}), 350 * GiB)
        for value in (None, False, True, 0, -1, '350', 350.0):
            with self.subTest(value=value), self.assertRaises(ValueError):
                rt.requested_disk_bytes({'disk_size_gib': value})

    def test_unconfigured_guest_does_not_run_disk_commands(self):
        with mock.patch.object(rt.subprocess, 'run') as command:
            self.assertIsNone(rt.grow_guest_root({}, {}))
            rt.grow_slot_overlay({}, Path('/not-used'))
        command.assert_not_called()


@unittest.skipUnless(shutil.which('qemu-img'), 'qemu-img is required for sparse image tests')
class OwnedOverlay(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'template.qcow2'
        subprocess.run(['qemu-img', 'create', '-f', 'qcow2', str(self.source), '1G'],
                       check=True, capture_output=True)
        self.source_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()
        config = self.root / 'template.json'
        config.write_text(json.dumps({'disk': str(self.source), 'disk_format': 'qcow2'}))
        identity = self.root / 'test-key'
        identity.write_text('synthetic-test-identity')
        self.template = {'name': 'template', 'vm_config': str(config),
                         'identity_file': str(identity), 'user': 'ubuntu'}
        self.slot = {'name': 'test-slot', 'ssh_port': 5991, 'disk_size_gib': 2}
        for patch in (mock.patch.object(rt, 'HA', self.root / 'ha'),
                      mock.patch.object(rt.guest, 'active', return_value=False),
                      mock.patch.object(rt.guest, 'launcher_idle', return_value=True)):
            patch.start()
            self.addCleanup(patch.stop)
        self.owned = rt.HA / 'build/guests/test-slot'
        self.disk = self.owned / 'disk.qcow2'

    def prepare(self):
        rt.prepare_slots({'slots': [self.slot]}, self.template, [{'name': 'test-slot'}])

    def size(self):
        return json.loads(subprocess.check_output(
            ['qemu-img', 'info', '--output=json', str(self.disk)], text=True))['virtual-size']

    def assert_backing_unchanged(self):
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), self.source_hash)

    def test_new_overlay_grows_without_touching_backing_and_lower_request_never_shrinks(self):
        self.prepare()
        self.assertEqual(self.size(), 2 * GiB)
        evidence = json.loads((self.owned / 'disk-growth.json').read_text())
        self.assertEqual(evidence['action'], 'grown')
        self.assertEqual(evidence['before_bytes'], GiB)
        self.slot['disk_size_gib'] = 1
        self.prepare()
        self.assertEqual(self.size(), 2 * GiB)
        self.assertEqual(json.loads((self.owned / 'disk-growth.json').read_text())['action'], 'unchanged')
        self.assert_backing_unchanged()

    def test_existing_owned_overlay_grows_after_target_increases(self):
        self.prepare()
        self.slot['disk_size_gib'] = 3
        self.prepare()
        self.assertEqual(self.size(), 3 * GiB)
        self.assert_backing_unchanged()

    def test_active_or_starting_guest_prevents_offline_disk_access(self):
        self.prepare()
        self.slot['disk_size_gib'] = 3
        for active, idle in ((True, True), (False, False)):
            with self.subTest(active=active, idle=idle), \
                 mock.patch.object(rt.guest, 'active', return_value=active), \
                 mock.patch.object(rt.guest, 'launcher_idle', return_value=idle), \
                 mock.patch.object(rt.subprocess, 'run') as command, \
                 self.assertRaisesRegex(ValueError, 'Stop the VM'):
                rt.grow_slot_overlay(self.slot, self.source)
            command.assert_not_called()
        self.assertEqual(self.size(), 2 * GiB)
        self.assert_backing_unchanged()

    def test_backing_symlink_is_rejected_before_qemu_img_can_change_it(self):
        self.prepare()
        self.disk.unlink()
        self.disk.symlink_to(self.source)
        with self.assertRaisesRegex(ValueError, 'independent owned overlay'):
            rt.grow_slot_overlay(self.slot, self.source)
        self.assert_backing_unchanged()

    def test_wrong_backing_is_rejected_even_with_old_ownership_marker(self):
        self.prepare()
        other = self.root / 'other.qcow2'
        subprocess.run(['qemu-img', 'create', '-f', 'qcow2', str(other), '1G'],
                       check=True, capture_output=True)
        self.disk.unlink()
        subprocess.run(['qemu-img', 'create', '-f', 'qcow2', '-F', 'qcow2', '-b',
                        str(other), str(self.disk)], check=True, capture_output=True)
        with self.assertRaisesRegex(ValueError, 'selected template'):
            self.prepare()
        self.assertEqual(self.size(), GiB)
        self.assert_backing_unchanged()


class GuestRoot(unittest.TestCase):
    def execute(self, grow_returncode=0, grow_output='CHANGED: partition=1',
                resize_returncode=0, disk_bytes=2 * GiB, fstype='ext4'):
        start = 227328 * 512
        record = subprocess.CompletedProcess([], grow_returncode, grow_output, '')
        resize = subprocess.CompletedProcess([], resize_returncode, '', 'resize2fs test output')
        def read_command(argv, **kwargs):
            if argv[0] == 'findmnt':
                return json.dumps({'filesystems': [{'source': '/dev/vda1', 'fstype': fstype}]})
            return str(disk_bytes if argv[-1] == '/dev/vda' else disk_bytes - start - 33 * 512)
        stream = io.StringIO()
        with mock.patch.object(sys, 'argv', ['-c', str(2 * GiB)]), \
             mock.patch.object(subprocess, 'check_output', side_effect=read_command), \
             mock.patch.object(subprocess, 'run', side_effect=[record, resize]) as command, \
             mock.patch.object(Path, 'read_text', return_value='227328'), \
             mock.patch.object(rt.os, 'statvfs', return_value=SimpleNamespace(
                 f_blocks=500000, f_frsize=4096, f_bavail=450000)), \
             contextlib.redirect_stdout(stream), self.assertRaises(SystemExit) as exited:
            exec(compile(rt.GROW_GUEST_ROOT, '<guest-root-growth>', 'exec'), {})
        return exited.exception.code, json.loads(stream.getvalue()), command.call_args_list

    def test_root_expansion_and_repeat_nochange_both_succeed(self):
        for code, output in ((0, 'CHANGED: partition=1'), (1, 'NOCHANGE: partition 1 could not be grown')):
            with self.subTest(code=code):
                status, record, calls = self.execute(code, output)
                self.assertEqual(status, 0)
                self.assertEqual(record['status'], 'PASS')
                self.assertEqual([call.args[0] for call in calls],
                                 [['growpart', '/dev/vda', '1'], ['resize2fs', '/dev/vda1']])

    def test_small_disk_or_wrong_filesystem_refuses_guest_writes(self):
        for kwargs in ({'disk_bytes': GiB}, {'fstype': 'xfs'}):
            with self.subTest(kwargs=kwargs):
                status, record, calls = self.execute(**kwargs)
                self.assertEqual(status, 1)
                self.assertEqual(record['status'], 'FAIL')
                self.assertEqual(calls, [])

    def test_growpart_failure_is_not_confused_with_nochange(self):
        status, record, calls = self.execute(1, 'FAILED: unable to grow')
        self.assertEqual(status, 1)
        self.assertEqual(record['error'], 'growpart failed')
        self.assertEqual(len(calls), 1)

    def test_resize_failure_is_preserved(self):
        status, record, calls = self.execute(resize_returncode=1)
        self.assertEqual(status, 1)
        self.assertEqual(record['error'], 'resize2fs failed')
        self.assertEqual(len(calls), 2)


if __name__ == '__main__':
    unittest.main()
