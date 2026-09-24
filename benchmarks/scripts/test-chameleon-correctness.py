#!/usr/bin/env python3
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec=importlib.util.spec_from_file_location('summary',Path(__file__).with_name('summarize-chameleon-apps.py'))
summary=importlib.util.module_from_spec(spec);spec.loader.exec_module(summary)


class LiblinearAccuracy(unittest.TestCase):
    def check_accuracy(self, output, tolerance=None):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'application').mkdir()
            profile={'reference_accuracy_percent':95.1394,'prepare':{'records':68000000}}
            if tolerance is not None:profile['reference_accuracy_tolerance_pp']=tolerance
            (root/'report.json').write_text(json.dumps({'workload_configuration':profile}))
            (root/'application/predict.log').write_text(output)
            return summary.correctness('liblinear',root)

    def test_exact_legacy_rule_is_preserved(self):
        self.assertEqual(self.check_accuracy('Accuracy = 95.1393% (64694717/68000000)')['status'],'FAIL')

    def test_multicore_small_variation_with_consistent_counts(self):
        row=self.check_accuracy('Accuracy = 95.1393% (64694717/68000000)',0.001)
        self.assertEqual(row['status'],'PASS')
        self.assertEqual(row['prediction_counts'][0][1:],(64694717,68000000))

    def test_quality_regression_is_rejected(self):
        self.assertEqual(self.check_accuracy('Accuracy = 95.13% (64688400/68000000)',0.001)['status'],'FAIL')

    def test_incomplete_or_inconsistent_counts_are_rejected(self):
        for text in ['Accuracy = 95.1393%', 'Accuracy = 95.1393% (64694717/67999999)',
                     'Accuracy = 95.1393% (64600000/68000000)', '']:
            with self.subTest(text=text):
                self.assertEqual(self.check_accuracy(text,0.001)['status'],'FAIL')


class CassandraLoadAcceptance(unittest.TestCase):
    def test_tolerated_load_counts_remain_visible_but_reads_must_complete(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);app=root/'application'/'run';app.mkdir(parents=True)
            (app/'load.log').write_text('[INSERT], Return=OK, 99999\n[INSERT], Return=ERROR, 1\n')
            (app/'run.log').write_text('[READ], Return=OK, 1000000\n')
            self.assertEqual(summary.correctness('cassandra',root)['status'],'FAIL')
            (app/'load-validation.json').write_text(json.dumps({'status':'PASS','acceptance_policy':'load-insert-errors-nonfatal-v1','warnings':['INSERT error'],'insert_ok':99999}))
            r=summary.correctness('cassandra',root)
            self.assertEqual(r['status'],'PASS');self.assertEqual(r['insert_ok'],['99999'])
            (app/'run.log').write_text('[READ], Return=OK, 999999\n[READ], Return=NOT_FOUND, 1\n')
            self.assertEqual(summary.correctness('cassandra',root)['status'],'FAIL')

if __name__=='__main__':unittest.main()
