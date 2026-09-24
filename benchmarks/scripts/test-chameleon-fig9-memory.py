#!/usr/bin/env python3
"""NUMA capacity/CPU placement regressions using synthetic host resources."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import chameleon_fig9 as data
import chameleon_fig9_runtime as rt

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('memory_fig9_runner', Path(__file__).with_name('run-chameleon-fig9.py'))
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def topology(counts=(24, 24)):
    return [{'cpu': n * 100 + c + sibling * 1000, 'node': n, 'socket': n, 'core': c}
            for n, count in enumerate(counts) for c in range(count) for sibling in range(2)]


def snapshot(gib=(125, 126)):
    return {n: {'total_mib': size * 1024, 'available_estimate_mib': size * 1024}
            for n, size in enumerate(gib)}


def inventory():
    return json.loads((ROOT / 'ae/config/host.example.json').read_text())


def apps(mix):
    config = json.loads((ROOT / 'ae/config/fig78-points.json').read_text())
    return [dict(application=case, **config['applications'][case]) for case in data.MIXES[mix]]


def place(mix, gib=(125, 126), counts=(24, 24), config=None):
    rows = topology(counts)
    return rt.allocate_resources(apps(mix), config or inventory(), snapshot(gib), rows, [r['cpu'] for r in rows])


class Placement(unittest.TestCase):
    def test_moves_whole_vm_to_larger_node_when_cpu_greedy_would_overfill(self):
        rows = topology()
        high = apps('mix1')
        before = copy.deepcopy(high)
        greedy = rt.allocate_cpus(high, 1, rows, [r['cpu'] for r in rows])
        self.assertEqual(greedy['applications']['graphchi']['host_numa_node'], 0)
        result = place('mix1', gib=(110, 148))
        self.assertEqual({k: v['host_numa_node'] for k, v in result['cpus']['applications'].items()},
                         {'memcached': 0, 'graphchi': 1, 'xsbench': 1})
        self.assertEqual(result['numa_memory']['nodes'][1]['vm_memory_mib'], 128 * 1024)
        self.assertEqual(result['cpus']['physical_isolation']['status'], 'PASS')
        slot = inventory()['slots'][1]
        config = rt.slot_config({'disk': '/synthetic/template', 'disk_format': 'qcow2'}, slot,
                                high[1], result['cpus']['applications']['graphchi'])
        self.assertEqual(config['host_numa_node'], 1)
        self.assertEqual(config['memory_mib'], 48 * 1024)
        self.assertEqual(high, before)

    def test_current_host_rejects_mix1_and_mix4_but_accepts_mix2_and_mix3(self):
        for mix in ('mix1', 'mix4'):
            with self.subTest(mix=mix), self.assertRaisesRegex(ValueError, 'NUMA memory') as raised:
                place(mix)
            self.assertIn('budget', str(raised.exception))
            if mix == 'mix4':
                self.assertIn('needs 16 VM physical cores, has 8', str(raised.exception))
        for mix in ('mix2', 'mix3'):
            with self.subTest(mix=mix):
                result = place(mix)
                self.assertTrue(all(row['spare_mib'] >= 0 for row in result['numa_memory']['nodes'].values()))
                self.assertEqual(result['numa_memory']['nodes'][0]['vm_memory_mib'], 104 * 1024)

    def test_cassandra_ycsb_remain_separate_after_moving_graph500(self):
        result = place('mix4', gib=(110, 148), counts=(24, 32))
        self.assertEqual({k: v['host_numa_node'] for k, v in result['cpus']['applications'].items()},
                         {'cassandra': 0, 'graph500': 1, 'xsbench': 1})
        self.assertEqual(result['cpus']['client_numa_node'], 1)
        self.assertEqual(len(result['cpus']['client_cpus']), 16)
        self.assertEqual(result['cpus']['physical_isolation']['status'], 'PASS')

    def test_client_and_qemu_reserves_can_make_raw_ram_fit_infeasible(self):
        config = inventory()
        config['numa_memory'] = {'host_reserve_mib': 0, 'client_reserve_mib': 0, 'per_vm_overhead_mib': 0}
        place('mix1', gib=(80, 128), config=config)
        for key in config['numa_memory']:
            changed = copy.deepcopy(config)
            changed['numa_memory'][key] = 1024
            with self.subTest(key=key), self.assertRaises(ValueError):
                place('mix1', gib=(80, 128), config=changed)

    def test_service_client_separation_is_not_relaxed_to_fit_memory(self):
        with self.assertRaisesRegex(ValueError, 'different nodes'):
            # Only node1 can hold the service, but the configured client owns node1.
            place('mix2', gib=(40, 256))
        config = inventory(); config['client_numa_node'] = 0
        result = place('mix2', gib=(60, 256), config=config)
        self.assertEqual(result['cpus']['applications']['memcached']['host_numa_node'], 1)

    def test_build_plan_reports_all_infeasible_mixes_before_any_vm_start(self):
        rows = topology()
        config = inventory()
        config['fig9_execution'] = {'mix1': 'three-concurrent', 'mix4': 'three-concurrent'}
        with mock.patch.object(rt, 'numa_memory_snapshot', return_value=snapshot()), \
             mock.patch.object(rt.affinity, 'topology', return_value=rows), \
             mock.patch.object(rt.os, 'sched_getaffinity', return_value={r['cpu'] for r in rows}), \
             mock.patch.object(rt.guest, 'start') as start:
            with self.assertRaises(ValueError) as raised:
                runner.build_plan(config, list(data.MIXES), ROOT / 'ae/config/fig9-highs.json',
                                  ROOT / 'ae/results_baselines/fig9.json')
        self.assertIn('mix1:', str(raised.exception))
        self.assertIn('mix4:', str(raised.exception))
        start.assert_not_called()


class Pressure(unittest.TestCase):
    def test_preflight_detects_busy_node_even_with_enough_global_memory(self):
        result = place('mix2')
        memory = snapshot()
        memory[0]['available_estimate_mib'] = 110 * 1024
        with self.assertRaisesRegex(ValueError, 'NUMA node0 needs'):
            rt.check_memory_available(apps('mix2'), result['cpus'], inventory(), memory, 220 * 1024)

    def test_recheck_does_not_double_count_already_booted_vm(self):
        result = place('mix2')
        memory = snapshot()
        memory[0]['available_estimate_mib'] -= 73 * 1024
        evidence = rt.check_memory_available(apps('mix2')[1:], result['cpus'], inventory(), memory, 170 * 1024)
        self.assertEqual(evidence['nodes'][0]['required_mib'], (32 + 1 + 8) * 1024)
        self.assertEqual(evidence['nodes'][1]['required_mib'], (48 + 1 + 8 + 4) * 1024)

    def test_keeps_host_headroom_on_node_with_only_already_booted_vms(self):
        result = place('mix1', gib=(110, 148))
        memory = snapshot((110, 148))
        memory[0]['available_estimate_mib'] = 7 * 1024
        with self.assertRaisesRegex(ValueError, 'NUMA node0 needs 8192MiB'):
            rt.check_memory_available(apps('mix1')[1:], result['cpus'], inventory(), memory, 155 * 1024)

    def test_runtime_pressure_aborts_before_boot_or_slot_config_changes(self):
        result = place('mix2')
        memory = snapshot()
        memory[0]['available_estimate_mib'] = 90 * 1024
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(rt, 'numa_memory_snapshot', return_value=memory), \
             mock.patch.object(rt, 'access') as access, \
             mock.patch.object(rt.guest, 'start') as start:
            with self.assertRaisesRegex(ValueError, 'NUMA node0 needs'):
                runner.run_mix('mix2', 1, {'applications': apps('mix2'), **result}, inventory(), {},
                               Path(tmp), 'memory-test', 60)
            start.assert_not_called()
            access.assert_not_called()
            record = json.loads((Path(tmp) / 'mix2/repeat-01/report.json').read_text())
            self.assertEqual(record['status'], 'FAIL')
            self.assertFalse((Path(tmp) / 'mix2/repeat-01/barrier/RELEASE').exists())

    def test_global_memavailable_still_limits_optimistic_node_estimates(self):
        result = place('mix2')
        with self.assertRaisesRegex(ValueError, 'Host needs'):
            rt.check_memory_available(apps('mix2'), result['cpus'], inventory(), snapshot(), 140 * 1024)

    def test_local_pools_charged_only_before_they_are_started(self):
        result = place('mix2')
        config = inventory(); config['server']['manage_local'] = True
        before = rt.check_memory_available(apps('mix2'), result['cpus'], config, snapshot(), 240 * 1024, True)
        after = rt.check_memory_available(apps('mix2'), result['cpus'], config, snapshot(), 240 * 1024)
        self.assertEqual(before['host_required_mib'] - after['host_required_mib'], 24 * 1024)

    def test_node_parser_deducts_shmem_dirty_and_writeback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'node3').mkdir()
            fields = {'MemTotal': 100, 'MemFree': 10, 'FilePages': 30, 'SReclaimable': 5,
                      'Shmem': 6, 'Dirty': 2, 'Writeback': 1}
            (root / 'node3/meminfo').write_text('\n'.join(f'Node 3 {k}: {v * 1024} kB' for k, v in fields.items()))
            self.assertEqual(rt.numa_memory_snapshot(root), {3: {'total_mib': 100, 'available_estimate_mib': 36}})

    def test_invalid_reserves_are_rejected(self):
        for policy in ([], {'host_reserve_mib': -1}, {'client_reserve_mib': True}, {'vm_reserve_mib': 1024}):
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                rt.memory_reserves({'numa_memory': policy})


if __name__ == '__main__':
    unittest.main()
