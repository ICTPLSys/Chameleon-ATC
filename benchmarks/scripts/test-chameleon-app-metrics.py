#!/usr/bin/env python3
"""Independent numeric checks for application reclaim-rate reporting."""
import importlib.util
from pathlib import Path
import unittest
spec=importlib.util.spec_from_file_location('app_runner',Path(__file__).with_name('run-chameleon-apps.py'))
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

def row(t,b,phase='running',rss=0):
    return {'seconds':t,'retired_bytes':b,'phase':phase,'application_rss_bytes':rss,'compute_memory':{'Rss':1000-b}}

class Metrics(unittest.TestCase):
    def test_free_capacity_is_net_not_cumulative_and_rss_formula_is_unchanged(self):
        rows=[row(0,10),row(2,30)]
        rows[0]['policy']={'hard_reclaimed_bytes':100,'free_reclaimed_bytes':1000}
        rows[1]['policy']={'hard_reclaimed_bytes':200,'free_reclaimed_bytes':2000}
        result=m.summarize(rows,1000)
        self.assertEqual(result['mean_free_reclaimed_bytes'],150)
        self.assertEqual(result['peak_free_reclaimed_bytes'],200)
        self.assertAlmostEqual(result['mean_reclaim_percent'],2)

    def test_irregular_samples_use_elapsed_time(self):
        # Areas 10 and 80 byte-seconds over 10 seconds, not mean(0,10,10).
        result=m.summarize([row(0,0),row(2,10),row(10,10)],100)
        self.assertEqual(result['mean_retired_bytes'],9)
        self.assertEqual(result['mean_backing_reclaim_percent'],9)
        self.assertEqual(result['maximum_sampling_gap_seconds'],8)
    def test_cleanup_is_not_a_workload_sample(self):
        r=m.summarize([row(0,0,'baseline'),row(1,10),row(3,30),row(100,0,'restored')],200)
        self.assertEqual(r['mean_backing_reclaim_percent'],10)
        self.assertEqual(r['duration_seconds'],2)
        self.assertEqual(r['peak_backing_reclaim_percent'],15)
    def test_zero_reclaim_is_a_valid_measurement(self):
        r=m.summarize([row(0,0,rss=100),row(10,0,rss=500)],1000)
        self.assertEqual(r['mean_reclaim_percent'],0)
        self.assertEqual(r['peak_application_rss_bytes'],500)
    def test_insufficient_samples_are_not_zero_measurement(self):
        self.assertIsNone(m.summarize([],100)['mean_reclaim_percent'])
        self.assertIsNone(m.summarize([row(0,10)],100)['mean_backing_reclaim_percent'])

    def test_paper_uses_arithmetic_qemu_rss_not_elapsed_weight(self):
        rows=[row(0,0),row(1,100),row(10,400)]
        result=m.summarize(rows,1000)
        self.assertAlmostEqual(result['mean_reclaim_percent'],100/6)
        self.assertAlmostEqual(result['peak_reclaim_percent'],40)
        self.assertNotAlmostEqual(result['mean_reclaim_percent'],result['mean_backing_reclaim_percent'])
    def test_rss_overhead_is_not_clipped_and_capacity_is_per_vm(self):
        rows=[row(0,0),row(1,0)]
        self.assertAlmostEqual(m.summarize(rows,500)['mean_reclaim_percent'],-100)
        self.assertAlmostEqual(m.summarize(rows,2000)['mean_reclaim_percent'],50)

if __name__=='__main__': unittest.main()
