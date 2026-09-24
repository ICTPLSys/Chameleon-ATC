#!/usr/bin/env python3
"""Failures are recorded, resources released, and remaining attempts still run."""
import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'ae/scripts'), str(ROOT / 'benchmarks/scripts'), str(ROOT / 'ae/tests')]
import test_fig78 as fixtures78
import test_figures as fixtures
import ae_fig78 as ae
import ae_fig9
import evaluate
import plot_figures as plots
import chameleon_fig9_runtime as rt
spec = importlib.util.spec_from_file_location('fig9_continue', ROOT / 'benchmarks/scripts/run-chameleon-fig9.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class Continuation(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = ae.load_points()

    def fixture(self):
        fixtures78.Figure78.fixture(self, self.root)

    def fail(self, case, role, repeat):
        ae.save(self.root / 'runs' / ae.run_name(case, role, repeat) / 'execution.json',
                {'status': 'FAIL', 'error': 'injected application crash', 'cleanup_errors': []})

    def test_second_failure_uses_first_and_third_for_both_memcached_figures(self):
        self.fixture()
        self.fail('memcached', 'high', 2)
        summary = ae.summarize(self.root)
        point = summary['applications']['memcached']['points'][-1]
        self.assertEqual(summary['status'], 'PARTIAL')
        self.assertEqual([r['repeat'] for r in point['runs']], [1, 3])
        self.assertEqual(point['successful_repeats'], 2)
        self.assertEqual(point['failed_runs'][0]['repeat'], 2)
        for fig in ('fig7', 'fig8'):
            data = plots.prepare(fig, summary, json.loads((plots.BASELINES / (fig+'.json')).read_text()))
            self.assertEqual(data['applications']['memcached']['observations'][-1]['successful_repeats'], 2)
            plots.render(data, self.root / 'plots')
        self.assertAlmostEqual(point['p95_slowdown_pct'], 150)

    def test_only_first_success_is_not_divided_by_three(self):
        self.fixture()
        for r in (2, 3): self.fail('xsbench', 'high', r)
        p = ae.summarize(self.root)['applications']['xsbench']['points'][-1]
        self.assertEqual(p['successful_repeats'], 1)
        self.assertAlmostEqual(p['slowdown_pct'], 10)
        self.assertEqual(p['reclamation_pct'], 10)

    def test_all_failed_point_omitted_but_later_points_and_apps_remain(self):
        self.fixture()
        for r in (1, 2, 3): self.fail('xsbench', 'medium', r)
        s = ae.summarize(self.root)
        self.assertEqual([p['name'] for p in s['applications']['xsbench']['points']], ['all_local', 'low', 'high'])
        self.assertIn('memcached', s['applications'])
        d = plots.prepare('fig7', s, json.loads((plots.BASELINES / 'fig7.json').read_text()))
        self.assertEqual(len(d['applications']['xsbench']['curves']['Chameleon']), 3)
        self.assertEqual(len(s['errors']), 3)

    def test_partial_all_local_is_denominator(self):
        self.fixture()
        self.fail('xsbench', 'all_local', 2)
        s = ae.summarize(self.root)['applications']['xsbench']
        self.assertEqual(s['all_local']['mean_cost'], 90)
        self.assertAlmostEqual(s['points'][-1]['slowdown_pct'], 100 * (120/90 - 1))

    def test_no_baseline_omits_application_and_dependent_mixes_without_fallback(self):
        self.fixture()
        for r in (1, 2, 3): self.fail('xsbench', 'all_local', r)
        s = ae.summarize(self.root)
        self.assertNotIn('xsbench', s['applications'])
        high = ae_fig9.frozen_highs(ROOT/'ae/config/fig9-highs.json', ROOT/'ae/config/fig78-points.json', self.root/'all-local.json')
        self.assertIn('xsbench', high['missing_baselines'])
        self.assertIsNone(high['applications']['xsbench']['all_local']['performance'])
        self.assertGreater(high['applications']['memcached']['all_local']['performance']['cost'], 0)

    def test_all_fig9_baselines_missing_completes_without_launching_vm(self):
        baseline = self.root/'all-local.json'
        ae.save(baseline, {'status':'PARTIAL','repeats':3,'applications':{}})
        out = self.root/'fig9'
        with mock.patch.object(ae_fig9.subprocess, 'run') as launch, mock.patch.object(ae_fig9.rt, 'validate_inventory'), \
             mock.patch.object(ae_fig9.rt, 'numa_memory_snapshot', return_value={}):
            ae_fig9.main(['--run','--all-local',str(baseline),'--inventory',str(ROOT/'ae/config/host.example.json'),'--results',str(out)])
        # Only the plotting subprocess is invoked; no runtime or pool is started.
        self.assertEqual(launch.call_count, 1)
        self.assertIn('plot_figures.py', launch.call_args.args[0][1])
        summary = json.loads((out/'summary.json').read_text())
        self.assertEqual(summary['mixes'], {})
        self.assertEqual(set(summary['omitted_mixes']), set(ae_fig9.data.MIXES))

    def test_empty_plots_remove_stale_figures_and_preserve_diagnostics(self):
        out=self.root/'plots';out.mkdir()
        for n in (7,8):
            for ext in ('svg','pdf'): (out/f'fig{n}.{ext}').write_text('stale')
        summary=self.root/'summary.json'
        ae.save(summary, {'status':'PARTIAL','repeats':3,'applications':{},'errors':[{'error':'all failed'}]})
        plots.main(['--figure','fig78','--input',str(summary),'--output-dir',str(out)])
        self.assertFalse(list(out.glob('*.svg')))
        self.assertEqual(json.loads((out/'fig7.plot-data.json').read_text())['errors'][0]['error'], 'all failed')

    def test_fig9_partial_repetitions_and_empty_mix(self):
        raw=fixtures.fixture9();raw['status']='PARTIAL'
        rows=raw['mixes']['mix1']['repetitions'];rows[1]['status']='FAIL'
        raw['mixes']['mix1']['status']='PARTIAL'
        for r in raw['mixes']['mix2']['repetitions']:r['status']='FAIL'
        raw['mixes']['mix2']['status']='FAIL'
        s=ae_fig9.summarize(raw)
        self.assertEqual(s['mixes']['mix1']['repetition_values'], [3,7])
        self.assertEqual(s['mixes']['mix1']['slowdown_pct'], 5)
        self.assertNotIn('mix2', s['mixes'])
        self.assertIn('mix3', s['mixes'])
        self.assertEqual(len(s['failed_runs']['mix2']), 3)
        d=plots.prepare('fig9', s, raw['reference'])
        plots.render(d, self.root/'plots')
        self.assertEqual(d['mixes']['mix1']['successful_repeats'], 2)

    def campaign78(self, interrupt=False):
        runtime, events, config = fixtures78.Figure78.fake_runtime(self, self.root)
        runtime.prepare_slots=lambda *a: None
        @contextlib.contextmanager
        def servers(*args):
            events.append('pool-start')
            try:yield []
            finally:events.append('pool-stop')
        runtime.servers=servers
        def recover(owned, originals):
            self.assertFalse(runtime.guest.active(owned[0]))
            self.assertEqual(json.loads(config.read_text()), {'original':True})
            events.append('recover')
            return {'errors':[]}
        runtime.recover_owned_guests=recover
        slot={'name':'single','rdma_interface':'ibp1s0'}
        inventory={'slots':[slot],'single_slot':slot,'server':{}}
        jobs=ae.schedule(self.config,['xsbench'])[:3]
        procs=[SimpleNamespace(poll=lambda:0,returncode=code) for code in (0,1,0)]
        if interrupt:procs[1]=KeyboardInterrupt()
        def summary(out):
            return [json.loads((out/'runs'/j['name']/'execution.json').read_text())['status'] for j in jobs]
        with mock.patch.object(ae, 'schedule', return_value=jobs), \
             mock.patch.object(ae,'single_cpu_plan',return_value={'profile':{},'client_cpus':[],'client_numa_node':1}), \
             mock.patch.object(ae,'start_guard',return_value=(None,None,None)), \
             mock.patch.object(ae.subprocess,'Popen',side_effect=procs), \
             mock.patch.object(ae,'extract_run',return_value={'performance':{'cost':1}}), \
             mock.patch.object(ae,'summarize',side_effect=summary), contextlib.redirect_stdout(io.StringIO()):
            result=ae.run_locked(runtime,self.config,inventory,['xsbench'],self.root/'campaign',60,runtime.access('single'))
        return result,events

    def test_real_fig78_runner_reboots_third_attempt_after_second_crash(self):
        statuses,events=self.campaign78()
        self.assertEqual(statuses,['PASS','FAIL','PASS'])
        self.assertEqual(events.count('boot'),3)
        self.assertEqual(events.count('stop'),3)
        self.assertEqual(events.count('pool-start'),3)
        self.assertEqual(events.count('pool-stop'),3)
        self.assertEqual(events.count('recover'),4)

    def test_user_interrupt_is_not_swallowed(self):
        with self.assertRaises(KeyboardInterrupt): self.campaign78(interrupt=True)

    def test_fig9_runner_cleans_second_crash_and_continues_next_mix(self):
        cfg=self.root/'vm.json';cfg.write_text('{}')
        access={'name':'vm','vm_config':str(cfg)}
        events=[];active=[False]
        def recover(*args):
            active[0]=False;events.append('clean');return {'errors':[]}
        @contextlib.contextmanager
        def pools(*args):
            events.append('pool-start')
            try:yield []
            finally:events.append('pool-stop')
        def run(mix,rep,*args):
            self.assertFalse(active[0]);active[0]=True;events.append((mix,rep))
            if mix=='mix1' and rep==2:raise RuntimeError('injected crash')
            active[0]=False
            return {'status':'PASS','mix':mix,'repetition':rep}
        with mock.patch.object(rt,'access',return_value=access), \
             mock.patch.object(rt.guest,'active',side_effect=lambda a:active[0]), \
             mock.patch.object(rt.guest,'launcher_idle',return_value=True), \
             mock.patch.object(rt,'recover_owned_guests',side_effect=recover), \
             mock.patch.object(rt,'servers',side_effect=pools), mock.patch.object(runner,'run_mix',side_effect=run):
            report=runner.run_campaign({'mixes':{'mix1':{},'mix2':{}},'reference':{}},
                                       {'slots':[{'name':'vm'}]}, {}, self.root, 'test',3,60)
        self.assertEqual([r['status'] for r in report['mixes']['mix1']['repetitions']],['PASS','FAIL','PASS'])
        self.assertEqual(report['mixes']['mix2']['status'],'PASS')
        self.assertEqual(events.count('pool-stop'),6)
        self.assertFalse(active[0])

    def test_recovery_uses_forced_stop_when_graceful_shutdown_fails(self):
        config=self.root/'vm.json';config.write_text('{"changed":true}')
        a={'name':'owned','vm_config':str(config)};active=[True]
        def stop(*args,**kwargs):
            self.assertTrue(kwargs['force']);active[0]=False
        with mock.patch.object(rt.guest,'control_lock',return_value=contextlib.nullcontext()), \
             mock.patch.object(rt.guest,'active',side_effect=lambda a:active[0]), \
             mock.patch.object(rt.guest,'launcher_idle',side_effect=lambda a:not active[0]), \
             mock.patch.object(rt,'stop_guest',side_effect=TimeoutError('hung guest')), \
             mock.patch.object(rt.guest,'stop',side_effect=stop):
            result=rt.recover_owned_guests([a],{'owned':'{}'})
        self.assertEqual(result['status'],'PASS')
        self.assertEqual(config.read_text(),'{}')

    def test_unreleased_vm_is_not_claimed_clean(self):
        config=self.root/'vm.json';config.write_text('{"changed":true}')
        a={'name':'owned','vm_config':str(config)}
        with mock.patch.object(rt.guest,'control_lock',return_value=contextlib.nullcontext()), \
             mock.patch.object(rt.guest,'active',return_value=True), mock.patch.object(rt.guest,'launcher_idle',return_value=False), \
             mock.patch.object(rt,'stop_guest',side_effect=TimeoutError('hung')), \
             mock.patch.object(rt.guest,'stop',side_effect=TimeoutError('still hung')):
            result=rt.recover_owned_guests([a],{'owned':'{}'})
        self.assertEqual(result['status'],'FAIL')
        self.assertEqual(config.read_text(),'{"changed":true}')

    def test_failed_fig78_stage_does_not_prevent_fig9_invocation(self):
        failure=subprocess.CalledProcessError(1,['fig78'])
        with mock.patch.object(ae_fig9,'resource_plans'), \
             mock.patch.object(evaluate.subprocess,'run',side_effect=[failure,None]) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                evaluate.main(['all','--results',str(self.root),
                               '--inventory',str(ROOT/'ae/config/host.example.json')])
        self.assertEqual(run.call_count,2)


if __name__=='__main__':unittest.main()
