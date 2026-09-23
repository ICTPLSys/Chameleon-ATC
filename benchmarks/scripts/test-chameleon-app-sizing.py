#!/usr/bin/env python3
import importlib.util
import tempfile
import json
from pathlib import Path
import unittest
s=importlib.util.spec_from_file_location('sized',Path(__file__).with_name('run-sized-chameleon-apps.py'))
m=importlib.util.module_from_spec(s);s.loader.exec_module(m)

class Sizing(unittest.TestCase):
    def test_tracking_profile_name_fits_leaf_limit(self):
        name='f78freq-spark-kmeans-b32768-s32768-c131072-h15000'
        self.assertTrue(m.valid_run_name(name,['spark-kmeans']))
        self.assertFalse(m.valid_run_name(name,['spark-kmeans'],True))
        self.assertFalse(m.valid_run_name('unsafe/name',['spark-kmeans']))
        self.assertFalse(m.valid_run_name('a'*70,['spark-kmeans']))

    def test_exact_generated_name_boundary_including_repeat(self):
        suffix=len('-spark-kmeans-local')
        self.assertTrue(m.valid_run_name('a'*(70-suffix),['spark-kmeans']))
        self.assertFalse(m.valid_run_name('a'*(71-suffix),['spark-kmeans']))
        self.assertFalse(m.valid_run_name('a'*(70-suffix),['spark-kmeans'],True))
        self.assertTrue(m.valid_run_name('a'*(67-suffix),['spark-kmeans'],True))

    def test_ten_percent_inclusive_byte_boundaries(self):
        self.assertTrue(m.within_target(18*2**30,20,10))
        self.assertTrue(m.within_target(22*2**30,20,10))
        self.assertFalse(m.within_target(18*2**30-1,20,10))
        self.assertFalse(m.within_target(22*2**30+1,20,10))

    def test_fixed_user_approved_sizes_are_used_without_reprofiling(self):
        config=json.loads((m.app.REPO/'benchmarks/config/chameleon-app-memory.json').read_text())
        expected={'liblinear':2166,'xsbench':7700,'graph500':4164,'graphchi':5144,'spark-kmeans':7796,'pvc':12608,'cassandra':11116,'memcached':2466}
        for case,memory in expected.items():
            self.assertEqual(m.fixed_plan(config,case)['memory_mib'],memory)

    def fixture(self,peak=100*2**20,mode='all-local'):
        row={'phase':'running','retired_bytes':0,'application_rss_bytes':peak,'application_hwm_bytes':peak,
             **{g:{'enabled':0} for g in ['tracker','manager','policy']}}
        return {'configuration':{'mode':mode},'cases':{'liblinear':{'status':'PASS','exit_code':0,'samples':[row]}}}
    def test_add_headroom_and_align_up(self):
        with tempfile.TemporaryDirectory() as d:
            p=m.sizing('liblinear',self.fixture(101*2**20+1),Path(d))
            self.assertEqual(p['memory_mib'],2150)
    def test_short_process_gnu_time_peak_exceeds_sampled_peak(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'application';path.mkdir()
            (path/'train-time.txt').write_text('Maximum resident set size (kbytes): 307200\n')
            self.assertEqual(m.sizing('liblinear',self.fixture(),Path(d))['memory_mib'],2348)
    def test_reclaimed_or_enabled_profile_is_rejected(self):
        for key in ['retired_bytes','policy']:
            report=self.fixture();row=report['cases']['liblinear']['samples'][0]
            if key=='policy':row[key]['enabled']=1
            else:row[key]=4096
            with self.assertRaises(ValueError):m.sizing('liblinear',report,Path('/unused'))
    def test_chameleon_profile_is_rejected(self):
        with self.assertRaises(ValueError):m.sizing('liblinear',self.fixture(mode='chameleon'),Path('/unused'))

if __name__=='__main__':unittest.main()
