#!/usr/bin/env python3
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('feedback',HERE/'chameleon-search-feedback.py')
feedback=importlib.util.module_from_spec(spec);spec.loader.exec_module(feedback)
spec=importlib.util.spec_from_file_location('app',HERE/'run-chameleon-apps.py')
app=importlib.util.module_from_spec(spec);spec.loader.exec_module(app)


class FeedbackTests(unittest.TestCase):
    def test_scoped_high_retune_preserves_floor_and_budget(self):
        import copy
        plan,app,entry=self.coverage()
        params=feedback.coverage_seeds(entry['memory_mib'],app,plan)[-1][1]
        entry['trials']['d01']={'trial_id':'d01','status':'PASS','parameters_requested':params}
        app={**app,'high_probe_anchor_trial_id':'d01',
             'high_probe_overrides':[{'sample_period':512},{'sample_period':512,'hhh_interval_ms':1000}]}
        before=copy.deepcopy(entry)
        memory,p,_=feedback.propose_coverage(entry,app,plan)
        self.assertEqual(p,{**params,'sample_period':512})
        self.assertEqual(memory,entry['memory_mib']);self.assertEqual(entry,before)
        entry['trials']['d02']={'trial_id':'d02','status':'PASS','parameters_requested':p}
        self.assertEqual(feedback.propose_coverage(entry,app,plan)[1],
                         {**params,'sample_period':512,'hhh_interval_ms':1000})
        app['remaining_candidates']=2
        self.assertIsNone(feedback.propose_coverage(entry,app,plan))
        app['remaining_candidates']=16;entry['trials']['d02']['status']='FAIL'
        self.assertIsNone(feedback.propose_coverage(entry,app,plan))
        entry['trials']['d02']['status']='CANCELLED_BY_USER'
        self.assertEqual(feedback.propose_coverage(entry,app,plan)[1],p)
        app['remaining_candidates']=2
        self.assertIsNone(feedback.propose_coverage(entry,app,plan))

    def test_user_scoped_floor_probe_changes_only_floor(self):
        plan,app,entry=self.coverage()
        params=feedback.coverage_seeds(entry['memory_mib'],app,plan)[-1][1]
        entry['trials']['d01']={'trial_id':'d01','status':'PASS','parameters_requested':params}
        lower=params['minimum_local_mib']-1024
        app={**app,'high_probe_anchor_trial_id':'d01','high_probe_allow_floor_reduction':True,
             'high_probe_overrides':[{'minimum_local_mib':lower}]}
        self.assertEqual(feedback.propose_coverage(entry,app,plan)[1],{**params,'minimum_local_mib':lower})
        app['high_probe_allow_floor_reduction']=False
        with self.assertRaises(ValueError):feedback.propose_coverage(entry,app,plan)


    def test_live_ycsb_failures_survive_later_zero_error_intervals(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); run=root/'timestamp';run.mkdir()
            log=run/'load.err.log'
            log.write_text('[INSERT-FAILED: Count=0, Max=0]\n')
            self.assertEqual(feedback.application_health(root,'cassandra')['status'],'NO_FAILURE_OBSERVED')
            log.write_text('Error inserting, not retrying any more.\n'
                           '[INSERT-FAILED: Count=15, Max=2008063]\n'
                           '[INSERT-FAILED: Count=0, Max=0]\n')
            health=feedback.application_health(root,'cassandra')
            self.assertEqual(health['status'],'WARN_INSERT_ERRORS')
            self.assertEqual(health['evidence'][0]['reported_failed_operations'],15)
            self.assertEqual(health['evidence'][0]['insert_worker_aborts'],1)
            (run/'run.err.log').write_text('[READ-FAILED: Count=1, Max=2000000]\n')
            self.assertEqual(feedback.application_health(root,'cassandra')['status'],'FAIL')
            self.assertIsNone(feedback.application_health(root,'liblinear'))

    def test_per_application_capacity_tracking_and_preparation_override(self):
        import copy
        plan,app,entry=self.coverage()
        original=copy.deepcopy(plan)
        scoped=copy.deepcopy(app)
        scoped['coverage_templates']=copy.deepcopy(plan['coverage_templates'])
        scoped['coverage_templates'][1]['sample_period']=32768
        scoped['coverage_templates'][1]['hhh_interval_ms']=10000
        scoped['pre_reclaim_headroom_mib']=0
        entry['trials']['d01']={'status':'FAIL','memory_mib':32768,
            'parameters_requested':feedback.coverage_seeds(32768,app,plan)[0][1]}
        entry['memory_mib']=73728
        memory,p,_=feedback.propose_coverage(entry,scoped,plan)
        self.assertEqual(memory,73728)
        self.assertGreater(p['minimum_local_mib'],32768)
        self.assertEqual(feedback.preparation_headroom(scoped,plan),0)
        self.assertEqual(feedback.preparation_headroom(app,plan),plan['pre_reclaim_headroom_mib'])
        middle=feedback.coverage_seeds(73728,scoped,plan)[1][1]
        self.assertEqual((middle['sample_period'],middle['hhh_interval_ms']),(32768,10000))
        self.assertEqual(plan,original)
        self.assertEqual(entry['trials']['d01']['status'],'FAIL')
        scoped['remaining_candidates']=1
        self.assertIsNone(feedback.propose_coverage(entry,scoped,plan))

    def test_resolved_setup_failure_can_retry_without_hiding_application_failure(self):
        plan,app,entry=self.coverage();p=feedback.coverage_seeds(32768,app,plan)[0][1]
        row={'status':'FAIL','parameters_requested':p,'setup_failure_resolved':{'evidence':'preflight.json'}}
        entry['trials']['d01']=row
        self.assertEqual(feedback.propose_coverage(entry,app,plan)[1],p)
        row['report']='application-report.json'
        self.assertIsNone(feedback.propose_coverage(entry,app,plan))
        row['setup_failure_resolved']['application_started']=False
        self.assertEqual(feedback.propose_coverage(entry,app,plan)[1],p)
        row['setup_failure_resolved']['application_started']=True
        self.assertIsNone(feedback.propose_coverage(entry,app,plan))
        row.pop('report');app['remaining_candidates']=1
        self.assertIsNone(feedback.propose_coverage(entry,app,plan))
    def test_new_execution_protocol_keeps_old_failure_but_requires_new_probe(self):
        plan,app,entry=self.coverage();plan['execution_protocol']='pinned-v1'
        p=feedback.coverage_seeds(32768,app,plan)[0][1]
        entry['trials']['d01']={'status':'FAIL','parameters_requested':p,'execution_protocol':None}
        self.assertEqual(feedback.propose_coverage(entry,app,plan)[1],p)
        self.assertEqual(entry['trials']['d01']['status'],'FAIL')
        entry['trials']['d02']={'status':'FAIL','parameters_requested':p,'execution_protocol':'pinned-v1'}
        self.assertIsNone(feedback.propose_coverage(entry,app,plan))
    def test_small_reference_crossing_can_advance_search_but_errors_cannot(self):
        plan,app,entry=self.coverage();plan['reference_tolerance_pp']=1
        seeds=feedback.coverage_seeds(32768,app,plan)
        trial={'status':'PASS','reference_status':'FAIL','parameters_requested':seeds[0][1],
               'reclaim_percent':5,'reference_margin_pp':{'fig7':{'target':-0.24}}}
        entry['trials']['d01']=trial
        self.assertEqual(feedback.propose_coverage(entry,app,plan)[1],seeds[1][1])
        self.assertEqual(trial['reference_status'],'FAIL')
        trial['comparison_error']='Outside reference interpolation domain'
        self.assertFalse(feedback.reference_probe_eligible(trial,1))
        del trial['comparison_error'];trial['reference_margin_pp']['fig7']['target']=-1.1
        self.assertFalse(feedback.reference_probe_eligible(trial,1))

    def test_high_reclaim_tracking_probe_prefers_pebs_before_hhh(self):
        plan=json.loads((HERE.parent/'config/chameleon-tracking-search-v6.json').read_text())
        app=plan['applications']['spark-kmeans'];entry={'memory_mib':32768,'trials':{}}
        for i,(target,p) in enumerate(feedback.coverage_seeds(32768,app,plan),1):
            tag='d%02d'%i
            entry['trials'][tag]={'trial_id':tag,'status':'PASS','reference_status':'PASS',
                                  'parameters_requested':p,'reclaim_percent':target}
        memory,p,reason=feedback.propose_coverage(entry,app,plan)
        self.assertEqual(memory,32768)
        self.assertEqual(p['sample_period'],8192)
        self.assertEqual(p['hhh_interval_ms'],15000)
        self.assertIn('PEBS',reason)
        entry['trials']['d04']={'trial_id':'d04','status':'PASS','reference_status':'PASS',
                              'parameters_requested':p,'reclaim_percent':43.5}
        _,p,reason=feedback.propose_coverage(entry,app,plan)
        self.assertEqual(p['hhh_interval_ms'],10000)
        self.assertIn('HHH',reason)

    def coverage(self):
        plan=json.loads((HERE.parent/'config/chameleon-lowrange-v5.json').read_text())
        app=plan['applications']['spark-kmeans']
        return plan,app,{'memory_mib':32768,'trials':{}}

    def test_repeated_low_middle_reversal_targets_middle_not_high(self):
        import copy
        plan=json.loads((HERE.parent/'config/chameleon-range-search-v8.json').read_text())
        app=plan['applications']['pvc']; memory=app['memory_steps_mib'][0]
        # This fixture models the original general search, not the later
        # user-scoped high-only campaign stored in the mutable plan.
        app.pop('high_probe_overrides',None)
        entry={'memory_mib':memory,'trials':{},'validation_sets':[]}
        for i,(target,p) in enumerate(feedback.coverage_seeds(memory,app,plan),1):
            tag='d%02d'%i
            entry['trials'][tag]={'trial_id':tag,'status':'PASS','reference_status':'PASS',
                'memory_mib':memory,'parameters_requested':p,'reclaim_percent':target,
                'execution_protocol':plan['execution_protocol'],
                'pre_reclaim_headroom_mib':app['pre_reclaim_headroom_mib'],
                'pre_reclaim_epoch_us':app['pre_reclaim_epoch_us']}
        entry['validation_sets']=[{'status':'FAIL','candidate_ids':['d01','d02','d03'],
            'rounds':[], 'aggregate_check':{'status':'FAIL','adjacent_mean_reversals':[
                {'left_index':0,'metric':'slowdown_percent','drop_pp':0.92,
                 'observed_ranges_overlap':False}]}}]
        before=copy.deepcopy(entry)
        m,p,reason=feedback.propose_coverage(entry,app,plan)
        middle=entry['trials']['d02']['parameters_requested']
        self.assertEqual((m,p['minimum_local_mib']),(memory,middle['minimum_local_mib']))
        self.assertEqual(p,{**middle,'sample_period':16384})
        self.assertIn('reversal',reason)
        self.assertEqual(entry,before)
        # One application's denser exploration must not alter other apps.
        scoped={**app,'sample_period_ladder':[4096]}
        self.assertEqual(feedback.propose_coverage(entry,scoped,plan)[1],
                         {**middle,'sample_period':4096})
        self.assertEqual(entry,before)
        app={**app,'remaining_candidates':3}
        self.assertIsNone(feedback.propose_coverage(entry,app,plan))

    def test_low_coverage_preserves_vm_and_changes_policy_not_only_floor(self):
        plan,app,entry=self.coverage()
        seeds=feedback.coverage_seeds(32768,app,plan)
        self.assertEqual(seeds[0][0],5)
        self.assertEqual(len({p['epoch_us'] for _,p in seeds}),3)
        for target,p in seeds:
            self.assertEqual(p['minimum_local_mib']%2,0)
            self.assertLessEqual(p['minimum_local_mib']+plan['pre_reclaim_headroom_mib'],32768)
        self.assertEqual(feedback.propose_coverage(entry,app,plan)[:2],(32768,seeds[0][1]))

    def test_low_coverage_failure_reduces_frequency_before_moving_band(self):
        plan,app,entry=self.coverage();p=feedback.coverage_seeds(32768,app,plan)[0][1]
        entry['trials']['d01']={'status':'PASS','reference_status':'FAIL','parameters_requested':p,'mechanism_observed':True,'reclaim_percent':5}
        memory,params,_=feedback.propose_coverage(entry,app,plan)
        self.assertEqual(memory,32768)
        self.assertEqual(params,{**p,'epoch_us':400000})
        entry['trials']['d01']['reference_status']='PASS'
        self.assertEqual(feedback.propose_coverage(entry,app,plan)[1],feedback.coverage_seeds(32768,app,plan)[1][1])

    def test_high_reclaim_cannot_masquerade_as_low_coverage(self):
        self.assertFalse(feedback.coverage_contains(35,5,[5,15.4,24.2]))
        self.assertTrue(feedback.coverage_contains(4.8,5,[5,15.4,24.2]))

    def test_coverage_edge_tolerance_does_not_accept_a_different_band(self):
        targets=[35,38.5,43.5]
        self.assertTrue(feedback.coverage_contains(36.69,38.5,targets,tolerance_pp=0.25))
        self.assertFalse(feedback.coverage_contains(36.49,38.5,targets,tolerance_pp=0.25))
        self.assertFalse(feedback.coverage_contains(34.79,38.5,targets,tolerance_pp=0.25))
        self.assertFalse(feedback.coverage_contains(36.69,38.5,targets,tolerance_pp=0))

    def test_free_only_point_does_not_force_more_cold_traffic(self):
        plan,app,entry=self.coverage();p=feedback.coverage_seeds(32768,app,plan)[0][1]
        entry['trials']['d01']={'status':'PASS','reference_status':'FAIL','parameters_requested':p,
                              'mechanism_observed':False,'reclaim_percent':4.8}
        self.assertEqual(feedback.propose_coverage(entry,app,plan)[1],{**p,'epoch_us':400000})
        entry['trials']['d01']['reference_status']='PASS'
        self.assertEqual(feedback.propose_coverage(entry,app,plan)[1],feedback.coverage_seeds(32768,app,plan)[1][1])

    def test_failed_repeat_reopens_its_band_instead_of_using_search_pass(self):
        plan,app,entry=self.coverage()
        seeds=feedback.coverage_seeds(32768,app,plan)
        for i,(target,p) in enumerate(seeds,1):
            tag='d%02d'%i
            entry['trials'][tag]={'trial_id':tag,'status':'PASS','reference_status':'PASS',
                                  'parameters_requested':p,'reclaim_percent':target}
            entry['trials']['v1r1-'+tag]={**entry['trials'][tag],'reference_status':'FAIL' if i==1 else 'PASS'}
        entry['validation_sets']=[{'status':'FAIL','candidate_ids':['d01','d02','d03'],
                                  'rounds':[{'trial_ids':['v1r1-d01','v1r1-d02','v1r1-d03']}]}]
        _,params,reason=feedback.propose_coverage(entry,app,plan)
        self.assertEqual(params,{**seeds[0][1],'epoch_us':400000})
        self.assertIn('frozen repeat',reason)
        self.assertEqual(entry['trials']['d01']['reference_status'],'PASS')
        entry['trials']['v1r1-d01']['status']='FAIL'
        self.assertIsNone(feedback.propose_coverage(entry,app,plan))

    def test_low_coverage_functional_failure_requires_diagnosis(self):
        plan,app,entry=self.coverage();p=feedback.coverage_seeds(32768,app,plan)[0][1]
        entry['trials']['d01']={'status':'FAIL','parameters_requested':p}
        self.assertIsNone(feedback.propose_coverage(entry,app,plan))

    def test_repeat_coverage_miss_reopens_middle_before_denser_high_sampling(self):
        plan=json.loads((HERE.parent/'config/chameleon-tracking-search-v6.json').read_text())
        app=plan['applications']['spark-kmeans'];entry={'memory_mib':32768,'trials':{}}
        seeds=feedback.coverage_seeds(32768,app,plan)
        ids=[]
        for i,(target,p) in enumerate(seeds,1):
            tag='d%02d'%i; ids.append(tag)
            entry['trials'][tag]={'trial_id':tag,'status':'PASS','reference_status':'PASS',
                                  'parameters_requested':p,'reclaim_percent':target}
            entry['trials']['v1r1-'+tag]={**entry['trials'][tag],
                                        'reclaim_percent':34.79 if i==2 else target}
        entry['validation_sets']=[{'status':'FAIL','candidate_ids':ids,
                                  'rounds':[{'trial_ids':['v1r1-'+tag for tag in ids]}]}]
        memory,p,reason=feedback.propose_coverage(entry,app,plan)
        self.assertEqual(memory,32768)
        self.assertEqual(p,{**seeds[1][1],'epoch_us':20000})
        self.assertIn('measured reclamation band',reason)
        self.assertEqual(entry['trials']['d02']['reference_status'],'PASS')

    def test_diagnosed_preparation_change_can_repeat_runtime_parameters(self):
        plan,app,entry=self.coverage();plan['pre_reclaim_headroom_mib']=512
        seed=feedback.coverage_seeds(32768,app,plan)[0][1]
        entry['trials']['d01']={'status':'PASS','reference_status':'FAIL','parameters_requested':seed,
                              'pre_reclaim':{'headroom_mib':0},'mechanism_observed':False}
        self.assertEqual(feedback.propose_coverage(entry,app,plan)[1],seed)
        self.assertIn('d01',entry['trials'])

    def trial(self, tag, cold, floor, reclaim, slowdown, cold_mean, flags=None):
        return {'trial_id':tag, 'memory_mib':32768, 'status':'PASS', 'reference_status':'PASS',
                'parameters_requested':{'psi_ppm':1000,'epoch_us':10000,'cold_folios':cold,'minimum_local_mib':floor},
                'reclaim_percent':reclaim,'slowdown_percent':slowdown,'cold_mean_percent':cold_mean,
                'reference_margin_pp':{'fig7':{'target':50-slowdown}},
                'diagnosis':{'flags':flags or []}}

    def propose(self, rows):
        return feedback.propose({'memory_mib':32768,'trials':{r['trial_id']:r for r in rows}},
                                {'memory_steps_mib':[32768,40960]}, {'max_candidates':16}, {})

    def test_cold_batch_plateau_keeps_cold_and_changes_floor(self):
        low=self.trial('d01',1,20210,37.91,3.08,.078,['local_floor_reached'])
        high=self.trial('d02',2,20210,38.50,15.32,.377,['local_floor_reached'])
        memory,params,reason=self.propose([low,high])
        self.assertEqual(memory,32768)
        self.assertEqual(params,{**high['parameters_requested'],'minimum_local_mib':19186})
        self.assertIn('Matched cold-batch probe',reason)

    def test_functional_failure_is_not_used_as_a_performance_anchor(self):
        row=self.trial('d01',1,20210,40,3,.01,['local_floor_reached'])
        row.update(status='FAIL',reference_status='FAIL')
        self.assertIsNone(self.propose([row]))

    def test_nonmatched_trials_do_not_establish_cold_plateau(self):
        low=self.trial('d01',1,21234,37.91,3.08,.078,['local_floor_reached'])
        high=self.trial('d02',2,20210,38.50,15.32,.377,['local_floor_reached'])
        _,_,reason=self.propose([low,high])
        self.assertNotIn('Matched cold-batch probe',reason)

    def test_extend_increasing_pair_from_higher_reclaim(self):
        low=self.trial('d01',1,20210,37.91,3.08,.078)
        high=self.trial('d02',2,19186,41.50,15.32,.377)
        _,params,reason=self.propose([low,high])
        self.assertEqual(params,{**high['parameters_requested'],'cold_folios':3})
        self.assertIn('Extend measured increasing pair',reason)

    def test_p95_must_also_increase_before_extending_pair(self):
        low=self.trial('d01',1,20210,37.91,3.08,.078)
        high=self.trial('d02',2,19186,41.50,15.32,.377)
        low['p95_slowdown_percent']=10
        high['p95_slowdown_percent']=5
        _,_,reason=self.propose([low,high])
        self.assertNotIn('Extend measured increasing pair',reason)

    def test_outside_domain_corrects_capacity_without_expanding_vm(self):
        row=self.trial('d01',1,20000,40,20,.05,['local_floor_reached','cold_reclaim_too_small'])
        row.update(case='example',reference_status='FAIL',comparison_error='Outside reference interpolation domain')
        ref={'fig7':{'example':{'a':[[0,0],[28,50]],'b':[[0,0],[36,50]]}}}
        entry={'memory_mib':32768,'trials':{'d01':row}}
        memory,params,reason=feedback.propose(entry,{'memory_steps_mib':[32768,40960]}, {'max_candidates':16},{},ref)
        self.assertEqual(memory,32768)
        self.assertGreater(params['minimum_local_mib'],row['parameters_requested']['minimum_local_mib']+1024)
        self.assertEqual(params['cold_folios'],1)
        self.assertIn('no extrapolation or VM expansion',reason)
        # An already tested correction must not fall through to more reclaim.
        other=json.loads(json.dumps(row));other.update(trial_id='d02',parameters_requested=params,diagnosis=None)
        entry['trials']['d02']=other
        self.assertIsNone(feedback.propose(entry,{'memory_steps_mib':[32768,40960]}, {'max_candidates':16},{},ref))

    def test_new_all_local_allows_tracking_but_rejects_reclamation(self):
        b={'tracker':{'enabled':1},'manager':{'enabled':1,'selected':0},
           'policy':{k:0 for k in ['enabled','lease_active','epochs','free_reclaimed_bytes','free_returned_bytes','shadow_prepared_pages']},
           'rdma':{'write_bytes':0,'read_bytes':0},'retired_bytes':0}
        e=json.loads(json.dumps(b));e['manager']['split_ok']=2
        self.assertTrue(app.all_local_tracking_active(b,e))
        e['rdma']['write_bytes']=4096
        self.assertFalse(app.all_local_tracking_active(b,e))
        e=json.loads(json.dumps(b));e['tracker']['enabled']=0
        self.assertFalse(app.all_local_tracking_active(b,e))

    def test_all_local_rejects_activity_even_if_end_switch_is_off(self):
        fields={'tracker':['enabled','hardware_samples','processed_samples'],
                'manager':['enabled','epochs','scanned','split_ok','selected'],
                'policy':['enabled','lease_active','epochs','free_reclaimed_bytes','free_returned_bytes','shadow_prepared_pages'],
                'rdma':['write_bytes','read_bytes']}
        b={g:{k:0 for k in keys} for g,keys in fields.items()}
        e=json.loads(json.dumps(b))
        self.assertTrue(app.all_local_inactive(b,e))
        e['manager']['selected']=1
        self.assertFalse(app.all_local_inactive(b,e))

    def test_wait_for_both_guest_and_host_ack(self):
        target=16*feedback.GIB
        row={'policy':{'local_bytes':target},'qmp':{'policy':{'local-bytes':target+feedback.GIB}}}
        self.assertFalse(app.pre_reclaim_reached(row,target))
        row['qmp']['policy']['local-bytes']=target
        self.assertTrue(app.pre_reclaim_reached(row,target))

    def test_partial_json_line_is_deferred_and_not_duplicated(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'samples.jsonl';reader=feedback.LiveSamples(p)
            p.write_text('{"phase":"running"')
            self.assertEqual(reader.read(),[])
            with p.open('a') as f:f.write('}\n')
            self.assertEqual(reader.read(),[{'phase':'running'}])
            self.assertEqual(reader.read(),[{'phase':'running'}])

    def test_pre_reclaim_not_counted_as_running_window(self):
        rows=[{'phase':'pre_reclaim','seconds':0}]
        self.assertIsNone(feedback.analyze(rows,32768,{'minimum_local_mib':16384}))

    def test_floor_near_is_distinct_from_reached_and_all_local_is_inactive(self):
        params={'minimum_local_mib':68076};memory=81920
        def samples(local,enabled):
            return [{'phase':'running','seconds':second,'retired_bytes':0,'application_rss_bytes':0,
                     'policy':{'enabled':enabled,'local_bytes':local*feedback.MIB},
                     'meminfo':{'MemAvailable':10*feedback.GIB},'rdma':{},'shadow':{},'manager':{},
                     'compute_memory':{'Rss':local*feedback.MIB}} for second in (0,10)]
        near=feedback.analyze(samples(69076,1),memory,params)
        self.assertIn('local_floor_near',near['flags'])
        self.assertNotIn('local_floor_reached',near['flags'])
        reached=feedback.analyze(samples(68076,1),memory,params)
        self.assertIn('local_floor_reached',reached['flags'])
        inactive=feedback.analyze(samples(0,0),memory,params)
        self.assertFalse(inactive['policy_active'])
        self.assertNotIn('local_floor_reached',inactive['flags'])
        self.assertNotIn('cold_reclaim_not_observed',inactive['flags'])

    def test_worker_pressure_reduces_cold_batch_first(self):
        params={'psi_ppm':10000,'epoch_us':10000,'cold_folios':8,'minimum_local_mib':16384}
        r={'trial_id':'d01','memory_mib':32768,'status':'PASS','parameters_requested':params,
           'reference_status':'FAIL','reference_margin_pp':{'fig7':{'target':-20}},
           'diagnosis':{'flags':['policy_worker_busy','cold_readback_pressure'],'rdma_read_gib':12,'cold_peak_gib':2}}
        result=feedback.propose({'memory_mib':32768,'trials':{'d01':r}},{'memory_steps_mib':[32768,40960]},{'max_candidates':16},params)
        self.assertEqual(result[0],32768)
        self.assertEqual(result[1],{**params,'cold_folios':4})

    def test_policy_error_is_visible_without_a_transport_error(self):
        rows=[{'phase':'running','seconds':t,'retired_bytes':0,'application_rss_bytes':0,
               'policy':{'enabled':1,'local_bytes':16*feedback.GIB},
               'meminfo':{'MemAvailable':2*feedback.GIB},'rdma':{},'shadow':{},'manager':{},
               'compute_memory':{'Rss':16*feedback.GIB}} for t in (0,10)]
        rows[-1]['policy'].update(action_errors=1,last_error=-22)
        result=feedback.analyze(rows,32768,{'minimum_local_mib':16384})
        self.assertIn('policy_error',result['flags'])
        self.assertNotIn('transport_error',result['flags'])
        self.assertEqual(result['policy_action_errors'],1)
        self.assertEqual(result['policy_last_error'],-22)
        rows[-1]['shadow']['data_load_failure']=1
        self.assertIn('data_path_error',feedback.analyze(rows,32768,{'minimum_local_mib':16384})['flags'])


if __name__=='__main__':unittest.main()
