#!/usr/bin/env python3
import copy
import importlib.util
from pathlib import Path
import unittest

spec=importlib.util.spec_from_file_location('plot_tuning',Path(__file__).with_name('plot-chameleon-tuning.py'))
plot=importlib.util.module_from_spec(spec);spec.loader.exec_module(plot)

class CurveExportTests(unittest.TestCase):
    def entry(self):
        trials={'b':{'status':'PASS','mode':'all-local','reclaim_percent':-.18,
                     'checks':{'all_local_tracking_active':True},'report':'baseline.json'}}
        for i in range(1,4):
            trials[str(i)]={'reclaim_percent':i*10,'slowdown_percent':i,'trial_id':str(i)}
        return {'trials':trials},{'baseline':'b','trial_ids':['3','1','2']}

    def test_zero_origin_preserves_raw_baseline_and_adds_fourth_point(self):
        entry,repeat=self.entry();before=copy.deepcopy(entry)
        points=plot.curve_points(entry,repeat,True)
        self.assertEqual(len(points),4)
        self.assertEqual(points[0]['reclaim_percent'],0)
        self.assertEqual(points[0]['slowdown_percent'],0)
        self.assertEqual(points[0]['raw_reclaim_percent'],-.18)
        self.assertEqual(points[0]['report'],'baseline.json')
        self.assertEqual(entry,before)

    def test_origin_requires_a_successful_tracked_baseline(self):
        entry,repeat=self.entry();entry['trials']['b']['checks']['all_local_tracking_active']=False
        with self.assertRaises(ValueError):plot.curve_points(entry,repeat,True)

    def test_legacy_export_does_not_silently_add_origin(self):
        entry,repeat=self.entry()
        self.assertEqual(len(plot.curve_points(entry,repeat)),3)

if __name__=='__main__':unittest.main()
