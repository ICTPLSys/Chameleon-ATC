#!/usr/bin/env python3
import importlib.util
from pathlib import Path
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location('prepare', Path(__file__).with_name('prepare-chameleon-fig9.py'))
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)


class Prepare(unittest.TestCase):
    def test_existing_guid_preserved_new_guids_unique(self):
        old = {'node_guid': '7c:8c:09:03:00:bf:34:3a', 'port_guid': '7c:8c:09:03:00:bf:34:3a', 'link_state': 'auto'}
        result = p.guid_plan({'pf': '0000:69:00.0', 'count': 1, 'devices': [old]})
        self.assertEqual(result[0], old)
        self.assertEqual(len({x['port_guid'] for x in result}), 3)
        for row in result:
            p.vf.guid(row['port_guid'])

    def test_population_unbinds_vfs_only_then_cycles_zero(self):
        parent, child = Path('/synthetic/pf'), Path('/synthetic/vf')
        calls = []
        with mock.patch.object(p.vf, 'read', side_effect=['1', '3']), \
             mock.patch.object(p, 'require_idle', side_effect=lambda x: calls.append(('idle', x))), \
             mock.patch.object(p, 'children', side_effect=[[child], [child] * 3]), \
             mock.patch.object(p.vf, 'rebind', side_effect=lambda *x: calls.append(('rebind', *x))), \
             mock.patch.object(p.vf, 'write', side_effect=lambda *x: calls.append(('write', *x))):
            p.population(parent, 3)
        self.assertEqual(calls, [('idle', parent), ('rebind', child, 'mlx5_core', ''),
                                 ('write', parent / 'sriov_numvfs', 0),
                                 ('write', parent / 'sriov_numvfs', 3)])

    def test_busy_population_never_writes(self):
        with mock.patch.object(p.vf, 'read', return_value='1'), \
             mock.patch.object(p, 'require_idle', side_effect=RuntimeError('VM still active')), \
             mock.patch.object(p.vf, 'write') as write:
            with self.assertRaisesRegex(RuntimeError, 'VM still active'):
                p.population(Path('/synthetic/pf'), 3)
            write.assert_not_called()

    def test_configure_verifies_netlink_readback(self):
        row = {'vf': 1, 'node guid': 'n', 'port guid': 'p', 'link_state': 'auto'}
        with mock.patch.object(p, 'command') as command, mock.patch.object(p, 'link', return_value={'vfinfo_list': [row]}):
            p.configure('ibtest', 1, 'n', 'p')
            self.assertEqual(command.call_count, 3)
            with self.assertRaisesRegex(RuntimeError, 'readback'):
                p.configure('ibtest', 1, 'n', 'different')

    def test_restore_rejects_different_boot_without_changes(self):
        with mock.patch.object(p.vf, 'read', return_value='new'), mock.patch.object(p, 'population') as population:
            with self.assertRaisesRegex(ValueError, 'another boot'):
                p.restore({'boot_id': 'old'})
            population.assert_not_called()


if __name__ == '__main__':
    unittest.main()
