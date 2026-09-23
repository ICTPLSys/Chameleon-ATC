#!/usr/bin/env python3
"""Parser checks for incomplete, duplicated and cross-boot mechanism evidence."""
from copy import deepcopy
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('physical_micro_analysis',
        Path(__file__).resolve().parents[1] / 'scripts/analyze-physical-micro.py')
analysis = importlib.util.module_from_spec(spec)
spec.loader.exec_module(analysis)


def snapshot(batches=0, pages=0, seconds=0):
    result = {'seconds': seconds, 'qmp': {'chameleon': {
        'begin-batches': batches, 'begin-flushes': batches, 'discarded-pages': pages,
        'installed-pages': pages, 'retired-pages': 0, 'session': 9, 'ept-mode': 'deferred'}},
        'manager': {'split_ok': 0}, 'shadow': {'prepare_pmd_demotions': 0, 'load_demand_attempts': 0,
                    'load_background_attempts': 0},
        'policy': {'high_epochs': 0, 'low_epochs': 0, 'shadow_restored_pages': 0},
        'tracker': {'hardware_samples': 0, 'synthetic_samples': 0}}
    for group, keys in analysis.COUNTERS.items():
        for key in keys:
            result.setdefault(group, {}).setdefault(key, 0)
    return result


def transaction(tx=11, sizes=(512, 256), token=100, batch=None):
    batch = tx if batch is None else batch
    lines = [f'chameleon begin-complete batch={batch} transaction={tx} ranges={len(sizes)}']
    for i, pages in enumerate(sizes):
        lines.append(f'chameleon discard transaction={tx} token={token+i} gpa=0x{(token+i)*2097152:x} pages={pages} status=0')
    for i, pages in enumerate(sizes):
        lines.append(f'chameleon retired token={token+i} pages={pages} mincore_resident=0 rc=0')
    for i, pages in enumerate(sizes):
        lines.append(f'chameleon installed token={token+i} gpa=0x{(token+i)*2097152:x} pages={pages}')
    return '\n'.join(lines) + '\n'


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.report = {'status': 'PASS', 'phases': {
            'baseline': snapshot(10, 200, 0), 'restored': snapshot(11, 968, 5)}}
        self.log = transaction()

    def run_analysis(self, **kwargs):
        return analysis.analyze(kwargs.get('report', self.report), kwargs.get('log', self.log))

    def test_complete_mixed_order_batch(self):
        result = self.run_analysis()
        self.assertEqual(result['status'], 'PASS', result['errors'])
        self.assertEqual(result['ept']['multi_range_begin_count'], 1)
        self.assertEqual(result['ept']['ranges_per_flush'], 2)
        self.assertEqual(result['retirement']['order_histogram'], {'8': 1, '9': 1})
        self.assertTrue(result['observed']['small_folio_retirement'])
        self.assertFalse(result['observed']['physical_folio_split'])

    def test_workload_capacity_failure_does_not_change_evidence_verdict(self):
        self.report['status'] = 'FAIL'
        result = self.run_analysis()
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(result['report_status'], 'FAIL')

    def test_missing_begin_is_failure(self):
        result = self.run_analysis(log='\n'.join(self.log.splitlines()[1:]))
        self.assertEqual(result['status'], 'FAIL')
        self.assertFalse(result['checks']['complete_begin_interval'])

    def test_duplicate_begin_is_not_counted_twice(self):
        result = self.run_analysis(log=self.log.splitlines()[0] + '\n' + self.log)
        self.assertEqual(result['status'], 'FAIL')
        self.assertFalse(result['checks']['latest_epoch_order'])

    def test_duplicate_discard_is_failure(self):
        result = self.run_analysis(log=self.log + self.log.splitlines()[1] + '\n')
        self.assertFalse(result['checks']['unique_discards'])
        self.assertEqual(result['status'], 'FAIL')

    def test_missing_retired_and_install_each_fail(self):
        for kind, check in [('retired', 'retired_evidence'), ('installed', 'installed_evidence')]:
            with self.subTest(kind=kind):
                log = '\n'.join(line for line in self.log.splitlines() if 'chameleon ' + kind not in line)
                result = self.run_analysis(log=log)
                self.assertFalse(result['checks'][check])

    def test_resident_page_does_not_count_as_retired(self):
        result = self.run_analysis(log=self.log.replace('mincore_resident=0', 'mincore_resident=1', 1))
        self.assertFalse(result['checks']['retired_evidence'])
        self.assertFalse(result['observed']['retirement_and_restore'])

    def test_failed_discard_is_not_successful_capacity(self):
        result = self.run_analysis(log=self.log.replace('status=0', 'status=-5', 1))
        self.assertFalse(result['checks']['successful_discards'])
        self.assertEqual(result['retirement']['successful_pages'], 256)

    def test_page_and_flush_counter_mismatch(self):
        for key in ('discarded-pages', 'installed-pages', 'begin-flushes'):
            with self.subTest(key=key):
                report = deepcopy(self.report)
                report['phases']['restored']['qmp']['chameleon'][key] += 1
                self.assertEqual(self.run_analysis(report=report)['status'], 'FAIL')

    def test_full_log_uses_latest_epoch_without_old_id_matches(self):
        old = ''.join(transaction(tx, (512,), token=tx) for tx in range(1, 12))
        latest = ''.join(transaction(tx, (512,), token=1000+tx) for tx in range(1, 11)) + self.log
        result = self.run_analysis(log=old + 'Booting Linux\n' + latest)
        self.assertEqual(result['status'], 'PASS', result['errors'])
        self.assertEqual(result['log_epoch']['discarded_older_epochs'], 1)
        self.assertEqual(result['ept']['range_count'], 2)

    def test_incomplete_latest_epoch_cannot_borrow_old_matches(self):
        old = ''.join(transaction(tx, (512,), token=tx) for tx in range(1, 11)) + self.log
        result = self.run_analysis(log=old + transaction(1, (512,), token=999))
        self.assertEqual(result['status'], 'FAIL')
        self.assertFalse(result['checks']['complete_begin_interval'])

    def test_immediate_mode_uses_separate_host_transactions(self):
        report = deepcopy(self.report)
        for phase in report['phases'].values():
            phase['qmp']['chameleon']['ept-mode'] = 'immediate'
        report['phases']['restored']['qmp']['chameleon'].update({'begin-batches': 12, 'begin-flushes': 12})
        result = self.run_analysis(report=report, log=transaction(11, (512,), 100, 11) + transaction(12, (256,), 101, 11))
        self.assertEqual(result['status'], 'PASS', result['errors'])
        self.assertEqual(result['ept']['maximum_ranges'], 1)
        self.assertEqual(result['ept']['begin_count'], 2)

    def test_disable_background_load_is_separate_from_psi_phase(self):
        retired = snapshot(11, 968, 2)
        shifted = snapshot(11, 968, 3)
        shifted['policy']['high_epochs'] = 4
        shifted['shadow']['load_background_attempts'] = 2
        restored = self.report['phases']['restored']
        restored['policy']['high_epochs'] = 4
        restored['shadow']['load_background_attempts'] = 10
        self.report['phases'].update({'retired': retired, 'shifted': shifted})
        result = self.run_analysis()
        self.assertEqual(result['phase_intervals']['retired->shifted']['counter_delta']['shadow']['load_background_attempts'], 2)
        self.assertEqual(result['phase_intervals']['shifted->restored']['counter_delta']['shadow']['load_background_attempts'], 8)

    def test_fixed_batch_interval_excludes_tail_drain(self):
        self.report['phases']['baseline']['qmp']['chameleon']['batch-pages'] = 4096
        active = snapshot(11, 968, 2)
        active['qmp']['chameleon']['batch-pages'] = 4096
        self.report['phases']['active_reclaim_end'] = active
        self.report['phases']['restored']['qmp']['chameleon'].update({
            'begin-batches': 12, 'begin-flushes': 12, 'discarded-pages': 972,
            'installed-pages': 972, 'batch-pages': 1})
        result = self.run_analysis(log=self.log + transaction(12, (4,), token=200))
        self.assertEqual(result['status'], 'PASS', result['errors'])
        active_ept = result['phase_intervals']['baseline->active_reclaim_end']['ept']
        tail_ept = result['phase_intervals']['active_reclaim_end->restored']['ept']
        self.assertEqual(active_ept['range_count'], 2)
        self.assertEqual(active_ept['maximum_ranges'], 2)
        self.assertEqual(tail_ept['range_count'], 1)
        self.assertEqual(tail_ept['discarded_order_histogram'], {'2': 1})

    def test_counter_reset_is_failure(self):
        self.report['phases']['baseline']['manager']['split_ok'] = 10
        result = self.run_analysis()
        self.assertFalse(result['checks']['counters_monotonic'])

    def test_intermediate_counter_reset_not_hidden_by_later_progress(self):
        self.report['phases']['retired'] = snapshot(11, 968, 2)
        self.report['phases']['retired']['manager']['split_ok'] = 8
        self.report['phases']['shifted'] = snapshot(11, 968, 3)
        self.report['phases']['restored']['manager']['split_ok'] = 9
        result = self.run_analysis()
        self.assertTrue(result['checks']['counters_monotonic'])
        self.assertFalse(result['checks']['phase_counters_monotonic'])

    def test_session_change_is_failure(self):
        self.report['phases']['restored']['qmp']['chameleon']['session'] += 1
        self.assertFalse(self.run_analysis()['checks']['same_session'])

    def test_missing_endpoint_is_failure_not_exception(self):
        self.report['phases'].pop('restored')
        result = self.run_analysis()
        self.assertEqual(result['status'], 'FAIL')
        self.assertFalse(result['checks']['endpoint_snapshots'])

    def test_missing_error_counter_cannot_be_silently_treated_as_zero(self):
        self.report['phases']['restored']['shadow'].pop('data_load_failure')
        result = self.run_analysis()
        self.assertEqual(result['status'], 'FAIL')
        self.assertFalse(result['checks']['counter_schema_complete'])
        self.assertEqual(result['missing_counters']['restored.shadow'], ['data_load_failure'])

    def test_matching_install_from_next_run_cannot_repair_incomplete_run(self):
        missing = '\n'.join(line for line in self.log.splitlines() if 'installed token=100 ' not in line)
        later = transaction(12, (512,), token=999) + '\n' + self.log.splitlines()[-2]
        result = self.run_analysis(log=missing + '\n' + later)
        self.assertFalse(result['checks']['installed_evidence'])


if __name__ == '__main__':
    unittest.main()
