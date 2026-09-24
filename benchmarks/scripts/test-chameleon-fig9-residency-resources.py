#!/usr/bin/env python3
"""NUMA constraints for retaining an already running Fig9 Guest."""
import copy
import json
from pathlib import Path
import unittest

import chameleon_fig9 as data
import chameleon_fig9_runtime as rt

ROOT = Path(__file__).resolve().parents[2]


class RetainedGuestResources(unittest.TestCase):
    def setUp(self):
        self.inventory = json.loads((ROOT / 'ae/config/host.example.json').read_text())
        points = json.loads((ROOT / 'ae/config/fig78-points.json').read_text())['applications']
        self.apps = {case: dict(application=case, **app) for case, app in points.items()}
        self.topology = [{'cpu': n * 24 + c, 'socket': n, 'core': c, 'node': n}
                         for n in range(2) for c in range(24)]
        self.allowed = list(range(48))
        self.snapshot = {n: {'total_mib': size, 'available_estimate_mib': size}
                         for n, size in enumerate((128564, 129005))}

    def allocate(self, cases, fixed_nodes=None, snapshot=None):
        return rt.allocate_resources([self.apps[case] for case in cases], self.inventory,
                                     snapshot or self.snapshot, self.topology, self.allowed,
                                     fixed_nodes=fixed_nodes)

    def test_pinned_retained_guest_prevents_optimizer_from_moving_it(self):
        cases = ['graphchi', 'xsbench']
        unrestricted = self.allocate(cases)
        old_node = 1 - unrestricted['cpus']['applications']['graphchi']['host_numa_node']
        placement = self.allocate(cases, {'graphchi': old_node})
        self.assertEqual(placement['cpus']['applications']['graphchi']['host_numa_node'], old_node)
        self.assertEqual(placement['cpus']['physical_isolation']['status'], 'PASS')

    def test_retained_node_capacity_failure_cannot_move_guest_to_fit(self):
        memory = copy.deepcopy(self.snapshot)
        memory[1] = {'total_mib': 40 * 1024, 'available_estimate_mib': 40 * 1024}
        self.allocate(['graphchi', 'graph500'], snapshot=memory)
        with self.assertRaisesRegex(ValueError, 'NUMA memory'):
            self.allocate(['graphchi', 'graph500'], {'graphchi': 1}, memory)

    def test_fixed_node_cannot_override_service_client_separation(self):
        with self.assertRaisesRegex(ValueError, 'different nodes'):
            self.allocate(['memcached', 'graphchi'], {'memcached': 1})

    def test_invalid_fixed_node_constraints_are_rejected(self):
        for fixed in ({'missing': 0}, {'graphchi': True}, {'graphchi': -1}, [0, 1]):
            with self.subTest(fixed=fixed), self.assertRaisesRegex(ValueError, 'fixed_nodes'):
                self.allocate(['graphchi', 'graph500'], fixed)

    def test_current_capacity_fits_both_resident_pairs_without_changing_apps(self):
        for mix, cases in data.MIXES.items():
            apps = [self.apps[case] for case in cases]
            before = copy.deepcopy(apps)
            plan = rt.plan_mix_resources(mix, apps, self.inventory, self.snapshot,
                                         self.topology, self.allowed)
            self.assertEqual(apps, before)
            for resident in plan['residency']:
                self.assertTrue(all(row['spare_mib'] >= 0
                                    for row in resident['numa_memory']['nodes'].values()))
                self.assertEqual(resident['cpus']['physical_isolation']['status'], 'PASS')
                for app in resident['applications']:
                    profile = resident['cpus']['applications'][app['application']]
                    self.assertEqual(len(profile['vcpu_host_cpus']), app['workload_configuration']['vcpus'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
