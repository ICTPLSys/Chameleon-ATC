#!/usr/bin/env python3
"""Physical-core isolation and live client/QEMU affinity verification tests."""
import copy
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import chameleon_affinity as affinity
import chameleon_fig9 as data
import chameleon_fig9_runtime as rt

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('fig9_cpu_runner', HERE / 'run-chameleon-fig9.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def topology():
    return [{'cpu': node * 40 + core + sibling * 80, 'socket': node, 'node': node, 'core': core}
            for node in range(2) for core in range(40) for sibling in range(2)]


def placement():
    return rt.allocate_cpus([data.resolve_high(case) for case in data.MIXES['mix1']], 1,
                            topology(), set(range(160)))


class PhysicalGroups(unittest.TestCase):
    def test_saved_mix_assignments_include_disjoint_clients_and_vm_service_cores(self):
        for mix in data.MIXES:
            current = rt.allocate_cpus([data.resolve_high(case) for case in data.MIXES[mix]], 1,
                                      topology(), set(range(160)))
            self.assertEqual(current['physical_isolation']['status'], 'PASS')
            self.assertEqual(len(current['physical_isolation']['groups']), 7)
            self.assertEqual(rt.validate_cpu_plan(current, topology()), current['physical_isolation'])

    def test_cross_vm_smt_and_client_overlap_rejected(self):
        for target in ('other-vm', 'client'):
            plan = placement()
            first, second = list(plan['applications'])[:2]
            cpu = plan['applications'][first]['vcpu_host_cpus'][0]
            if target == 'other-vm':
                plan['applications'][second]['vcpu_host_cpus'][0] = cpu + 80
                plan['applications'][second]['host_numa_node'] = 0
            else:
                # Keep node validation valid; collide a batch VM with the
                # dedicated Host generator core using its SMT sibling.
                case = 'xsbench'
                cpus = plan['client_cpus'][:6]
                plan['applications'][case]['host_numa_node'] = 1
                plan['applications'][case]['vcpu_host_cpus'] = [c + 80 for c in cpus[:4]]
                plan['applications'][case]['qemu_service_cpus'] = [c + 80 for c in cpus[4:6]]
            with self.subTest(target=target), self.assertRaisesRegex(ValueError, 'Physical core'):
                rt.validate_cpu_plan(plan, topology())

    def test_client_cpu_parser_rejects_two_smt_siblings(self):
        with self.assertRaisesRegex(ValueError, 'SMT'):
            affinity.validate_client_cpus('40,120', 1, topology(), range(160))

    def test_full_thread_masks_checked_including_jvm_helpers(self):
        for masks in ({11: [40, 41], 12: [41]}, {11: [40], 12: [41], 13: [0]}):
            with mock.patch.object(affinity, 'snapshot', return_value=masks):
                if 13 in masks:
                    with self.assertRaisesRegex(RuntimeError, 'escaped'):
                        affinity.verify_process_masks([11], [40, 41])
                else:
                    self.assertEqual(affinity.verify_process_masks([11], [40, 41]), {'11': masks})


class RuntimeVMChecks(unittest.TestCase):
    def test_parent_pin_means_leaf_restore_keeps_exact_masks(self):
        profile = {'vcpu_host_cpus': [0, 1], 'qemu_service_cpus': [2, 3]}
        cpus = [{'cpu-index': 0, 'thread-id': 101}, {'cpu-index': 1, 'thread-id': 102}]
        masks = {100: [0, 1, 2, 3], 101: [0, 1, 2, 3], 102: [0, 1, 2, 3]}
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'qemu.pid').write_text('100')
            with mock.patch.object(rt.guest, 'run_dir', return_value=Path(tmp)), \
                 mock.patch.object(rt.guest, 'qmp', return_value=cpus), \
                 mock.patch.object(affinity, 'snapshot', side_effect=lambda pid: copy.deepcopy(masks)), \
                 mock.patch.object(affinity.os, 'sched_setaffinity', side_effect=lambda tid, selected: masks.__setitem__(tid, list(selected))):
                boot = rt.pin_guest_cpus({}, profile)
                self.assertEqual(boot['status'], 'PASS')
                leaf_before, _ = affinity.apply(100, cpus, profile)
                affinity.restore(100, leaf_before)
                self.assertEqual(masks, {100: [2, 3], 101: [0], 102: [1]})
                self.assertEqual(affinity.verify(100, cpus, profile)['status'], 'PASS')

    def test_all_three_actual_qemu_profiles_checked_and_drift_fails(self):
        plan = placement()
        with tempfile.TemporaryDirectory() as tmp:
            accesses = {}
            for i, case in enumerate(plan['applications']):
                directory = Path(tmp) / case
                directory.mkdir()
                (directory / 'qemu.pid').write_text(str(100 + i))
                accesses[case] = {'directory': directory}
            with mock.patch.object(rt.guest, 'run_dir', side_effect=lambda a: a['directory']), \
                 mock.patch.object(rt.guest, 'qmp', return_value=[]), \
                 mock.patch.object(rt.affinity, 'verify', return_value={'status': 'PASS', 'threads': {}}) as verify:
                self.assertEqual(rt.verify_vm_cpu_isolation(plan, accesses, topology())['status'], 'PASS')
                self.assertEqual(verify.call_count, 3)
                verify.side_effect = [dict(status='PASS'), RuntimeError('thread drift')]
                with self.assertRaisesRegex(RuntimeError, 'thread drift'):
                    rt.verify_vm_cpu_isolation(plan, accesses, topology())
            with self.assertRaisesRegex(ValueError, 'Every planned VM'):
                rt.verify_vm_cpu_isolation(plan, {}, topology())

    def test_barrier_refuses_release_when_actual_cpu_check_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            barrier = Path(tmp)
            children = {case: mock.Mock(poll=mock.Mock(return_value=None)) for case in data.MIXES['mix1']}
            for case in children:
                (barrier / (case + '.ready.json')).write_text('{}')
            with self.assertRaisesRegex(RuntimeError, 'CPU collision'):
                runner.wait_ready(barrier, children, 10, cpu_check=mock.Mock(side_effect=RuntimeError('CPU collision')))
            self.assertFalse((barrier / 'RELEASE').exists())


class LiveClients(unittest.TestCase):
    def physical_cpus(self, minimum=2):
        allowed = os.sched_getaffinity(0)
        physical = {}
        for row in affinity.topology():
            if row['cpu'] in allowed:
                physical.setdefault((row['socket'], row['core']), row['cpu'])
        cpus = list(physical.values())
        if len(cpus) < minimum:
            self.skipTest('Requires distinct allowed physical cores')
        return cpus

    def test_real_multithread_child_and_descendant_masks_are_recorded(self):
        cpus = self.physical_cpus()[:2]
        original = os.sched_getaffinity(0)
        code = ('import subprocess,sys,threading,time\n'
                'p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(.25)"])\n'
                'threads=[threading.Thread(target=time.sleep,args=(.25,)) for _ in range(3)]\n'
                '[t.start() for t in threads]\n[t.join() for t in threads]\np.wait()\n')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'affinity.json'
            self.assertEqual(affinity.run_confined([sys.executable, '-c', code], cpus, path, interval=.02), 0)
            result = json.loads(path.read_text())
            self.assertEqual(result['status'], 'PASS')
            self.assertGreaterEqual(len(result['observed_pids']), 2)
            self.assertGreaterEqual(result['maximum_threads'], 5)
            snapshots = [json.loads(line) for line in path.with_suffix('.samples.jsonl').read_text().splitlines()]
            self.assertTrue(all(set(mask) <= set(cpus) for row in snapshots for threads in row['process_threads'].values() for mask in threads.values()))
        self.assertEqual(os.sched_getaffinity(0), original)

    def test_real_child_affinity_escape_fails_and_owned_process_is_reaped(self):
        cpus = self.physical_cpus()
        original = os.sched_getaffinity(0)
        code = f'import os,time;time.sleep(.05);os.sched_setaffinity(0,{{{cpus[1]}}});time.sleep(5)'
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'affinity.json'
            with self.assertRaisesRegex(RuntimeError, 'escaped'):
                affinity.run_confined([sys.executable, '-c', code], cpus[:1], path, interval=.02)
            result = json.loads(path.read_text())
            self.assertEqual(result['status'], 'FAIL')
            with self.assertRaises(ProcessLookupError):
                os.kill(result['pid'], 0)
        self.assertEqual(os.sched_getaffinity(0), original)

    def test_process_group_parser_handles_spaces_and_parentheses_in_comm(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp)
            for pid, content in {100: '100 (name ) extra) S 1 999 0 0',
                                 101: '101 (worker) S 1 888 0 0',
                                 102: '102 (zombie) Z 1 999 0 0'}.items():
                (proc / str(pid)).mkdir()
                (proc / str(pid) / 'stat').write_text(content)
            self.assertEqual(affinity.process_group_members(999, proc), [100])


if __name__ == '__main__':
    unittest.main()
