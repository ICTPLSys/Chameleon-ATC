#!/usr/bin/env python3
import copy,importlib.util,unittest
from pathlib import Path
s=importlib.util.spec_from_file_location('m',Path(__file__).with_name('chameleon-tuning-metrics.py'));m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
class Acceptance(unittest.TestCase):
 def overlap_fixture(self):
  rows=self.rows();rows[-1].update(reclaim_percent=24,slowdown_percent=6)
  ref=self.reference();ref['comparison_domains']={'x':'per-baseline-overlap'}
  ref['fig7']['x']['B']=[(0,0),(30,30)]
  for row in rows:row.update(m.reference_point(row,ref))
  return rows,ref
 def test_overlap_point_does_not_extrapolate_missing_baseline(self):
  rows,ref=self.overlap_fixture();before=copy.deepcopy(ref)
  self.assertEqual(rows[-1]['reference_status'],'PASS')
  self.assertNotIn('A',rows[-1]['reference_margin_pp']['fig7'])
  self.assertEqual(rows[-1]['reference_point_coverage']['fig7']['A']['status'],'OUTSIDE_REFERENCE_DOMAIN')
  result=m.curve_check(rows,ref,include_all_local=True)
  self.assertEqual(result['status'],'PASS')
  self.assertEqual(result['reference_compared_intervals']['fig7']['A']['compared_domain'],[0,20])
  self.assertEqual(ref,before)
 def test_overlap_curve_checks_clipped_endpoint_and_interior_crossings(self):
  rows,ref=self.overlap_fixture();ref['fig7']['x']['A']=[(0,0),(8,10),(20,3)]
  self.assertEqual(m.curve_check(rows,ref,include_all_local=True)['status'],'FAIL')
  rows,ref=self.overlap_fixture();ref['fig7']['x']['A']=[(0,0),(8,10),(14,.1),(20,20)]
  self.assertEqual(m.curve_check(rows,ref,include_all_local=True)['status'],'FAIL')
 def test_overlap_outside_all_baselines_needs_curve_overlap(self):
  rows,ref=self.overlap_fixture();rows[-1]['reclaim_percent']=40
  rows[-1].update(m.reference_point(rows[-1],ref))
  self.assertEqual(rows[-1]['reference_status'],'OUTSIDE_DOMAIN')
  self.assertEqual(m.curve_check(rows,ref,include_all_local=True)['status'],'PASS')
  for row in rows:row['reclaim_percent']+=50;row.update(m.reference_point(row,ref))
  self.assertEqual(m.curve_check(rows,ref)['status'],'FAIL')
 def test_overlap_repeats_and_legacy_domain_remain_distinct(self):
  rows,ref=self.overlap_fixture()
  result=m.repeated_curve_check([rows,copy.deepcopy(rows)],ref,required_repeats=2,include_all_local=True)
  self.assertEqual(result['status'],'PASS')
  ref.pop('comparison_domains')
  with self.assertRaisesRegex(ValueError,'Outside reference'):m.reference_point(rows[-1],ref)
  self.assertEqual(m.repeated_curve_check([rows,copy.deepcopy(rows)],ref,required_repeats=2)['status'],'FAIL')
 def test_explicit_mean_crossing_tolerance_retains_strict_failure(self):
  rows,ref=self.flat_memcached_fixture()
  ref={'fig7':{'memcached':{'A':[(0,0),(5,2),(20,20)]}},'fig8':{'memcached':{'A':[(0,0),(5,2),(20,20)]}}}
  for i,r in enumerate(rows):
   r['p95_slowdown_percent']=[2,3,5][i]
   r['reference_margin_pp']={fig:{'A':m.interpolate(ref[fig]['memcached']['A'],r['reclaim_percent'])-r[metric]} for fig,metric in [('fig7','slowdown_percent'),('fig8','p95_slowdown_percent')]}
   r['reference_status']='FAIL' if i==0 else 'PASS'
  kwargs=dict(required_repeats=2,reference_tolerance_pp=1,flat_throughput_tolerance_pp=1)
  rounds=[copy.deepcopy(rows),copy.deepcopy(rows)]
  self.assertEqual(m.repeated_curve_check(rounds,ref,**kwargs)['status'],'FAIL')
  result=m.repeated_curve_check(rounds,ref,mean_reference_tolerance_pp=1,**kwargs)
  self.assertEqual(result['status'],'PASS')
  self.assertEqual(result['strict_mean_curve_check']['status'],'FAIL')
  self.assertAlmostEqual(result['mean_curve_check']['max_reference_excess_pp'],.4)
  self.assertEqual(m.repeated_curve_check(rounds,ref,mean_reference_tolerance_pp=.1,**kwargs)['status'],'FAIL')
 def test_shared_denominator_preserves_round_normalization_without_changing_raw_data(self):
  run,fixed,matched=self.tracking_comparison()
  paired=copy.deepcopy(fixed);paired.update(report='second-round.json',performance={'cost':90,'p95_us':90,'p99_us':90})
  before=copy.deepcopy(run)
  value=m.compare_shared_baseline(run,fixed,paired,matched,self.reference())
  self.assertAlmostEqual(value['slowdown_percent'],21)
  self.assertAlmostEqual(value['per_repeat_normalization']['slowdown_percent'],100*(121/90-1))
  self.assertEqual(value['baseline_report'],'fixed.json')
  self.assertEqual(value['per_repeat_normalization']['baseline_report'],'second-round.json')
  self.assertEqual(run,before)
 def test_fixed_denominator_cannot_change_between_repeats(self):
  runs=self.repeated([(1,2,3)]*3)
  for rows in runs:
   for row in rows:row.update(normalization_protocol='fixed-first-capacity-all-local-v1',baseline_report='first.json')
  self.assertEqual(m.repeated_curve_check(runs,self.reference())['status'],'PASS')
  runs[1][0]['baseline_report']='different.json'
  self.assertEqual(m.repeated_curve_check(runs,self.reference())['reason'],'Fixed normalization baseline differs between repeats')
 def flat_memcached_fixture(self):
  rows=self.rows()
  for i,r in enumerate(rows):
   r.update(case='memcached',slowdown_percent=[.01,0,0][i],p95_slowdown_percent=i+1)
  target=self.reference()['fig7']['x']
  return rows,{'fig7':{'memcached':target},'fig8':{'memcached':target}}
 def test_memcached_flat_throughput_is_explicit_and_p95_still_must_increase(self):
  rows,ref=self.flat_memcached_fixture()
  self.assertEqual(m.curve_check(rows,ref)['status'],'FAIL')
  self.assertEqual(m.curve_check(rows,ref,flat_throughput_tolerance_pp=1)['status'],'PASS')
  for r in rows:r['p95_slowdown_percent']=1
  self.assertEqual(m.curve_check(rows,ref,flat_throughput_tolerance_pp=1)['status'],'FAIL')
  self.assertFalse(m.near_flat_memcached_throughput(self.rows(),1))
 def test_near_flat_memcached_keeps_bounded_noise_and_reference_checks(self):
  rows,ref=self.flat_memcached_fixture();rows[0]['slowdown_percent']=.9;rows[-1]['slowdown_percent']=-.9
  self.assertEqual(m.curve_check(rows,ref,flat_throughput_tolerance_pp=1)['status'],'FAIL')
  rows,ref=self.flat_memcached_fixture();rows[-1].update(p95_slowdown_percent=30,reference_status='FAIL')
  self.assertEqual(m.curve_check(rows,ref,flat_throughput_tolerance_pp=1)['status'],'FAIL')
 def test_near_flat_memcached_requires_two_complete_compatible_observations(self):
  rows,ref=self.flat_memcached_fixture();rounds=[copy.deepcopy(rows),copy.deepcopy(rows)]
  kwargs=dict(required_repeats=2,flat_throughput_tolerance_pp=1)
  result=m.repeated_curve_check(rounds,ref,**kwargs)
  self.assertEqual(result['status'],'PASS')
  self.assertTrue(result['mean_curve_check']['near_flat_throughput'])
  self.assertEqual(m.repeated_curve_check(rounds[:1],ref,**kwargs)['status'],'FAIL')
  rounds[1][0]['slowdown_percent']=2.1
  self.assertEqual(m.repeated_curve_check(rounds,ref,**kwargs)['status'],'FAIL')
 def test_repeats_cannot_mix_preparation_epochs(self):
  runs=self.repeated([(1,2,3)]*3)
  for rows in runs:
   for row in rows:row['pre_reclaim']={'epoch_us':10000,'headroom_mib':0}
  self.assertEqual(m.repeated_curve_check(runs,self.reference())['status'],'PASS')
  runs[1][0]['pre_reclaim']['epoch_us']=100000
  result=m.repeated_curve_check(runs,self.reference())
  self.assertEqual(result['status'],'FAIL')
  self.assertIn('pre_reclaim_epoch_us',result['reason'])
 def test_cpu_affinity_must_match_fixed_and_diagnostic_baselines(self):
  run,fixed,matched=self.tracking_comparison()
  run['cpu_affinity_profile']={'vcpu_host_cpus':[0,1]}
  self.assertEqual(m.compare_fixed_tracking(run,fixed,matched,self.reference())['comparison_error'],'Baseline CPU affinity differs')
  fixed['cpu_affinity_profile']=run['cpu_affinity_profile']
  self.assertEqual(m.compare_fixed_tracking(run,fixed,matched,self.reference())['status'],'FAIL')
  matched['cpu_affinity_profile']=run['cpu_affinity_profile']
  self.assertNotIn('comparison_error',m.compare_fixed_tracking(run,fixed,matched,self.reference()))
 def tracking_comparison(self):
  common=dict(status='PASS',case='x',memory_mib=20000,workload_configuration={},baseline_protocol='all-local-pebs-hhh-on-v1',
              tracking_profile={'sampling':65536,'cooling':131072,'hhh_interval_ms':15000})
  fixed={**common,'report':'fixed.json','reclaim_percent':0,'performance':{'cost':100,'p95_us':100,'p99_us':100}}
  matched={**fixed,'report':'matched.json','performance':{'cost':110,'p95_us':110,'p99_us':110},'tracking_profile':{**common['tracking_profile'],'sampling':16384}}
  run={**matched,'report':'run.json','reclaim_percent':12,'performance':{'cost':121,'p95_us':121,'p99_us':121}}
  return run,fixed,matched
 def test_explicit_fixed_baseline_keeps_tracking_cost_in_total(self):
  run,fixed,matched=self.tracking_comparison()
  self.assertEqual(m.compare(run,fixed,self.reference())['comparison_error'],'Baseline tracking protocol/profile differs')
  value=m.compare_fixed_tracking(run,fixed,matched,self.reference())
  self.assertAlmostEqual(value['slowdown_percent'],21)
  self.assertAlmostEqual(value['tracking_only_overhead_percent'],10)
  self.assertAlmostEqual(value['reclaim_over_matched_tracking_percent'],10)
  self.assertAlmostEqual(value['p95_slowdown_percent'],21)
  self.assertEqual(value['baseline_report'],'fixed.json')
  self.assertEqual(value['matched_tracking_baseline_report'],'matched.json')
 def test_fixed_baseline_does_not_allow_wrong_matched_configuration(self):
  run,fixed,matched=self.tracking_comparison();matched['memory_mib']=10000
  self.assertEqual(m.compare_fixed_tracking(run,fixed,matched,self.reference())['status'],'FAIL')
  run,fixed,matched=self.tracking_comparison();matched['status']='FAIL'
  self.assertEqual(m.compare_fixed_tracking(run,fixed,matched,self.reference())['status'],'FAIL')
 def test_fixed_tracking_cannot_mix_protocol_or_workload(self):
  run,fixed,matched=self.tracking_comparison();fixed['baseline_protocol']='all-local-controls-off-legacy'
  self.assertIn('comparison_error',m.compare_fixed_tracking(run,fixed,matched,self.reference()))
  run,fixed,matched=self.tracking_comparison();fixed['workload_configuration']={'different':True}
  self.assertEqual(m.compare_fixed_tracking(run,fixed,matched,self.reference())['comparison_error'],'Baseline workload/VM differs')
 def test_cannot_mix_old_off_and_new_tracked_baselines(self):
  run={'status':'PASS','baseline_protocol':'all-local-pebs-hhh-on-v1'}
  baseline={'status':'PASS','baseline_protocol':'all-local-controls-off-legacy'}
  self.assertEqual(m.compare(run,baseline,{})['comparison_error'],'Baseline tracking protocol/profile differs')
 def reference(self):return {'fig7':{'x':{'A':[(0,0),(20,20)],'B':[(0,0),(20,25)]}},'fig8':{}}
 def rows(self):
  return [dict(case='x',trial_id=str(i),memory_mib=20000,reclaim_percent=x,slowdown_percent=y,cold_mean_percent=x/2,reference_status='PASS',parameters={'psi_ppm':1000,'epoch_us':10000,'cold_folios':i+1}) for i,(x,y) in enumerate([(4,1),(8,2),(12,3)])]
 def test_accepts_monotonic_below_both(self):self.assertEqual(m.curve_check(self.rows(),self.reference())['status'],'PASS')
 def test_noisy_search_point_can_be_repeated_but_does_not_relax_mean(self):
  rows=self.rows()
  for r,y in zip(rows,[4.2,5,6]):r['slowdown_percent']=y
  rows[0].update(reference_status='FAIL',reference_margin_pp={'fig7':{'A':-0.2,'B':0.8}})
  before=copy.deepcopy(rows)
  self.assertIsNone(m.find_curve(rows,self.reference()))
  self.assertIsNotNone(m.find_curve(rows,self.reference(),reference_tolerance_pp=1))
  result=m.repeated_curve_check([rows,copy.deepcopy(rows)],self.reference(),required_repeats=2,reference_tolerance_pp=1)
  self.assertEqual(result['status'],'FAIL')
  self.assertEqual(rows,before)
 def test_all_local_origin_does_not_replace_a_measured_middle_range(self):
  limits={'low_max_percent':5,'middle_min_percent':6,'middle_max_percent':10,'high_min_percent':11}
  self.assertEqual(m.curve_check(self.rows(),self.reference(),include_all_local=True,reclaim_range=limits)['status'],'PASS')
  rows=self.rows()
  for r,x in zip(rows,[10,14,18]):r['reclaim_percent']=x
  self.assertEqual(m.curve_check(rows,self.reference(),include_all_local=True,reclaim_range=limits)['status'],'FAIL')
 def test_origin_alone_does_not_establish_reclaimed_point_trend(self):
  rows=self.rows()
  for r in rows:r['slowdown_percent']=1
  self.assertEqual(m.curve_check(rows,self.reference(),include_all_local=True)['status'],'PASS')
  self.assertEqual(m.curve_check(rows,self.reference(),include_all_local=True,trend_from_reclaimed_only=True)['status'],'FAIL')
 def test_large_mean_drop_is_not_allowed_just_because_ranges_overlap(self):
  runs=self.repeated([(1,2,3),(6,2,7)])
  ref={'fig7':{'x':{'A':[(0,0),(20,40)],'B':[(0,0),(20,50)]}},'fig8':{}}
  self.assertEqual(m.repeated_curve_check(runs,ref,required_repeats=2,include_all_local=True)['status'],'PASS')
  result=m.repeated_curve_check(runs,ref,required_repeats=2,include_all_local=True,max_mean_reversal_pp=1)
  self.assertEqual(result['status'],'FAIL')
  self.assertIn('small fluctuation',result['reason'])
 def test_two_repeats_include_origin_and_average_both_runs(self):
  runs=self.repeated([(1,2,3),(2,3,4)]);before=copy.deepcopy(runs)
  result=m.repeated_curve_check(runs,self.reference(),required_repeats=2,include_all_local=True)
  self.assertEqual(result['status'],'PASS')
  self.assertEqual(result['mean_curve_check']['curve_point_count'],4)
  self.assertEqual(result['mean_points'][0]['slowdown_percent'],1.5)
  self.assertEqual(result['required_repeats'],2)
  self.assertEqual(runs,before)
  self.assertEqual(m.repeated_curve_check(runs[:1],self.reference(),required_repeats=2)['status'],'FAIL')
 def test_origin_segment_must_also_stay_below_targets(self):
  ref=self.reference();ref['fig7']['x']['A']=[(0,0),(2,.1),(4,4),(20,20)]
  self.assertEqual(m.curve_check(self.rows(),ref)['status'],'PASS')
  self.assertEqual(m.curve_check(self.rows(),ref,include_all_local=True)['status'],'FAIL')
 def test_coverage_curve_must_include_each_requested_band(self):
  rows=self.rows()
  for r in rows:r['coverage_target_percent']=5
  self.assertIsNone(m.find_curve(rows,self.reference(),required_targets=[5,10,15]))
  for r,t in zip(rows,[5,10,15]):r['coverage_target_percent']=t
  self.assertIsNotNone(m.find_curve(rows,self.reference(),required_targets=[5,10,15]))
 def test_rejects_interior_crossing_despite_endpoints(self):
  r=self.reference();r['fig7']['x']['A']=[(0,0),(4,8),(6,0.1),(8,9),(20,20)]
  self.assertEqual(m.curve_check(self.rows(),r)['status'],'FAIL')
 def test_rejects_memory_only_and_flat_performance(self):
  rows=self.rows()
  for r in rows:r['slowdown_percent']=1
  self.assertEqual(m.curve_check(rows,self.reference())['status'],'FAIL')
  rows=self.rows()
  for r in rows:r['parameters']['cold_folios']=1
  self.assertEqual(m.curve_check(rows,self.reference())['status'],'FAIL')
 def test_free_dominated_curve_does_not_require_cold_growth(self):
  rows=self.rows()
  for i,r in enumerate(rows):r['cold_mean_percent']=.001/(i+1)
  self.assertEqual(m.curve_check(rows,self.reference())['status'],'PASS')
 def test_tracking_frequency_can_be_the_policy_tradeoff(self):
  rows=self.rows()
  for r,period in zip(rows,[65536,32768,16384]):
   r['parameters']['cold_folios']=1
   r['tracking_profile']={'sampling':period,'hhh_interval_ms':15000}
  self.assertEqual(m.curve_check(rows,self.reference())['status'],'PASS')
 def test_small_but_real_cold_rdma_is_sufficient(self):
  run={'cold_peak_mib':1/256,'counters':{'rdma':{'write_bytes':4096,'read_bytes':4096}}}
  self.assertTrue(m.mechanism_observed(run))
  run['counters']['rdma']['read_bytes']=0
  self.assertFalse(m.mechanism_observed(run))
  run['counters']['rdma']['read_bytes']=4096;run['cold_peak_mib']=0
  self.assertFalse(m.mechanism_observed(run))
 def test_free_only_point_compares_without_fabricating_rdma(self):
  run=dict(status='PASS',case='x',memory_mib=20000,workload_configuration={},
           reclaim_percent=4,cold_peak_mib=0,free_mean_mib=800,
           counters={'rdma':{'write_bytes':0,'read_bytes':0}},performance={'cost':101})
  baseline={**run,'report':'baseline.json','reclaim_percent':0,'performance':{'cost':100}}
  result=m.compare(run,baseline,self.reference())
  self.assertEqual(result['reference_status'],'PASS')
  self.assertFalse(result['mechanism_observed'])
  self.assertEqual(result['counters']['rdma'],{'write_bytes':0,'read_bytes':0})
  run['status']='FAIL'
  self.assertEqual(m.compare(run,baseline,self.reference())['reference_status'],'FAIL')
 def test_missing_reference_is_not_vacuously_accepted(self):
  run=dict(status='PASS',case='x',memory_mib=20000,workload_configuration={},
           reclaim_percent=4,performance={'cost':101})
  baseline={**run,'report':'baseline.json','reclaim_percent':0,'performance':{'cost':100}}
  self.assertEqual(m.compare(run,baseline,{'fig7':{},'fig8':{}})['reference_status'],'FAIL')
 def test_rejects_one_failed_repeat_point(self):
  rows=self.rows();rows[1]['reference_status']='FAIL';self.assertEqual(m.curve_check(rows,self.reference())['status'],'FAIL')
 def test_no_extrapolation(self):
  with self.assertRaises(ValueError):m.interpolate([(0,0),(10,5)],11)
 def repeated(self,values):
  runs=[]
  for ys in values:
   rows=self.rows()
   for r,y in zip(rows,ys):r['slowdown_percent']=y
   runs.append(rows)
  return runs
 def test_small_mean_reversal_with_overlapping_repeats_is_allowed(self):
  runs=self.repeated([(1,3.1,3.0),(1.1,3.0,3.2),(.9,3.3,3.0)])
  before=copy.deepcopy(runs)
  result=m.repeated_curve_check(runs,self.reference())
  self.assertEqual(result['status'],'PASS')
  self.assertTrue(result['adjacent_mean_reversals'][0]['observed_ranges_overlap'])
  self.assertEqual(runs,before)
 def test_consistent_reverse_trend_is_not_called_noise(self):
  runs=self.repeated([(1,3,2.8)]*3)
  self.assertEqual(m.repeated_curve_check(runs,self.reference())['status'],'FAIL')
 def test_single_repeat_can_be_flat_but_flat_mean_is_rejected(self):
  runs=self.repeated([(1,1,1)]*3)
  self.assertEqual(m.curve_check(runs[0],self.reference(),check_trend=False)['status'],'PASS')
  self.assertEqual(m.repeated_curve_check(runs,self.reference())['status'],'FAIL')
 def test_low_range_coverage_can_be_flat_without_claiming_overall_trend(self):
  runs=self.repeated([(1,1,1)]*3)
  result=m.repeated_curve_check(runs,self.reference(),check_trend=False)
  self.assertEqual(result['status'],'PASS')
  self.assertFalse(result['overall_trend_required'])
  self.assertIsNone(m.find_curve(runs[0],self.reference()))
  self.assertIsNotNone(m.find_curve(runs[0],self.reference(),check_trend=False))
  runs[1][1]['reference_status']='FAIL'
  self.assertEqual(m.repeated_curve_check(runs,self.reference(),check_trend=False)['status'],'FAIL')
 def excursion_runs(self,values):
  runs=self.repeated([(y,2,3) for y in values])
  for rows in runs:
   r=rows[0]
   r['reference_margin_pp']={'fig7':{'A':4-r['slowdown_percent'],'B':5-r['slowdown_percent']}}
   r['reference_status']='PASS' if r['slowdown_percent']<4 else 'FAIL'
  return runs
 def test_small_repeat_excursion_allowed_but_raw_status_retained(self):
  runs=self.excursion_runs([4.7,1,1]);before=copy.deepcopy(runs)
  result=m.repeated_curve_check(runs,self.reference(),check_trend=False,reference_tolerance_pp=1)
  self.assertEqual(result['status'],'PASS')
  self.assertAlmostEqual(result['repeat_checks'][0]['max_reference_excess_pp'],.7)
  self.assertEqual(result['mean_curve_check']['reference_tolerance_pp'],0)
  self.assertEqual(runs,before)
 def test_tolerance_does_not_allow_persistent_mean_above_reference(self):
  runs=self.excursion_runs([4.3]*3)
  self.assertEqual(m.repeated_curve_check(runs,self.reference(),False,1)['status'],'FAIL')
 def test_excess_beyond_one_pp_and_functional_errors_still_fail(self):
  runs=self.excursion_runs([5.1,1,1])
  self.assertEqual(m.repeated_curve_check(runs,self.reference(),False,1)['status'],'FAIL')
  runs=self.excursion_runs([4.7,1,1]);runs[0][0]['status']='FAIL'
  self.assertEqual(m.repeated_curve_check(runs,self.reference(),False,1)['status'],'FAIL')
 def test_interior_crossing_tolerance_is_bounded(self):
  ref=self.reference();ref['fig7']['x']['A']=[(0,0),(4,8),(6,.8),(8,9),(20,20)]
  self.assertEqual(m.curve_check(self.rows(),ref,reference_tolerance_pp=1)['status'],'PASS')
  ref['fig7']['x']['A'][2]=(6,.1)
  self.assertEqual(m.curve_check(self.rows(),ref,reference_tolerance_pp=1)['status'],'FAIL')
 def test_each_repeat_must_still_beat_references(self):
  runs=[self.rows() for _ in range(3)];runs[1][1]['reference_status']='FAIL'
  self.assertEqual(m.repeated_curve_check(runs,self.reference())['status'],'FAIL')
 def test_requires_three_matching_frozen_configurations(self):
  runs=[self.rows() for _ in range(3)]
  self.assertEqual(m.repeated_curve_check(runs[:2],self.reference())['status'],'FAIL')
  runs[2][1]['parameters']['cold_folios']=8
  self.assertEqual(m.repeated_curve_check(runs,self.reference())['status'],'FAIL')
 def test_p95_trend_is_checked_independently(self):
  runs=[self.rows() for _ in range(3)];ref=self.reference();ref['fig8']=ref['fig7']
  for rows in runs:
   for r,y in zip(rows,[1,3,2.8]):r['p95_slowdown_percent']=y
  self.assertEqual(m.repeated_curve_check(runs,ref)['status'],'FAIL')
if __name__=='__main__':unittest.main()
