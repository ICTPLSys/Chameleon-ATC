#!/usr/bin/env python3
"""Synthetic unit fixtures test aggregation/rendering, never paper measurements."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'ae/scripts'))
import ae_fig9
import plot_figures as plots


def fixture78():
    applications = {}
    for case in plots.ORDER:
        points = []
        for index, role in enumerate(('all_local', 'low', 'medium', 'high')):
            rows = [{'status': 'PASS', 'repeat': r + 1, 'reclamation_pct': index * 10 + (r - 1 if index else 0),
                     'slowdown_pct': index * 3 + r - 1, 'p95_slowdown_pct': index * 5 + r - 1} for r in range(3)]
            points.append({'name': role, 'reclamation_pct': index * 10, 'slowdown_pct': index * 3,
                           'p95_slowdown_pct': index * 5, 'configuration': {}, 'runs': rows})
        applications[case] = {'display_name': case, 'vm_memory_mib': 8192,
                              'all_local': {'mean_cost': 100}, 'points': points}
    return {'status': 'PASS', 'repeats': 3, 'applications': applications, 'test_fixture': True}


def fixture9():
    report = {'status': 'PASS', 'mixes': {}, 'reference': json.loads((plots.BASELINES / 'fig9.json').read_text())}
    for mix, cases in ae_fig9.data.MIXES.items():
        rows = []
        for repeat in range(1, 4):
            apps = [{'application': case, 'status': 'PASS', 'slowdown_percent': 2 * repeat + i}
                    for i, case in enumerate(cases)]
            rows.append({'repetition': repeat, **ae_fig9.data.aggregate_mix(mix, apps)})
        report['mixes'][mix] = {'status': 'PASS', 'repetitions': rows}
    return report


class FigureTests(unittest.TestCase):
    def test_three_repeat_mix_mean(self):
        result = ae_fig9.summarize(fixture9())
        self.assertEqual(result['mixes']['mix1']['repetition_values'], [3, 5, 7])
        self.assertEqual(result['mixes']['mix1']['slowdown_pct'], 5)

    def test_missing_repeat_rejected(self):
        report = fixture9(); report['mixes']['mix1']['repetitions'].pop()
        with self.assertRaises(ValueError): ae_fig9.summarize(report)

    def test_duplicate_repeat_rejected(self):
        report = fixture9(); report['mixes']['mix1']['repetitions'][2]['repetition'] = 1
        with self.assertRaises(ValueError): ae_fig9.summarize(report)

    def test_failed_app_rejected(self):
        report = fixture9(); report['mixes']['mix1']['repetitions'][0]['applications'][0]['status'] = 'FAIL'
        with self.assertRaises(ValueError): ae_fig9.summarize(report)

    def test_p95_same_runs(self):
        summary = fixture78()
        result = plots.prepare('fig8', summary, json.loads((plots.BASELINES / 'fig8.json').read_text()))
        self.assertEqual(result['applications']['memcached']['curves']['Chameleon'], [[0, 0], [10, 5], [20, 10], [30, 15]])
        self.assertEqual(result['applications']['memcached']['observations'], summary['applications']['memcached']['points'])

    def test_changed_mean_rejected(self):
        summary = fixture78(); summary['applications']['xsbench']['points'][3]['slowdown_pct'] = 8
        with self.assertRaises(ValueError):
            plots.prepare('fig7', summary, json.loads((plots.BASELINES / 'fig7.json').read_text()))

    def test_missing_chameleon_not_filled_from_reference(self):
        summary = fixture78(); summary['applications'].pop('xsbench')
        result = plots.prepare('fig7', summary, json.loads((plots.BASELINES / 'fig7.json').read_text()))
        self.assertNotIn('xsbench', result['applications'])
        self.assertNotIn('gcc', result['applications'])

    def test_new_denominator_and_high_match(self):
        configs = json.loads((ROOT / 'ae/config/fig78-points.json').read_text())
        baseline = {'status': 'PASS', 'repeats': 3, 'applications': {
            case: {'vm_memory_mib': app['vm_memory_mib'], 'all_local': {'mean_cost': 123.0}}
            for case, app in configs['applications'].items()}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'all-local.json'; path.write_text(json.dumps(baseline))
            high = ae_fig9.frozen_highs(ROOT / 'ae/config/fig9-highs.json', ROOT / 'ae/config/fig78-points.json', path)
        for case, app in configs['applications'].items():
            self.assertEqual(high['applications'][case]['all_local']['performance']['cost'], 123.0)
            self.assertEqual(high['applications'][case]['configuration']['sample_period'], app['points'][2]['configuration']['sample_period'])

    def test_render_all_figures(self):
        with tempfile.TemporaryDirectory() as directory:
            for fig in ('fig7', 'fig8', 'fig9'):
                summary = fixture78() if fig != 'fig9' else ae_fig9.summarize(fixture9())
                result = plots.prepare(fig, summary, json.loads((plots.BASELINES / (fig + '.json')).read_text()))
                plots.render(result, directory)
                svg = (Path(directory) / (fig + '.svg')).read_text()
                pdf = (Path(directory) / (fig + '.pdf')).read_bytes()
                self.assertIn('<svg', svg)
                self.assertIn('Slowdown', svg)
                self.assertTrue(pdf.startswith(b'%PDF'))
                self.assertFalse(result['error_bars'])


if __name__ == '__main__':
    unittest.main()
