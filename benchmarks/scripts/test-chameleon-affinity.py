#!/usr/bin/env python3
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
import chameleon_affinity as a


class Affinity(unittest.TestCase):
    def test_node_check_accepts_pinned_subset_and_rejects_cross_node(self):
        rows=[{'cpu':0,'node':0},{'cpu':1,'node':0},{'cpu':2,'node':1}]
        with patch.object(a,'topology',return_value=rows):
            with patch.object(a,'snapshot',return_value={99:[0],100:[1]}):
                self.assertEqual(a.verify_node(99,0),{99:[0],100:[1]})
            for masks in [{99:[0,2]},{}]:
                with patch.object(a,'snapshot',return_value=masks),self.assertRaises(ValueError):a.verify_node(99,0)
    def test_selects_distinct_physical_cores_on_requested_node(self):
        rows=[{'cpu':cpu,'core':cpu%4,'socket':cpu//8,'node':cpu//8} for cpu in range(16)]
        p=a.select_cores(rows,set(range(16)),0,2)
        self.assertEqual(p['vcpu_host_cpus'],[0,1])
        self.assertEqual(p['qemu_service_cpus'],[2,3])
        self.assertEqual(p['guest_application_cpus'],[0,1])
        with self.assertRaises(ValueError):a.select_cores(rows,{0,1,4,5},0,2)

    def test_qmp_order_does_not_change_mapping(self):
        profile={'vcpu_host_cpus':[4,6]}
        cpus=[{'cpu-index':1,'thread-id':101},{'cpu-index':0,'thread-id':100}]
        self.assertEqual(a.expected_threads(cpus,profile),{100:[4],101:[6]})
        with self.assertRaises(ValueError):a.expected_threads(cpus[:1],profile)

    def test_verify_rejects_migration_and_missing_vcpu(self):
        p={'vcpu_host_cpus':[4],'qemu_service_cpus':[6,7]}
        cpus=[{'cpu-index':0,'thread-id':100}]
        with patch.object(a,'snapshot',return_value={99:[6,7],100:[4]}):
            self.assertEqual(a.verify(99,cpus,p)['status'],'PASS')
        for rows in [{99:[6,7],100:[4,5]},{99:[6,7]}]:
            with patch.object(a,'snapshot',return_value=rows),self.assertRaises(RuntimeError):a.verify(99,cpus,p)

    def test_failed_apply_restores_original_masks(self):
        p={'vcpu_host_cpus':[4],'qemu_service_cpus':[6,7]};before={99:[0,1],100:[0,1]}
        with patch.object(a,'snapshot',return_value=before),patch.object(a.os,'sched_setaffinity'),patch.object(a,'verify',side_effect=RuntimeError('drift')),patch.object(a,'restore') as restore:
            with self.assertRaises(RuntimeError):a.apply(99,[{'cpu-index':0,'thread-id':100}],p)
            restore.assert_called_once_with(99,before)

    def test_real_child_affinity_and_restore(self):
        allowed=sorted(os.sched_getaffinity(0))
        if len(allowed)<3:self.skipTest('requires three available CPUs')
        child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])
        try:
            original=a.snapshot(child.pid)
            p={'vcpu_host_cpus':[allowed[0]],'qemu_service_cpus':allowed[1:3]}
            before,evidence=a.apply(child.pid,[{'cpu-index':0,'thread-id':child.pid}],p)
            self.assertEqual(evidence['threads'][child.pid],[allowed[0]])
            a.restore(child.pid,before)
            self.assertEqual(a.snapshot(child.pid),original)
        finally:
            child.terminate();child.wait()


if __name__=='__main__':unittest.main()
