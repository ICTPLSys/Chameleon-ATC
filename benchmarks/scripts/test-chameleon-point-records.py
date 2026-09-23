#!/usr/bin/env python3
import copy
import csv
import json
from pathlib import Path
import tempfile
import unittest

import chameleon_point_records as records


class PointRecordsTests(unittest.TestCase):
    def accepted_fixture(self):
        data=copy.deepcopy(self.data);entry=data['cases']['app'];entry['status']='ACCEPTED'
        ids=['d1','d2','d3'];repeat_ids=['v2-d1','v2-d2','v2-d3']
        entry['trials']['b']['tracking_profile']=self.trial['tracking_profile']
        entry['trials']['b2']={**entry['trials']['b'],'report':'/baseline2.json'}
        means=[]
        for i,(tag,rep) in enumerate(zip(ids,repeat_ids)):
            r=copy.deepcopy(self.trial);r['report']='/'+tag+'.json'
            r['parameters']['minimum_local_bytes']=(23000-i*1000)*2**20
            r['baseline_tracking_profile']=self.trial['tracking_profile']
            r['reclaim_percent']=5+i*10;r['slowdown_percent']=i+1
            entry['trials'][tag]=r
            entry['trials'][rep]={**copy.deepcopy(r),'report':'/'+rep+'.json','baseline_report':'/baseline2.json'}
            means.append({'reclaim_percent':r['reclaim_percent'],'slowdown_percent':r['slowdown_percent']})
        validation={'status':'PASS','candidate_ids':ids,
            'rounds':[{'repeat':1,'baseline':'b','trial_ids':ids,'check':{'status':'PASS'}},
                      {'repeat':2,'baseline':'b2','trial_ids':repeat_ids,'check':{'status':'PASS'}}],
            'aggregate_check':{'status':'PASS','mean_points':means,
                'observed_ranges':[{'slowdown_percent':{'min':i+1,'max':i+1}} for i in range(3)]}}
        entry['accepted']={'memory_mib':32768,'validation':validation}
        return data

    def test_frozen_curve_contains_only_selected_low_middle_high(self):
        data=self.accepted_fixture();result=records.accepted_configurations(data)
        app=result['applications']['app']
        self.assertEqual([p['role'] for p in app['points']],['low','middle','high'])
        self.assertEqual([p['candidate_id'] for p in app['points']],['d1','d2','d3'])
        self.assertEqual(app['points'][0]['configuration']['minimum_local_mib'],23000)
        self.assertEqual([m['trial_id'] for m in app['points'][0]['measurements']],['d1','v2-d1'])
        self.assertTrue(app['all_local']['pebs_enabled'])
        self.assertFalse(app['all_local']['policy_enabled'])
        self.assertEqual(len(app['all_local']['measurements']),2)
        data['cases']['app']['accepted']['validation']['status']='FAIL'
        self.assertEqual(records.accepted_configurations(data)['applications'],{})

    def test_replay_preserves_old_affinity_and_new_explicit_pinning(self):
        import shlex
        command=['python3','/tmp/run-sized-chameleon-apps.py','--name','old']
        self.assertIn('--no-cpu-pinning',shlex.split(records.replay(command,'new')))
        self.assertNotIn('--no-cpu-pinning',shlex.split(records.replay(command+['--cpu-pinning'],'new')))

    def test_frozen_curve_refuses_changed_config_or_duplicated_measurement(self):
        data=self.accepted_fixture();data['cases']['app']['trials']['v2-d1']['parameters']['epoch_us']=20000
        with self.assertRaisesRegex(ValueError,'parameter mismatch'):records.accepted_configurations(data)
        data=self.accepted_fixture();data['cases']['app']['trials']['v2-d1']['report']='/d1.json'
        with self.assertRaisesRegex(ValueError,'Duplicate observation'):records.accepted_configurations(data)

    def test_frozen_curve_export_and_old_protocol_does_not_overwrite(self):
        data=self.accepted_fixture()
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory);config=p/'config.json';progress=p/'curves.md'
            records.export_accepted(data,p,config,progress)
            saved=config.read_text();self.assertIn('app',json.loads(saved)['applications'])
            self.assertIn('低 (d1)',progress.read_text());self.assertIn('高 (d3)',progress.read_text())
            data['plan']['version']=7
            self.assertIsNone(records.export_accepted(data,p,config,progress))
            self.assertEqual(config.read_text(),saved)

    def setUp(self):
        self.trial = {
            'mode': 'chameleon', 'status': 'PASS', 'reference_status': 'PASS',
            'memory_mib': 32768, 'report': '/first.json', 'baseline_report': '/baseline.json',
            'parameters_requested': {'minimum_local_mib': 22000, 'psi_ppm': 1000},
            'parameters': {'minimum_local_bytes': 20000 * 2**20, 'psi_ppm': 3000,
                           'epoch_us': 10000, 'cold_folios': 2, 'free_pages': 512},
            'tracking_profile': {'sampling': 65536, 'cooling': 131072, 'hhh_interval_ms': 15000},
            'baseline_tracking_profile': {'sampling': 65536},
            'workload_identity': 'input-a', 'workload_configuration': {'threads': 8},
            'pre_reclaim_headroom_mib': 2048, 'reclaim_percent': 20.1, 'slowdown_percent': 4.2,
            'command': ['python3', '/path with spaces/run.py', '--name', 'original'],
        }
        self.data = {'plan': {'validation_repeats': 2, 'version': 8, 'reference_tolerance_pp': 1},
                     'cases': {'app': {'status': 'RUNNING', 'trials': {'d01': self.trial,
                         'b': {'mode': 'all-local', 'status': 'PASS', 'report': '/baseline.json',
                               'command': ['python3', 'run.py', '--name', 'base']}}}}}

    def test_actual_config_and_failed_repeat_are_preserved(self):
        self.data['cases']['app']['trials']['r2'] = {**self.trial, 'report': '/second.json', 'status': 'FAIL'}
        group = records.collect(self.data)['configurations'][0]
        self.assertEqual(group['configuration']['minimum_local_mib'], 20000)
        self.assertEqual(group['configuration']['psi_ppm'], 3000)
        self.assertEqual((group['point_pass_runs'], group['completed_runs']), (1, 2))
        self.assertFalse(group['measurements'][1]['point_pass'])

    def test_running_and_duplicate_reports_do_not_count_twice(self):
        self.data['cases']['app']['trials'].update(alias=dict(self.trial),
            running={**self.trial, 'status': 'RUNNING', 'report': '/running.json'})
        group = records.collect(self.data)['configurations'][0]
        self.assertEqual(group['completed_runs'], 1)

    def test_different_workload_and_denominator_do_not_merge(self):
        trials = self.data['cases']['app']['trials']
        trials['other-input'] = {**self.trial, 'workload_identity': 'input-b', 'report': '/input-b.json'}
        trials['other-baseline'] = {**self.trial, 'baseline_tracking_profile': {'sampling': 8192}, 'report': '/other.json'}
        self.assertEqual(len(records.collect(self.data)['configurations']), 3)

    def test_historical_acceptance_is_not_current_full_curve_acceptance(self):
        entry = self.data['cases']['app']
        entry['accepted'] = {'validation': {'rounds': [{'trial_ids': ['d01']}]}}
        self.assertEqual(records.collect(self.data)['configurations'][0]['full_curve_status'], 'NOT_YET_ACCEPTED')
        entry['status'] = 'ACCEPTED'
        self.data['cases']['app']['trials']['old-repeat'] = {**self.trial, 'report': '/old.json'}
        result=records.collect(self.data)
        group=result['configurations'][0]
        self.assertEqual(group['full_curve_status'], 'ACCEPTED_UNDER_CURRENT_CRITERION')
        self.assertEqual([r['in_current_accepted_curve'] for r in group['measurements']], [True,False])
        self.assertEqual(result['accepted_curves'][0]['rounds'][0]['trial_ids'], ['d01'])

    def test_tolerance_keeps_strict_result_and_rejects_comparison_errors(self):
        self.trial.update(reference_status='FAIL', reference_margin_pp={'fig7': {'target': -0.7}})
        group = records.collect(self.data)['configurations'][0]
        self.assertTrue(group['measurements'][0]['point_pass'])
        self.assertEqual(group['measurements'][0]['reference_status'], 'FAIL')
        self.trial['comparison_error'] = 'Outside reference interpolation domain'
        self.assertEqual(records.collect(self.data)['configurations'], [])

    def test_export_is_replayable_without_mutating_original(self):
        original = copy.deepcopy(self.data)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            records.export(self.data, path, path / 'progress.md')
            exported = json.loads((path / 'qualified-point-configs.json').read_text())
            group = exported['configurations'][0]
            self.assertIn("'/path with spaces/run.py'", group['replay_command'])
            self.assertIn('CHOOSE_UNIQUE_RUN_NAME', group['replay_command'])
            self.assertIn('CHOOSE_UNIQUE_ALL_LOCAL_NAME', group['measurements'][0]['baseline_replay_command'])
            with (path / 'qualified-point-configs.csv').open() as stream:
                self.assertEqual(len(list(csv.DictReader(stream))), 1)
        self.assertEqual(self.data, original)


if __name__ == '__main__':
    unittest.main()
