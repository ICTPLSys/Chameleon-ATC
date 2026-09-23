#!/usr/bin/env python3
"""VF selection, bind/rollback and recovery tests on a fake sysfs only."""
from contextlib import ExitStack, redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    'hermit_vfio', Path(__file__).resolve().parent.parent / 'scripts/hermit-vfio.py')
VFIO = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VFIO)
PF = '0000:69:00.0'
VFS = ['0000:69:00.2', '0000:69:00.3']


class VFIOTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.sys = self.root / 'sys'
        self.proc = self.root / 'proc'
        self.dev = self.root / 'dev'
        self.state = self.root / 'state/vfio.json'
        for name, path in [('SYS', self.sys), ('PROC', self.proc), ('DEV', self.dev)]:
            self.stack.enter_context(patch.object(VFIO, name, path))
        self.run = self.stack.enter_context(patch.object(VFIO.subprocess, 'run'))
        self.stack.enter_context(patch.object(VFIO.os, 'geteuid', return_value=0))
        self.stack.enter_context(patch.object(VFIO.time, 'sleep'))
        self.put(self.proc / 'sys/kernel/random/boot_id', 'test-boot')
        for name in ('mlx5_core', 'vfio-pci', 'other'):
            self.put(self.sys / 'bus/pci/drivers' / name / 'unbind', '')
        self.put(self.sys / 'bus/pci/drivers_probe', '')
        self.make_device(PF, None, 1)
        self.put(VFIO.device(PF) / 'sriov_totalvfs', '16')
        self.put(VFIO.device(PF) / 'sriov_numvfs', '2')
        for index, address in enumerate(VFS):
            path = self.make_device(address, PF, 20 + index)
            (VFIO.device(PF) / f'virtfn{index}').symlink_to(path)
        self.real_write = VFIO.write
        self.writes = []
        self.fail_probe = None
        self.fail_recovery = False
        self.write = self.stack.enter_context(patch.object(VFIO, 'write', side_effect=self.sysfs_write))

    def put(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value + '\n')

    def make_device(self, address, pf, group, driver='mlx5_core'):
        path = self.sys / 'bus/pci/devices' / address
        for name, value in [('vendor', '0x15b3'), ('device', '0x1018'),
                            ('subsystem_vendor', '0x15b3'), ('subsystem_device', '0x0008'),
                            ('driver_override', '(null)')]:
            self.put(path / name, value)
        if driver:
            (path / 'driver').symlink_to(self.sys / 'bus/pci/drivers' / driver)
        if pf:
            (path / 'physfn').symlink_to(self.sys / 'bus/pci/devices' / pf)
        directory = self.sys / 'kernel/iommu_groups' / str(group)
        (directory / 'devices').mkdir(parents=True, exist_ok=True)
        (directory / 'devices' / address).symlink_to(path)
        (path / 'iommu_group').symlink_to(directory)
        return path

    def sysfs_write(self, path, value):
        self.writes.append((str(path), value))
        if path.name == 'unbind':
            address = str(value)
            if self.fail_recovery and VFIO.driver(VFIO.device(address)) == 'vfio-pci':
                raise OSError('injected rollback failure')
            (VFIO.device(address) / 'driver').unlink()
        elif path.name == 'drivers_probe':
            address = str(value)
            target = VFIO.override(VFIO.device(address))
            if self.fail_probe == address and target == 'vfio-pci':
                raise OSError('injected probe failure')
            (VFIO.device(address) / 'driver').symlink_to(self.sys / 'bus/pci/drivers' / target)
        else:
            self.real_write(path, value)

    def main(self, *args):
        capture = io.StringIO()
        with redirect_stdout(capture):
            VFIO.main(list(args))
        return json.loads(capture.getvalue())

    def bind(self):
        return VFIO.apply_bind(PF, VFS, self.state)

    def test_default_bind_only_plans_without_writes_state_or_modprobe(self):
        result = self.main('bind', '--pf', PF, '--vf', VFS[0], '--state', str(self.state))
        self.assertFalse(result['applied'])
        self.assertEqual(result['result']['devices'][0]['bdf'], VFS[0])
        self.assertFalse(self.state.parent.exists())
        self.write.assert_not_called()
        self.run.assert_not_called()

    def test_inventory_reports_pf_vf_and_group_without_writes(self):
        result = self.main('inspect', '--pf', PF)
        self.assertEqual({item['bdf'] for item in result['result']}, {PF, *VFS})
        self.assertEqual(result['result'][1]['pf'], PF)
        self.write.assert_not_called()
        self.run.assert_not_called()

    def test_pf_cannot_be_selected_for_unbinding(self):
        with self.assertRaisesRegex(RuntimeError, 'not a VF'):
            VFIO.bind_plan(PF, [PF])
        self.write.assert_not_called()

    def test_vf_of_another_pf_is_rejected(self):
        other = self.make_device('0000:70:00.1', '0000:70:00.0', 40)
        # The fake parent must exist for the symlink, just like real sysfs.
        self.make_device('0000:70:00.0', None, 39)
        with self.assertRaisesRegex(RuntimeError, 'not a VF'):
            VFIO.bind_plan(PF, [other.name])

    def test_iommu_group_neighbour_must_be_explicitly_selected(self):
        path = VFIO.device(VFS[1])
        (path / 'iommu_group').unlink()
        old = self.sys / 'kernel/iommu_groups/21/devices' / VFS[1]
        old.unlink()
        group = self.sys / 'kernel/iommu_groups/20'
        (path / 'iommu_group').symlink_to(group)
        (group / 'devices' / VFS[1]).symlink_to(path)
        with self.assertRaisesRegex(RuntimeError, 'unselected'):
            VFIO.bind_plan(PF, [VFS[0]])
        self.assertEqual(len(VFIO.bind_plan(PF, VFS)['devices']), 2)

    def test_no_iommu_group_rejected(self):
        (VFIO.device(VFS[0]) / 'iommu_group').unlink()
        with self.assertRaisesRegex(RuntimeError, 'no IOMMU group'):
            VFIO.bind_plan(PF, [VFS[0]])

    def test_active_vf_network_refuses_binding_without_touching_pf(self):
        self.put(VFIO.device(VFS[0]) / 'net/vf0/flags', '0x1003')
        with self.assertRaisesRegex(RuntimeError, 'active network'):
            VFIO.bind_plan(PF, VFS)
        self.assertEqual(VFIO.driver(VFIO.device(PF)), 'mlx5_core')
        self.write.assert_not_called()

    def test_open_rdma_or_vfio_device_refuses_binding_and_restore(self):
        with patch.object(VFIO, 'node_users', return_value=[4242]):
            with self.assertRaisesRegex(RuntimeError, '4242'):
                VFIO.bind_plan(PF, VFS)
        saved = VFIO.bind_plan(PF, VFS)
        with patch.object(VFIO, 'node_users', return_value=[4242]):
            with self.assertRaisesRegex(RuntimeError, '4242'):
                VFIO.restore_devices(saved)
        self.write.assert_not_called()

    def test_open_fd_inventory_matches_device_number_not_filename(self):
        node = self.dev / 'infiniband/uverbs0'
        node.parent.mkdir(parents=True)
        node.symlink_to('/dev/null')
        descriptor = self.proc / '42/fd/7'
        descriptor.parent.mkdir(parents=True)
        descriptor.symlink_to('/dev/null')
        # The pathname can differ; kernel character-device identity must match.
        self.assertEqual(VFIO.node_users([node]), [42])
        descriptor.unlink()
        descriptor.symlink_to('/dev/zero')
        self.assertEqual(VFIO.node_users([node]), [])

    def test_bind_and_restore_preserve_original_override_and_pf(self):
        self.put(VFIO.device(VFS[1]) / 'driver_override', 'mlx5_core')
        saved = self.bind()
        self.assertEqual(saved['status'], 'active')
        self.assertEqual([VFIO.driver(VFIO.device(v)) for v in VFS], ['vfio-pci'] * 2)
        result = self.main('restore', '--state', str(self.state), '--apply')
        self.assertEqual(result['result']['status'], 'restored')
        self.assertEqual([VFIO.driver(VFIO.device(v)) for v in VFS], ['mlx5_core'] * 2)
        self.assertEqual([VFIO.override(VFIO.device(v)) for v in VFS], ['', 'mlx5_core'])
        self.assertFalse(any(value == PF for _, value in self.writes))

    def test_second_vf_probe_failure_rolls_back_first_and_unbound_second(self):
        self.fail_probe = VFS[1]
        with self.assertRaisesRegex(OSError, 'probe failure'):
            self.bind()
        self.assertEqual([VFIO.driver(VFIO.device(v)) for v in VFS], ['mlx5_core'] * 2)
        self.assertEqual([VFIO.override(VFIO.device(v)) for v in VFS], ['', ''])
        self.assertEqual(json.loads(self.state.read_text())['status'], 'rolled-back')

    def test_failed_rollback_keeps_journal_for_recovering_partial_bind(self):
        self.fail_probe = VFS[1]
        self.fail_recovery = True
        with self.assertRaisesRegex(RuntimeError, 'Recovery state retained'):
            self.bind()
        self.assertEqual(json.loads(self.state.read_text())['status'], 'prepared')
        self.fail_probe = None
        self.fail_recovery = False
        self.main('restore', '--state', str(self.state), '--apply')
        self.assertEqual([VFIO.driver(VFIO.device(v)) for v in VFS], ['mlx5_core'] * 2)

    def test_existing_active_journal_is_not_overwritten(self):
        saved = self.bind()
        with self.assertRaisesRegex(RuntimeError, 'Existing recovery state'):
            self.bind()
        self.assertEqual(json.loads(self.state.read_text()), saved)

    def test_restore_different_boot_or_device_identity_refuses_mutation(self):
        saved = VFIO.bind_plan(PF, VFS)
        self.put(self.proc / 'sys/kernel/random/boot_id', 'another-boot')
        with self.assertRaisesRegex(RuntimeError, 'another boot'):
            VFIO.restore_devices(saved)
        self.put(self.proc / 'sys/kernel/random/boot_id', 'test-boot')
        self.put(VFIO.device(VFS[0]) / 'device', '0xffff')
        with self.assertRaisesRegex(RuntimeError, 'identity'):
            VFIO.restore_devices(saved)
        self.write.assert_not_called()

    def test_restore_unrelated_driver_refuses_mutation(self):
        saved = VFIO.bind_plan(PF, VFS)
        link = VFIO.device(VFS[0]) / 'driver'
        link.unlink()
        link.symlink_to(self.sys / 'bus/pci/drivers/other')
        with self.assertRaisesRegex(RuntimeError, 'unexpected current driver'):
            VFIO.restore_devices(saved)
        self.write.assert_not_called()

    def test_originally_unbound_vf_restores_to_unbound(self):
        (VFIO.device(VFS[0]) / 'driver').unlink()
        self.bind()
        self.main('restore', '--state', str(self.state), '--apply')
        self.assertIsNone(VFIO.driver(VFIO.device(VFS[0])))
        self.assertEqual(VFIO.override(VFIO.device(VFS[0])), '')

    def test_enable_existing_count_noop_and_other_count_rejected(self):
        self.assertIsNone(VFIO.enable_plan(PF, 2)['write'])
        for count in (0, 1, 3, 17):
            with self.assertRaises(RuntimeError):
                VFIO.enable_plan(PF, count)
        self.main('enable-vfs', '--pf', PF, '--count', '2', '--apply')
        self.write.assert_not_called()

    def test_enable_zero_population_only_writes_selected_pf_count(self):
        self.put(VFIO.device(PF) / 'sriov_numvfs', '0')
        result = self.main('enable-vfs', '--pf', PF, '--count', '4')
        self.assertFalse(result['applied'])
        self.write.assert_not_called()
        self.main('enable-vfs', '--pf', PF, '--count', '4', '--apply')
        self.assertEqual(self.writes, [(str(VFIO.device(PF) / 'sriov_numvfs'), 4)])
        self.run.assert_not_called()

    def test_enabling_vfs_preserves_a_busy_pf_and_allows_existing_noop(self):
        self.put(VFIO.device(PF) / 'net/pf0/flags', '0x1003')
        self.assertIsNone(VFIO.enable_plan(PF, 2)['write'])
        self.put(VFIO.device(PF) / 'sriov_numvfs', '0')
        with self.assertRaisesRegex(RuntimeError, 'active network'):
            self.main('enable-vfs', '--pf', PF, '--count', '2', '--apply')
        self.assertEqual(VFIO.read(VFIO.device(PF) / 'sriov_numvfs'), '0')
        self.assertEqual(VFIO.driver(VFIO.device(PF)), 'mlx5_core')
        self.write.assert_not_called()

    def test_apply_requires_root_but_inspect_does_not(self):
        with patch.object(VFIO.os, 'geteuid', return_value=1000):
            self.main('inspect', '--pf', PF)
            with self.assertRaisesRegex(RuntimeError, 'requires root'):
                self.main('bind', '--pf', PF, '--vf', VFS[0], '--apply')
        self.write.assert_not_called()

    def test_dma_capacity_dry_run_and_apply_only_increase_the_limit(self):
        path = self.sys / 'module/vfio_iommu_type1/parameters/dma_entry_limit'
        self.put(path, '65535')
        result = self.main('configure-vfio', '--memory-mib', '8192')
        self.assertEqual(result['result']['target_entries'], 8192 * 256 + 4096)
        self.write.assert_not_called()
        self.run.assert_not_called()
        self.main('configure-vfio', '--memory-mib', '8192', '--apply')
        self.assertEqual(int(VFIO.read(path)), 8192 * 256 + 4096)
        self.write.reset_mock()
        self.main('configure-vfio', '--memory-mib', '2048', '--apply')
        self.write.assert_not_called()

    def test_dma_capacity_before_module_load_and_invalid_sizes(self):
        result = self.main('configure-vfio', '--memory-mib', '2048')
        self.assertIsNone(result['result']['current_entries'])
        self.assertEqual(result['result']['required_entries'], 2048 * 256 + 4096)
        for memory, reserve in [(0, 0), (2048, -1), (16777216, 1)]:
            with self.assertRaises(RuntimeError):
                VFIO.vfio_limit_plan(memory, reserve)
        self.write.assert_not_called()
        self.run.assert_not_called()

    def test_configure_ib_vf_uses_exact_index_and_rolls_back_partial_failure(self):
        directory = VFIO.device(PF) / 'sriov/1'
        self.put(directory / 'node', '00:11:22:33:44:55:66:01')
        self.put(directory / 'port', '00:11:22:33:44:55:66:02')
        self.put(directory / 'policy', 'Down')
        args = ['configure-vf', '--pf', PF, '--vf-index', '1',
                '--node-guid', '11:22:33:44:55:66:77:01', '--policy', 'Follow']
        result = self.main(*args)
        self.assertEqual(result['result']['vf'], VFS[1])
        self.write.assert_not_called()
        def inject(path, value):
            if path.name == 'policy' and value == 'Follow':
                raise OSError('policy rejected')
            self.sysfs_write(path, value)
        self.write.side_effect = inject
        with self.assertRaisesRegex(OSError, 'policy rejected'):
            self.main(*args, '--apply')
        self.assertEqual(VFIO.read(directory / 'node'), '00:11:22:33:44:55:66:01')
        self.assertEqual(VFIO.read(directory / 'policy'), 'Down')


if __name__ == '__main__':
    unittest.main(verbosity=2)
