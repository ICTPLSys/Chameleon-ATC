#!/usr/bin/env python3
"""Co-run coordination, CPU allocation, and independent VM client routing."""
from concurrent.futures import ThreadPoolExecutor
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import subprocess
import sys
import chameleon_barrier as barrier
import chameleon_affinity as affinity

spec=importlib.util.spec_from_file_location('apps',Path(__file__).with_name('run-chameleon-apps.py'))
apps=importlib.util.module_from_spec(spec)
spec.loader.exec_module(apps)


class Barrier(unittest.TestCase):
    def ready(self,directory,n):
        deadline=time.monotonic()+2
        while len(list(directory.glob('*.ready.json')))<n:
            if time.monotonic()>deadline: self.fail('workers never became ready')
            time.sleep(.005)

    def test_three_members_wait_for_one_release(self):
        with tempfile.TemporaryDirectory() as temporary, ThreadPoolExecutor(max_workers=3) as pool:
            directory=Path(temporary)
            futures=[pool.submit(barrier.wait,directory,str(i),2,{'vm':i}) for i in range(3)]
            self.ready(directory,3)
            for i in range(3):
                self.assertEqual(json.loads((directory/f'{i}.ready.json').read_text())['evidence'],{'vm':i})
            self.assertFalse(any(f.done() for f in futures))
            (directory/'RELEASE').write_text(json.dumps({'timestamp':time.time()}))
            results=[f.result(timeout=2) for f in futures]
            self.assertTrue(all(r['released_unix_seconds']>=r['ready_unix_seconds'] for r in results))

    def test_abort_cancels_wait_and_is_not_release(self):
        with tempfile.TemporaryDirectory() as temporary, ThreadPoolExecutor(max_workers=1) as pool:
            directory=Path(temporary)
            future=pool.submit(barrier.wait,directory,'vm',2)
            self.ready(directory,1)
            (directory/'ABORT').write_text('another VM preparation failed')
            (directory/'RELEASE').touch()
            with self.assertRaisesRegex(RuntimeError,'another VM preparation failed'):
                future.result(timeout=2)

    def test_timeout_keeps_evidence_and_duplicate_member_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary)
            with self.assertRaises(TimeoutError): barrier.wait(directory,'vm',.01)
            self.assertTrue((directory/'vm.ready.json').exists())
            with self.assertRaises(FileExistsError): barrier.wait(directory,'vm',.01)

    def test_released_or_invalid_barrier_is_rejected_before_readiness(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary)
            for member,timeout in [('../escape',1),('vm',float('nan')),('vm',0)]:
                with self.assertRaises(ValueError): barrier.wait(directory,member,timeout)
            (directory/'RELEASE').touch()
            with self.assertRaises(RuntimeError): barrier.wait(directory,'vm',1)
            self.assertFalse((directory/'vm.ready.json').exists())

    def test_host_client_cli_waits_after_preparation(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary)
            child=subprocess.Popen([sys.executable,str(Path(barrier.__file__)),
                '--directory',str(directory),'--member','cassandra','--timeout','2',
                '--case','cassandra','--vm','vm3','--phase','after_load_before_read'],
                stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            try:
                self.ready(directory,1)
                ready=json.loads((directory/'cassandra.ready.json').read_text())
                self.assertEqual(ready['evidence']['phase'],'after_load_before_read')
                self.assertIsNone(child.poll())
                (directory/'RELEASE').touch()
                stdout,stderr=child.communicate(timeout=2)
                self.assertEqual(child.returncode,0,stderr)
                self.assertEqual(json.loads(stdout)['member'],'cassandra')
            finally:
                if child.poll() is None: child.kill();child.wait()


class Allocation(unittest.TestCase):
    def setUp(self):
        self.rows=[{'cpu':i,'node':i//6,'socket':i//6,'core':i%6} for i in range(12)]
        self.profile={'host_numa_node':0,'vcpu_host_cpus':[0,1],
                      'qemu_service_cpus':[2,3],'guest_application_cpus':[0,1]}

    def test_external_allocation_retained(self):
        profile={**self.profile,'vcpu_host_cpus':[2,3],'qemu_service_cpus':[4,5]}
        result=apps.validate_affinity_profile(profile,self.rows,range(12),0,2)
        self.assertEqual(result['vcpu_host_cpus'],[2,3])

    def test_leaf_accepts_parent_pinned_vm_and_preserves_masks_on_restore(self):
        cpus=[{'cpu-index':0,'thread-id':101},{'cpu-index':1,'thread-id':102}]
        masks={100:[2,3],101:[0],102:[1],103:[2,3]}
        original=copy.deepcopy(masks)
        cfg={'host_numa_node':0,'host_cpus':[0,1,2,3]}
        with patch.object(affinity,'topology',return_value=self.rows), \
             patch.object(affinity,'snapshot',side_effect=lambda pid:copy.deepcopy(masks)), \
             patch.object(affinity.os,'sched_setaffinity',side_effect=lambda tid,cpus:masks.__setitem__(tid,list(cpus))):
            profile,evidence=apps.preflight_affinity(100,cpus,cfg,self.profile)
            self.assertEqual(evidence['allowed_cpus'],[0,1,2,3])
            self.assertEqual(evidence['thread_cpu_masks'][100],[2,3])
            before,verification=affinity.apply(100,cpus,profile)
            self.assertEqual(verification['status'],'PASS')
            affinity.restore(100,before)
        self.assertEqual(masks,original)

    def test_leaf_does_not_expand_to_cpus_only_present_in_configuration(self):
        cpus=[{'cpu-index':0,'thread-id':101},{'cpu-index':1,'thread-id':102}]
        cfg={'host_numa_node':0,'host_cpus':[0,1,2,3,4,5]}
        profile={**self.profile,'vcpu_host_cpus':[4,5]}
        with patch.object(affinity,'topology',return_value=self.rows), \
             patch.object(affinity,'snapshot',return_value={100:[2,3],101:[0],102:[1]}):
            with self.assertRaisesRegex(ValueError,'outside the QEMU allowed mask'):
                apps.preflight_affinity(100,cpus,cfg,profile)

    def test_leaf_rejects_any_thread_outside_configured_allocation(self):
        cpus=[{'cpu-index':0,'thread-id':101},{'cpu-index':1,'thread-id':102}]
        cfg={'host_numa_node':0,'host_cpus':[0,1,2,3]}
        masks={100:[2,3],101:[0],102:[1],103:[4]}
        with patch.object(affinity,'snapshot',return_value=masks):
            with self.assertRaisesRegex(ValueError,'outside configured host_cpus'):
                apps.preflight_affinity(100,cpus,cfg,self.profile)

    def test_leaf_requires_main_and_every_vcpu_thread(self):
        cpus=[{'cpu-index':0,'thread-id':101},{'cpu-index':1,'thread-id':102}]
        cfg={'host_numa_node':0,'host_cpus':[0,1,2,3]}
        for masks in ({100:[2,3],101:[0]},{101:[0],102:[1]},{100:[],101:[0],102:[1]}):
            with self.subTest(masks=masks), patch.object(affinity,'snapshot',return_value=masks):
                with self.assertRaisesRegex(ValueError,'missing'):
                    apps.preflight_affinity(100,cpus,cfg,self.profile)

    def test_unconfigured_vm_selection_stays_in_observed_thread_masks(self):
        cpus=[{'cpu-index':0,'thread-id':101},{'cpu-index':1,'thread-id':102}]
        cfg={'host_numa_node':0,'host_cpus':[]}
        with patch.object(affinity,'topology',return_value=self.rows), \
             patch.object(affinity,'snapshot',return_value={100:[2,3],101:[0],102:[1]}):
            profile,evidence=apps.preflight_affinity(100,cpus,cfg)
        self.assertEqual(profile['vcpu_host_cpus'],[0,1])
        self.assertEqual(profile['qemu_service_cpus'],[2,3])
        self.assertEqual(evidence['allowed_cpus'],[0,1,2,3])

    def test_vm_topology_node_cpuset_and_duplicate_core_mismatch(self):
        bad=[{'host_numa_node':1},{'vcpu_host_cpus':[0]},
             {'qemu_service_cpus':[1,2]},{'qemu_service_cpus':[6,7]},
             {'guest_application_cpus':[2]}]
        for change in bad:
            with self.subTest(change=change),self.assertRaises(ValueError):
                apps.validate_affinity_profile({**self.profile,**change},self.rows,range(12),0,2)
        with self.assertRaises(ValueError):
            apps.validate_affinity_profile(self.profile,self.rows,{0,1,2},0,2)
        rows=[dict(row,core=0) if row['cpu']==3 else row for row in self.rows]
        with self.assertRaisesRegex(ValueError,'SMT'):
            apps.validate_affinity_profile(self.profile,rows,range(12),0,2)

    def test_client_subset_validation(self):
        self.assertEqual(affinity.validate_client_cpus('6-7,9',1,self.rows,range(12)),[6,7,9])
        for value in ['6,6','7-6','6,0','6,999','6;echo','']:
            with self.subTest(value=value),self.assertRaises(ValueError):
                affinity.validate_client_cpus(value,1,self.rows,range(12))
        with self.assertRaises(ValueError):
            affinity.validate_client_cpus('6-7',1,self.rows,{6})

    def test_client_environment_routes_to_selected_vm(self):
        access={'name':'fig9-vm2','_path':Path('/tmp/guests/fig9-vm2/access.json'),
                'host':'127.0.0.1','port':5225,'user':'ubuntu'}
        env=apps.client_environment(access,0,1,19142,11312,'6,7',
            {'DIR':Path('/tmp/mix1'),'MEMBER':'cassandra','TIMEOUT':60})
        for prefix in ['CASSANDRA','MEMCACHED']:
            self.assertEqual(env[prefix+'_GUEST_NAME'],'fig9-vm2')
            self.assertEqual(env[prefix+'_GUEST_SSH_PORT'],'5225')
            self.assertEqual(env[prefix+'_GUEST_DIR'],'/tmp/guests/fig9-vm2')
            self.assertEqual(env[prefix+'_CLIENT_CPUS'],'6,7')
        self.assertTrue(env['CASSANDRA_QEMU_PID_FILE'].endswith('/fig9-vm2/qemu.pid'))
        self.assertEqual(env['CASSANDRA_LOCAL_CQL_PORT'],'19142')
        self.assertEqual(env['MEMCACHED_HOST_PORT'],'11312')
        self.assertEqual(env['CHAMELEON_BARRIER_DIR'],'/tmp/mix1')
        self.assertEqual(env['CHAMELEON_BARRIER_MEMBER'],'cassandra')
        self.assertEqual(env['CHAMELEON_BARRIER_TIMEOUT'],'60')


if __name__=='__main__': unittest.main()
