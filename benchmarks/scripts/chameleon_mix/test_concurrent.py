#!/usr/bin/env python3
"""CPU/RAM planning, concurrent launch, and failure recovery tests."""
import contextlib
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from chameleon_mix.common import HERE, ROOT, HA, VMROOT, within
from chameleon_mix import chameleon_affinity as affinity
from chameleon_mix import launch_vm
from chameleon_mix import placement
from chameleon_mix import run
from chameleon_mix.runtime import rt, engine, leaf_command
from chameleon_mix.concurrent_run import start_applications, run_mix


def topology(cores=24):
    return [{'cpu': n*cores+c+s*cores*2, 'node': n, 'socket': n, 'core': c}
            for n in (0, 1) for c in range(cores) for s in (0, 1)]


def snapshot(mib=128*1024):
    return {n: {'total_mib': mib, 'available_estimate_mib': mib} for n in (0, 1)}


class Resources(unittest.TestCase):
    def setUp(self):
        self.high = run.configurations(ROOT/'ae/config/fig9-highs.json')
        self.inventory = json.loads((ROOT/'ae/config/host.json').read_text())
        self.rows = topology()
        self.allowed = {r['cpu'] for r in self.rows}

    def plan(self, **kwargs):
        return run.build_plan(self.high, self.inventory, list(run.data.MIXES),
                              kwargs.get('snapshot', snapshot()), kwargs.get('topology', self.rows),
                              kwargs.get('allowed', self.allowed))

    def test_all_mixes_keep_parameters_and_fit_physical_cores_and_ram(self):
        plan = self.plan(snapshot=snapshot(128550))
        for mix, spec in plan['mixes'].items():
            self.assertEqual(spec['execution_mode'], 'three-concurrent')
            self.assertEqual(len(spec['phases']), 1)
            self.assertEqual([a['application'] for a in spec['applications']], run.data.MIXES[mix])
            for app in spec['applications']:
                expected = copy.deepcopy(self.high['applications'][app['application']])
                expected['configuration'].update(self.high.get('mix_configurations', {}).get(mix, {}).get(app['application'], {}))
                self.assertEqual(app, expected)
                self.assertEqual(app['remote_pool_mib'], 24576)
            physical = placement.validate_cpu_plan(spec['cpus'], self.rows)
            self.assertEqual(physical['status'], 'PASS')
            for row in spec['numa_memory']['nodes'].values():
                self.assertGreaterEqual(row['spare_mib'], 0)
        self.assertEqual(plan['mixes']['mix4']['physical_core_count'], 46)
        for mix in ('mix1', 'mix4'):
            self.assertTrue(any(len(p['host_memory_nodes']) > 1 for p in plan['mixes'][mix]['cpus']['applications'].values()))

    def test_capacity_not_reclaimed_floor_drives_boot_plan(self):
        with self.assertRaisesRegex(ValueError, 'No feasible'):
            self.plan(snapshot=snapshot(70*1024))

    def test_latest_parameters_are_selected_per_mix(self):
        plan = self.plan()
        expected = {
            'mix1': {'memcached': (1048576, 320000), 'graphchi': (32768, 64000), 'xsbench': (1048576, 960000)},
            'mix2': {'memcached': (131072, 40000), 'graphchi': (4096, 8000), 'spark-kmeans': (524288, 120000)},
            'mix3': {'memcached': (131072, 40000), 'graph500': (4096, 8), 'liblinear': (131072, 40000)},
            'mix4': {'cassandra': (4096, 8), 'graph500': (4096, 8), 'xsbench': (131072, 120000)},
        }
        for mix, spec in plan['mixes'].items():
            for app in spec['applications']:
                cfg = app['configuration']
                self.assertEqual((cfg['sample_period'], cfg['hhh_interval_ms']), expected[mix][app['application']])
        self.assertEqual(rt.REMOTE_POOL_MIB, 24576)

    def test_interleaved_vm_boots_first_without_changing_slots(self):
        from chameleon_mix.concurrent_run import boot_order
        plan = self.plan()
        for mix in ('mix1', 'mix4'):
            spec = plan['mixes'][mix]
            slots = plan['inventory']['slots']
            ordered = boot_order(spec, plan['inventory'])
            self.assertGreater(len(spec['cpus']['applications'][ordered[0][1]['application']]['host_memory_nodes']), 1)
            self.assertEqual({a['application']: s['name'] for s, a in ordered},
                             {a['application']: s['name'] for s, a in zip(slots, spec['applications'])})

    def test_cpu_restriction_does_not_count_smt_as_spare_physical_cores(self):
        with self.assertRaisesRegex(ValueError, 'physical cores'):
            self.plan(topology=topology(16), allowed=set(range(64)))

    def test_cross_node_client_can_fill_remaining_cores(self):
        apps = [{'application': name, 'vm_memory_mib': 4096,
                 'workload_configuration': {'vcpus': 4, 'args': ['--threads','10'] if name=='cassandra' else []}}
                for name in ('cassandra','graph500','xsbench')]
        rows = topology(14)
        plan = placement.plan(apps, self.inventory, snapshot(), rt.memory_reserves(self.inventory),
                              rows, {r['cpu'] for r in rows})
        self.assertEqual(plan['cpus']['client_numa_node'], '0,1')
        self.assertEqual(len(plan['cpus']['client_cpus']), 10)
        placement.validate_cpu_plan(plan['cpus'], rows)

    def test_smt_collision_rejected(self):
        p = self.plan()['mixes']['mix1']['cpus']
        app = next(iter(p['applications'].values()))
        p['client_cpus'][0] = app['vcpu_host_cpus'][0] + 48
        p['client_numa_node'] = '0,1'
        with self.assertRaisesRegex(ValueError, 'shared'):
            placement.validate_cpu_plan(p, self.rows)

    def test_available_memory_rechecked_after_other_allocations(self):
        spec = self.plan()['mixes']['mix1']
        with self.assertRaisesRegex(ValueError, 'need'):
            placement.check_available(spec['applications'], spec['cpus'], self.inventory,
                                      snapshot(80*1024), rt.memory_reserves(self.inventory), 256*1024)

    def test_qemu_memory_policy_and_paths_are_explicit(self):
        c = launch_vm.config({'name':'co-test', 'disk':'/disk.qcow2', 'kernel':None,
                              'memory_mib':49152, 'host_numa_node':1, 'host_memory_nodes':[0,1]})
        argv = launch_vm.command(c)
        obj = json.loads(argv[argv.index('-object')+1])
        self.assertEqual(obj['policy'], 'interleave')
        self.assertEqual(obj['host-nodes'], [0,1])
        self.assertIn(str(VMROOT/'build/running/co-test/qemu.pid'), argv)
        self.assertEqual(obj['size'], 49152*1024**2)

    def test_external_results_directory_rejected(self):
        with self.assertRaises(ValueError):
            within(Path('/tmp/outside-ae-results'))

    def test_leaf_command_uses_private_entrypoint_and_keeps_high_knobs(self):
        spec = self.plan()['mixes']['mix1']
        high = self.high['applications']['graphchi']
        argv = leaf_command('test', self.inventory['slots'][0], high,
                                   'affinity.json','work.json','memory.json',spec['cpus'],14400,HERE/'build/raw')
        self.assertEqual(argv[1:3], ['-B',str(HERE/'leaf.py')])
        self.assertEqual(argv[argv.index('--minimum-local-mib')+1], '28672')


class Lifecycle(unittest.TestCase):
    def test_leaf_entrypoint_starts_independently(self):
        result = subprocess.run([sys.executable, '-B', str(HERE / 'leaf.py'), '--help'],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--sample-period', result.stdout)

    def temporary(self):
        (ROOT/'ae/build').mkdir(parents=True, exist_ok=True)
        return tempfile.TemporaryDirectory(dir=ROOT/'ae/build')

    def test_three_applications_start_without_waiting_for_completion(self):
        with self.temporary() as tmp:
            directory = Path(tmp)
            children, logs, record = {}, [], {}
            program = ('import json,sys,time; from pathlib import Path; '
                       'start=time.time(); time.sleep(.5); '
                       'Path(sys.argv[1]).write_text(json.dumps([start,time.time()]))')
            commands = {case: [sys.executable, '-B', '-c', program, str(directory/(case+'.json'))]
                        for case in ('memcached','graphchi','xsbench')}
            try:
                start_applications(commands, directory, record, children, logs)
                self.assertEqual(len(children), 3)
                self.assertTrue(all(child.poll() is None for child in children.values()))
                for child in children.values():
                    self.assertEqual(child.wait(timeout=5), 0)
                times = [json.loads((directory/(case+'.json')).read_text()) for case in commands]
                self.assertLess(max(t[0] for t in times), min(t[1] for t in times))
            finally:
                for child in children.values():
                    if child.poll() is None: child.terminate()
                    child.wait(timeout=5)
                for log in logs: log.close()

    def test_three_24gib_pools_start_on_distinct_ports_and_stop(self):
        with self.temporary() as tmp:
            out = Path(tmp)
            binary = out / 'server'
            binary.touch()
            inventory = {'server': {'manage_local': True, 'binary': str(binary), 'address': '192.0.2.1'},
                         'slots': [{'name': 'vm' + str(n), 'server_port': 9451 + n} for n in range(3)]}
            commands, children = [], []
            def start(command, stdout, **kwargs):
                commands.append(command)
                child = mock.Mock(poll=mock.Mock(return_value=None))
                children.append(child)
                stdout.write('READY Hermit RDMA\n')
                stdout.flush()
                return child
            with mock.patch.object(rt.subprocess, 'Popen', side_effect=start):
                with rt.servers(inventory, out) as running:
                    self.assertEqual(len(running), 3)
                for child in children:
                    child.terminate.assert_called_once_with()
                    child.wait.assert_called_once_with(timeout=20)
            self.assertEqual([c[-2:] for c in commands], [[str(9451 + n), '24576'] for n in range(3)])

    def test_one_attempt_plots_without_changing_default_repeats(self):
        with self.temporary() as tmp:
            out = Path(tmp)
            (out / 'raw').mkdir()
            row = engine.data.aggregate_mix('mix1', [
                {'application': case, 'status': 'PASS', 'slowdown_percent': value}
                for case, value in zip(engine.data.MIXES['mix1'], (3, 6, 12))])
            row.update(repetition=1, cleanup_errors=[])
            (out / 'raw/report.json').write_text(json.dumps({'status': 'PASS', 'reference': {},
                'mixes': {'mix1': {'status': 'PASS', 'repetitions': [row]}}}))
            (out / 'config.json').write_text(json.dumps({'normalization': 'test'}))
            summary = run.summarize(out, 1)
            self.assertEqual(summary['mixes']['mix1']['slowdown_pct'], 7)
            self.assertEqual(json.loads((out / 'fig9.plot-data.json').read_text())['repeats'], 1)
            self.assertGreater((out / 'fig9.svg').stat().st_size, 0)
        import contextlib, io
        for args, repeats in (([], 3), (['--repeats', '1'], 1)):
            stream = io.StringIO()
            with mock.patch.object(run, 'build_plan', return_value={}), contextlib.redirect_stdout(stream):
                run.main(['--plan', *args])
            self.assertEqual(json.loads(stream.getvalue())['repeats'], repeats)

    def test_failure_recovery_continues_next_repetition_and_mix(self):
        with self.temporary() as tmp:
            out=Path(tmp); config=out/'vm.json'; config.write_text('{}')
            access={'name':'co-test','vm_config':str(config)}
            inventory={'slots':[{'name':'co-test'}]}
            plan={'mixes':{'mix1':{},'mix2':{}},'reference':{}}
            def attempt(mix,rep,*args,**kwargs):
                if mix=='mix1' and rep==2: raise RuntimeError('injected failure')
                return {'status':'PASS','repetition':rep,'mix':mix,'cleanup_errors':[]}
            with mock.patch.object(rt,'access',return_value=access), \
                 mock.patch.object(rt.guest,'active',return_value=None), \
                 mock.patch.object(rt.guest,'launcher_idle',return_value=True), \
                 mock.patch.object(rt,'recover_owned_guests',return_value={'errors':[]}) as recover, \
                 mock.patch.object(rt,'servers',side_effect=lambda *a:contextlib.nullcontext([])), \
                 mock.patch.object(engine,'run_mix',side_effect=attempt) as run_mix:
                report=engine.run_campaign(plan,inventory,{},out,'test',3,60)
            self.assertEqual(run_mix.call_count,6)
            self.assertEqual(recover.call_count,7)
            self.assertEqual(report['mixes']['mix1']['successful_repeats'],2)
            self.assertEqual(report['mixes']['mix2']['successful_repeats'],3)
            self.assertEqual(report['status'],'PARTIAL')

    def test_failed_second_boot_stops_both_owned_vms_and_restores_configs(self):
        with self.temporary() as tmp:
            out=Path(tmp)
            accesses={}
            for name in ('co-one','co-two','co-three'):
                file=out/(name+'.json'); file.write_text('{"original": true}')
                accesses[name]={'name':name,'vm_config':str(file)}
            apps=[{'application':case,'vm_memory_mib':4096,'workload_configuration':{'args':[]}}
                  for case in ('memcached','graphchi','xsbench')]
            spec={'applications':apps,'cpus':{'applications':{a['application']:{'host_memory_nodes':[0]} for a in apps}}}
            inventory={'slots':[{'name':name} for name in accesses],'server':{}}
            def boot(access,timeout):
                if access['name']=='co-two': raise TimeoutError('injected boot failure')
            with mock.patch.object(rt,'access',side_effect=accesses.__getitem__), \
                 mock.patch.object(rt,'assert_servers'), \
                 mock.patch.object(rt,'check_memory_available',return_value={}), \
                 mock.patch.object(rt,'slot_config',return_value={'temporary':True}), \
                 mock.patch.object(rt.guest,'control_lock',side_effect=lambda a:contextlib.nullcontext()), \
                 mock.patch.object(rt.guest,'start',side_effect=boot), \
                 mock.patch.object(rt,'setup_guest',return_value={'status':'PASS'}), \
                 mock.patch.object(rt,'pin_guest_cpus',return_value={}), \
                 mock.patch.object(rt,'stop_guest') as stop, \
                 mock.patch('chameleon_mix.concurrent_run.leaf_command',return_value=['unused']), mock.patch('chameleon_mix.concurrent_run.memory_observation',return_value={}):
                with self.assertRaisesRegex(TimeoutError,'injected'):
                    run_mix('mix1',1,spec,inventory,{},out,'test',60,results_root=out/'apps')
            self.assertEqual([c.args[0]['name'] for c in stop.call_args_list],['co-two','co-one'])
            self.assertTrue(all(json.loads(Path(a['vm_config']).read_text())=={'original':True} for a in accesses.values()))
            report=json.loads((out/'mix1/repeat-01/report.json').read_text())
            self.assertEqual(report['status'],'FAIL')

    def test_partial_results_plot_successes_and_omit_failed_mix(self):
        with self.temporary() as tmp:
            out=Path(tmp); (out/'raw').mkdir()
            rows=[]
            for repeat in range(1,4):
                if repeat==2:
                    rows.append({'status':'FAIL','repetition':repeat,'error':'injected'})
                else:
                    row=engine.data.aggregate_mix('mix1',[{'application':case,'status':'PASS','slowdown_percent':value}
                        for case,value in zip(engine.data.MIXES['mix1'],(10,20,30))])
                    rows.append(dict(row,repetition=repeat,execution_mode='three-concurrent',cleanup_errors=[]))
            report={'status':'PARTIAL','reference':{},'mixes':{
                'mix1':{'status':'PARTIAL','repetitions':rows},
                'mix2':{'status':'FAIL','repetitions':[{'status':'FAIL','repetition':n,'error':'injected'} for n in (1,2,3)]}}}
            (out/'raw/report.json').write_text(json.dumps(report))
            (out/'config.json').write_text(json.dumps({'normalization':'test'}))
            summary=run.summarize(out,3)
            self.assertEqual(summary['mixes']['mix1']['successful_repeats'],2)
            self.assertEqual(summary['mixes']['mix1']['slowdown_pct'],20)
            self.assertIn('mix2',summary['omitted_mixes'])
            self.assertTrue((out/'fig9.svg').stat().st_size>0)
            self.assertTrue((out/'fig9.pdf').stat().st_size>0)


if __name__=='__main__':
    unittest.main(verbosity=2)
