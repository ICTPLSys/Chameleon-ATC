#!/usr/bin/env python3
import importlib.util
import copy
import json
from pathlib import Path
import unittest
import tempfile

spec = importlib.util.spec_from_file_location('search', Path(__file__).with_name('run-chameleon-capacity-search.py'))
search = importlib.util.module_from_spec(spec)
spec.loader.exec_module(search)


class CapacitySearchTests(unittest.TestCase):
    def test_high_point_target_is_strict_and_does_not_replace_lower_points(self):
        rows=[{'trial_id':'d'+str(i),'case':'x','memory_mib':32768,'status':'PASS',
               'reference_status':'PASS','reclaim_percent':x,'slowdown_percent':y,
               'parameters_requested':{'minimum_local_mib':floor},
               'parameters':{'psi_ppm':1000,'epoch_us':10000,'cold_folios':i+1}}
              for i,(x,y,floor) in enumerate([(4,1,30000),(8,2,27000),(12,3,24000),(12,10.1,24000)])]
        entry={'memory_mib':32768,'trials':{r['trial_id']:r for r in rows},'validation_sets':[],
               'frozen_low_middle_trial_ids':['d0','d1'],'high_probe_anchor_trial_id':'d2',
               'high_slowdown_min_exclusive_percent':10}
        reference={'fig7':{'x':{'target':[(0,0),(20,40)]}},'fig8':{}}
        before=copy.deepcopy(entry)
        self.assertEqual([r['trial_id'] for r in search.pending_curve(entry,reference)],['d0','d1','d3'])
        self.assertEqual(entry,before)
        entry['trials']['d3']['parameters_requested']['minimum_local_mib']=23000
        self.assertIsNone(search.pending_curve(entry,reference))
        entry['high_probe_allow_floor_reduction']=True
        self.assertEqual([r['trial_id'] for r in search.pending_curve(entry,reference)],['d0','d1','d3'])
        self.assertEqual(search.high_slowdown_check(rows[:3],entry)['status'],'FAIL')
        for value in (10,9.9,float('nan')):
            entry['trials']['d3']['slowdown_percent']=value
            self.assertIsNone(search.pending_curve(entry,reference))
            self.assertEqual(search.high_slowdown_check([entry['trials']['d3']],entry)['status'],'FAIL')
        self.assertEqual(search.high_slowdown_check(rows[:3],{})['status'],'PASS')

    def test_repaired_baseline_id_is_scoped_to_one_application(self):
        plan={'baseline_suffix':'-pin2','applications':{
            'graphchi':{'baseline_suffix':'-pin2-spacefix1'},'pvc':{}}}
        before=copy.deepcopy(plan)
        self.assertEqual(search.fixed_baseline_id('graphchi',49152,plan),
                         'b49152-pin2-spacefix1')
        self.assertEqual(search.fixed_baseline_id('pvc',32768,plan),'b32768-pin2')
        self.assertEqual(search.fixed_baseline_id('new-app',8192,{}),'b8192')
        self.assertEqual(plan,before)

    def test_drain_finishes_child_but_blocks_next_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            stop=Path(directory)/'STOP';drain=Path(directory)/'DRAIN'
            self.assertFalse(search.stop_requested(False,stop,drain))
            drain.touch()
            self.assertTrue(search.stop_requested(False,stop,drain))
            self.assertFalse(search.stop_requested(False,stop,drain,interrupt_child=True))
            stop.touch()
            self.assertTrue(search.stop_requested(False,stop,drain,interrupt_child=True))
            stop.unlink();drain.unlink()
            self.assertTrue(search.stop_requested(True,stop,drain,interrupt_child=True))

    def verification_fixture(self):
        profile={'sampling':65536,'cooling':131072,'hhh_interval_ms':15000}
        common={'status':'PASS','memory_mib':32768,'baseline_protocol':'tracked',
                'workload_identity':'input-a','workload_configuration':{'threads':8}}
        baseline={**common,'mode':'all-local','report':'verification-baseline.json',
                  'tracking_profile':profile,'checks':{'all_local_tracking_active':True}}
        entry={'trials':{'vb':baseline},'validation_sets':[{'rounds':[{'repeat':2,'baseline':'vb',
                 'trial_ids':['old-low','old-middle','old-high']}]}]}
        for i,tag in enumerate(['d-low','d-middle','d-high']):
            row={**common,'parameters':{'epoch_us':10000,'cold_folios':i+1},
                 'tracking_profile':profile,'baseline_tracking_profile':profile,
                 'baseline_report':'search-baseline.json','pre_reclaim_headroom_mib':2048}
            entry['trials'][tag]=row
            entry['trials'][['old-low','old-middle','old-high'][i]]={**row,'baseline_report':baseline['report']}
        entry['trials']['d-low']['parameters']={'epoch_us':20000,'cold_folios':1}
        return entry,['d-low','d-middle','d-high']

    def test_replacing_one_point_retains_two_measured_verification_points(self):
        entry,ids=self.verification_fixture();before=copy.deepcopy(entry)
        # Strict reference failure is retained, not used to select a better run.
        entry['trials']['old-middle']['reference_status']='FAIL'
        result=search.reusable_verification_group(entry,ids)
        self.assertEqual(result['baseline'],'vb')
        self.assertEqual(result['trial_ids_by_candidate'],{'d-middle':'old-middle','d-high':'old-high'})
        del entry['trials']['old-middle']['reference_status']
        self.assertEqual(entry,before)

    def test_reuse_rejects_wrong_baseline_or_identity(self):
        for key,value in [('memory_mib',40960),('workload_identity','input-b'),
                          ('tracking_profile',{'sampling':8192}),('status','FAIL'),
                          ('checks',{'all_local_tracking_active':False})]:
            entry,ids=self.verification_fixture();entry['trials']['vb'][key]=value
            self.assertIsNone(search.reusable_verification_group(entry,ids),key)
        entry,ids=self.verification_fixture();entry['trials']['old-middle']['pre_reclaim_headroom_mib']=0
        self.assertIsNone(search.reusable_verification_group(entry,ids))

    def test_initial_observation_cannot_count_as_verification(self):
        entry,ids=self.verification_fixture()
        entry['validation_sets'][0]['rounds'][0]['stage']='initial measured search points'
        self.assertIsNone(search.reusable_verification_group(entry,ids))

    def test_prepared_baseline_can_resume_without_inventing_point_repeats(self):
        entry,ids=self.verification_fixture()
        entry['validation_sets']=[]
        entry['prepared_verification_baseline']='vb'
        result=search.reusable_verification_group(entry,ids)
        self.assertEqual(result['baseline'],'vb')
        self.assertEqual(result['trial_ids_by_candidate'],{})
        entry['trials']['vb']['measurement_eligible']=False
        self.assertIsNone(search.reusable_verification_group(entry,ids))
        entry['trials']['vb']['measurement_eligible']=True
        entry['trials']['vb']['memory_mib']=65536
        self.assertIsNone(search.reusable_verification_group(entry,ids))
        entry['trials']['vb']['memory_mib']=32768
        entry['trials']['d-low']['baseline_report']='verification-baseline.json'
        self.assertIsNone(search.reusable_verification_group(entry,ids))
        entry,ids=self.verification_fixture()
        entry['trials']['d-low']['baseline_report']='verification-baseline.json'
        self.assertIsNone(search.reusable_verification_group(entry,ids))

    def test_historical_floor_can_be_replayed_without_current_target_label(self):
        plan=json.loads((search.ROOT/'benchmarks/config/chameleon-range-search-v8.json').read_text())
        app=plan['applications']['spark-kmeans']
        self.assertIsNone(search.coverage_target_for(32768,app,plan,{'minimum_local_mib':21300}))
        for target,params in search.feedback.coverage_seeds(32768,app,plan):
            self.assertEqual(search.coverage_target_for(32768,app,plan,params),target)

    def test_initial_measurement_requires_shared_tracked_baseline(self):
        trials={'b':{'mode':'all-local','status':'PASS','report':'baseline.json',
                     'checks':{'all_local_tracking_active':True}}}
        for i in range(1,4):
            trials['d'+str(i)]={'trial_id':'d'+str(i),'case':'x','memory_mib':32768,'status':'PASS',
                'reference_status':'PASS','baseline_report':'baseline.json','reclaim_percent':4*i,
                'slowdown_percent':i,'parameters':{'psi_ppm':1000,'epoch_us':10000,'cold_folios':i}}
        ids=['d1','d2','d3'];entry={'trials':trials};ref={'fig7':{'x':{'target':[(0,0),(20,20)]}},'fig8':{}}
        result=search.measured_search_round(entry,ids,ref,{'include_all_local_anchor':True})
        self.assertEqual(result['baseline'],'b')
        self.assertEqual(result['check']['curve_point_count'],4)
        trials['d2']['baseline_report']='different.json'
        self.assertIsNone(search.measured_search_round(entry,ids,ref,{}))
        trials['d2']['baseline_report']='baseline.json';trials['b']['checks']['all_local_tracking_active']=False
        self.assertIsNone(search.measured_search_round(entry,ids,ref,{}))

    def test_reused_diagnostic_requires_matching_vm_workload_and_tracking(self):
        tracking={'sample_period':16384,'cooling_samples':131072,'hhh_interval_ms':15000}
        fixed={'mode':'all-local','status':'PASS','memory_mib':32768,'baseline_protocol':'tracked',
               'workload_identity':'fixed-input','workload_configuration':{'threads':8},
               'checks':{'all_local_tracking_active':True},
               'tracking_profile':{'sampling':65536,'cooling':131072,'hhh_interval_ms':15000}}
        matched={**fixed,'tracking_profile':{'sampling':16384,'cooling':131072,'hhh_interval_ms':15000}}
        entry={'trials':{'fixed':fixed,'old-match':matched}}
        self.assertEqual(search.cached_tracking_baseline(entry,'fixed',tracking),'old-match')
        for key,value in [('memory_mib',40960),('workload_identity','other-input'),('status','FAIL'),
                          ('checks',{'all_local_tracking_active':False})]:
            entry['trials']['old-match']={**matched,key:value}
            self.assertIsNone(search.cached_tracking_baseline(entry,'fixed',tracking))

    def test_fast_search_changes_vm_after_bounded_unsuccessful_search(self):
        plan=json.loads((search.ROOT/'benchmarks/config/chameleon-fast-search-v7.json').read_text())
        app=plan['applications']['spark-kmeans'];entry={'memory_mib':32768,'trials':{}}
        seed=search.feedback.coverage_seeds(32768,app,plan)[0][1]
        for i in range(6):
            entry['trials']['d'+str(i)]={'memory_mib':32768,'status':'PASS',
                'parameters_requested':{**seed,'epoch_us':10000*(i+1)},'reference_status':'FAIL','reclaim_percent':35}
        memory,params,reason=search.next_candidate(entry,app,plan)
        self.assertEqual(memory,40960)
        self.assertEqual(params,search.feedback.coverage_seeds(40960,app,plan)[0][1])
        self.assertIn('fresh all-local',reason)
        self.assertEqual(entry['memory_mib'],32768)
        for i in range(6,16):entry['trials']['d'+str(i)]=dict(entry['trials']['d0'])
        self.assertIsNone(search.next_candidate(entry,app,plan))

    def test_tracking_override_keeps_default_and_baselines_distinct(self):
        plan={'tracking':{'sample_period':65536,'cooling_samples':131072,'hhh_interval_ms':15000}}
        base=search.tracking_for(plan)
        high=search.tracking_for(plan,{'sample_period':16384,'hhh_interval_ms':5000})
        self.assertEqual(base,plan['tracking'])
        self.assertEqual(high,{'sample_period':16384,'cooling_samples':131072,'hhh_interval_ms':5000})
        self.assertEqual(search.tracking_suffix(plan,base),'')
        self.assertEqual(search.tracking_suffix(plan,high),'-s16384-c131072-h5000')
        self.assertEqual(plan['tracking']['sample_period'],65536)

    def test_repeat_must_cover_actual_low_bands_not_only_labels(self):
        rows=[{'coverage_target_percent':t,'reclaim_percent':t} for t in [5,15,25]]
        self.assertTrue(search.coverage_verified(rows,[5,15,25]))
        rows[0]['reclaim_percent']=35
        self.assertFalse(search.coverage_verified(rows,[5,15,25]))
        self.assertTrue(search.coverage_verified(rows,None))

    def test_existing_curve_can_be_validated_after_budget_exhaustion(self):
        rows=[{'trial_id':'d'+str(i),'case':'x','memory_mib':32768,'status':'PASS',
               'reference_status':'PASS','reclaim_percent':x,'slowdown_percent':y,
               'parameters':{'psi_ppm':1000,'epoch_us':10000,'cold_folios':i+1}}
              for i,(x,y) in enumerate([(4,1),(8,2),(12,3)])]
        entry={'status':'SKIPPED_AFTER_16','memory_mib':32768,'trials':{r['trial_id']:r for r in rows},'validation_sets':[]}
        reference={'fig7':{'x':{'target':[(0,0),(20,20)]}},'fig8':{}}
        self.assertEqual(len(search.pending_curve(entry,reference)),3)
        entry['validation_sets']=[{'candidate_ids':[r['trial_id'] for r in rows]}]
        self.assertIsNone(search.pending_curve(entry,reference))

    def setUp(self):
        self.plan = json.loads(search.PLAN.read_text())
        self.app = self.plan['applications']['spark-kmeans']
        self.entry = {'memory_mib': 32768, 'trials': {}}

    def add(self, status, count=1):
        for _ in range(count):
            tag = 'd%02d' % (len(self.entry['trials']) + 1)
            params=search.parameters(self.app,self.plan['policy_templates'][0])
            params['minimum_local_mib']=self.app['initial_local_floor_mib']
            self.entry['trials'][tag] = {'trial_id':tag,'memory_mib': self.entry['memory_mib'], 'status': 'PASS', 'reference_status': status,
                'parameters_requested':params,'reference_margin_pp':{'fig7':{'target':-5}},
                'diagnosis':{'flags':['free_reclaim_not_converged'],'coalesced_tick_fraction':.1,'floor_headroom_gib':12,'net_free_mib_per_second':120}}

    def test_no_fixed_six_or_eight_gib_budget(self):
        memory, params, _ = search.next_candidate(self.entry, self.app, self.plan)
        self.assertGreater(memory - params['minimum_local_mib'], 8192)
        self.assertGreater(params['cold_folios'], 0)

    def test_rate_limited_misses_shorten_cycle_not_increase_capacity(self):
        self.add('FAIL', 2)
        memory, params, reason = search.next_candidate(self.entry, self.app, self.plan)
        self.assertEqual(memory, 32768)
        self.assertEqual(params['epoch_us'],5000)
        self.assertIn('net free speed', reason)

    def test_pass_keeps_capacity_and_changes_policy(self):
        self.add('PASS')
        self.add('FAIL')
        memory, params, _ = search.next_candidate(self.entry, self.app, self.plan)
        self.assertEqual(memory, 32768)
        original=self.entry['trials']['d01']['parameters_requested']
        self.assertEqual(sum(params[k]!=original[k] for k in params),1)

    def test_does_not_expand_capacity_just_because_six_points_finished(self):
        self.add('PASS', 6)
        self.assertEqual(search.next_candidate(self.entry, self.app, self.plan)[0], 32768)

    def test_global_attempt_limit(self):
        self.entry['memory_mib'] = 49152
        self.add('FAIL', self.app['remaining_candidates'])
        self.assertIsNone(search.next_candidate(self.entry, self.app, self.plan))

    def test_all_generated_floors_fit_and_change(self):
        for app in self.plan['applications'].values():
            floors = set()
            for template in self.plan['policy_templates']:
                floor = search.parameters(app, template)['minimum_local_mib']
                self.assertEqual(floor % 2, 0)
                self.assertGreaterEqual(floor, 2048)
                self.assertLess(floor, min(app['memory_steps_mib']))
                floors.add(floor)
            self.assertGreater(len(floors), 3)


if __name__ == '__main__':
    unittest.main()
