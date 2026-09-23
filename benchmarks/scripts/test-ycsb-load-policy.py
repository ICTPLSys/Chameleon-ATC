#!/usr/bin/env python3
import importlib.util
import tempfile
import unittest
from pathlib import Path

spec=importlib.util.spec_from_file_location('check',Path(__file__).with_name('check-ycsb-result.py'))
check=importlib.util.module_from_spec(spec);spec.loader.exec_module(check)

class LoadPolicy(unittest.TestCase):
    def validate(self,phase,counts,completed=100,strict=False,totals=True):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'log'
            p.write_text((f'[OVERALL], RunTime(ms), 1000\n[OVERALL], Throughput(ops/sec), {completed}\n' if totals else '')+counts)
            return check.validate(p,phase,100,not strict)

    def test_insert_errors_keep_actual_partial_counts(self):
        r=self.validate('load','[INSERT], Return=OK, 59\n[INSERT], Return=ERROR, 1\n',60)
        self.assertEqual(r['status'],'PASS');self.assertEqual(r['insert_ok'],59)
        self.assertEqual(r['missing_insert_ok'],41);self.assertTrue(r['warnings']);self.assertEqual(r['errors'][0]['count'],1)

    def test_strict_mode_retains_original_failure(self):
        self.assertEqual(self.validate('load','[INSERT], Return=OK, 99\n[INSERT], Return=ERROR, 1\n',strict=True)['status'],'FAIL')

    def test_read_errors_still_fail(self):
        self.assertEqual(self.validate('run','[READ], Return=OK, 99\n[READ], Return=NOT_FOUND, 1\n')['status'],'FAIL')

    def test_missing_totals_or_empty_load_is_not_accepted(self):
        for counts,totals in [('[INSERT], Return=ERROR, 100\n',True),('[INSERT], Return=OK, 99\n[INSERT], Return=ERROR, 1\n',False)]:
            self.assertEqual(self.validate('load',counts,totals=totals)['status'],'FAIL')

    def test_complete_success_has_no_warning(self):
        r=self.validate('load','[INSERT], Return=OK, 100\n')
        self.assertEqual(r['status'],'PASS');self.assertEqual(r['warnings'],[])

    def test_retry_errors_with_all_records_successful_are_nonfatal(self):
        r=self.validate('load','[INSERT], Return=OK, 100\n[INSERT], Return=ERROR, 2\n')
        self.assertEqual(r['status'],'PASS');self.assertEqual(r['missing_insert_ok'],0)
        self.assertEqual(r['errors'][0]['count'],2)

    def test_unexplained_partial_work_still_fails(self):
        self.assertEqual(self.validate('load','[INSERT], Return=OK, 59\n',59)['status'],'FAIL')

if __name__=='__main__':unittest.main()
