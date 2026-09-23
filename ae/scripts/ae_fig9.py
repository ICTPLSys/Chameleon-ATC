#!/usr/bin/env python3
"""Run three independent Guests for each Mix1--4, repeat three times, and plot."""
import argparse
import copy
import json
import math
from pathlib import Path
import statistics
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'benchmarks/scripts'))
import chameleon_fig9 as data


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + '\n')


def frozen_highs(config, fig78_config, all_local_summary=None):
    """Load portable high points and bind one explicit isolated denominator."""
    frozen = copy.deepcopy(json.loads(Path(config).read_text()))
    if frozen.get('schema') != 'chameleon-ae-frozen-high-v1':
        raise ValueError('Unsupported frozen high configuration')
    current = json.loads(Path(fig78_config).read_text()) if Path(fig78_config).exists() else None
    summary = json.loads(Path(all_local_summary).read_text()) if all_local_summary else None
    if summary is not None and (summary.get('status') != 'PASS' or summary.get('repeats') != 3):
        raise ValueError('All-local summary must contain three completed repetitions')
    for case, high in frozen['applications'].items():
        historical_vm_memory_mib = high['vm_memory_mib']
        if current is not None:
            app = current['applications'][case]
            point = next(p for p in app['points'] if p['name'] == 'high')
            high['configuration'] = copy.deepcopy(point['configuration'])
            high['configuration']['vm_memory_mib'] = app['vm_memory_mib']
            high['vm_memory_mib'] = app['vm_memory_mib']
            high['workload_configuration'] = copy.deepcopy(app['workload_configuration'])
            high['provenance']['configuration'] = 'ae/config/fig78-points.json#' + case + '/high'
        if summary is None:
            if historical_vm_memory_mib != high['vm_memory_mib']:
                raise ValueError('Run a new all-local baseline after changing VM capacity for ' + case)
            continue
        app = summary['applications'].get(case)
        if app is None:
            # A subset Fig78 run must not silently become a mixed protocol.
            raise ValueError('Fig78 all-local summary is missing ' + case)
        if app['vm_memory_mib'] != high['vm_memory_mib']:
            raise ValueError('All-local and high VM sizes differ for ' + case)
        local = app['all_local']
        cost = local['mean_cost']
        if isinstance(cost, bool) or not isinstance(cost, (int, float)) or not math.isfinite(cost) or cost <= 0:
            raise ValueError('Invalid all-local mean cost for ' + case)
        perf = {'cost': cost, 'runtime_seconds': local.get('mean_runtime_seconds'),
                'throughput_ops': local.get('mean_throughput_ops_sec'),
                'p95_us': local.get('mean_p95_us')}
        high['all_local'].update(performance=perf, report=str(Path(all_local_summary).resolve()) + '#' + case,
                                 normalization_protocol='isolated-tracked-all-local-three-run-mean-v1')
        if all(local.get(key) is not None for key in ('sample_period', 'cooling_samples', 'hhh_interval_ms')):
            high['all_local']['tracking_profile'] = {'sampling': local['sample_period'],
                                                     'cooling': local['cooling_samples'],
                                                     'hhh_interval_ms': local['hhh_interval_ms']}
        high['provenance']['all_local_report'] = high['all_local']['report']
    frozen['normalization'] = ('new-fig78-three-run-all-local-mean' if summary is not None
                               else 'bundled-historical-tracked-all-local')
    return frozen


def summarize(report, expected_repeats=3):
    """No missing/failed repetitions are silently discarded from the mean."""
    if report.get('status') != 'PASS':
        raise ValueError('Figure 9 did not complete successfully')
    result = {'schema_version': 1, 'figure': 'fig9', 'status': 'PASS',
              'repeats': expected_repeats, 'mixes': {},
              'aggregation': 'Mean of three per-application slowdowns per repetition; arithmetic mean over three repetitions',
              'baseline_kind': 'pre-measured', 'reference': report['reference']}
    for mix, group in report['mixes'].items():
        rows = group.get('repetitions', [])
        if group.get('status') != 'PASS' or len(rows) != expected_repeats:
            raise ValueError('Expected three completed repetitions for ' + mix)
        means = []
        seen = set()
        for row in rows:
            if row.get('status') != 'PASS':
                raise ValueError('Failed repetition for ' + mix)
            repetition = row['repetition']
            if repetition in seen:
                raise ValueError('Duplicate repetition for ' + mix)
            seen.add(repetition)
            computed = data.aggregate_mix(mix, row['applications'])['slowdown_percent']
            if not math.isclose(computed, row['slowdown_percent'], rel_tol=1e-9, abs_tol=1e-8):
                raise ValueError('Inconsistent three-application mean for ' + mix)
            means.append(computed)
        result['mixes'][mix] = {'status': 'PASS', 'slowdown_pct': statistics.mean(means),
                               'runs': copy.deepcopy(rows), 'repetition_values': means,
                               'application_names': data.MIXES[mix]}
    return result


def finish(out, repeats=3, baseline_level='75'):
    raw = json.loads((out / 'raw/report.json').read_text())
    summary = summarize(raw, repeats)
    summary['normalization'] = json.loads((out / 'config.json').read_text())['normalization']
    summary['baseline_level'] = baseline_level
    save(out / 'summary.json', summary)
    subprocess.run([sys.executable, str(ROOT / 'ae/scripts/plot_figures.py'), '--figure', 'fig9',
                    '--input', str(out / 'summary.json'), '--output-dir', str(out)], check=True)
    print('RESULT ' + str(out / 'summary.json'))


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--inventory', type=Path, default=ROOT / 'ae/config/host.json')
    p.add_argument('--config', type=Path, default=ROOT / 'ae/config/fig9-highs.json')
    p.add_argument('--fig78-config', type=Path, default=ROOT / 'ae/config/fig78-points.json')
    p.add_argument('--all-local-summary', '--all-local', dest='all_local_summary', type=Path)
    p.add_argument('--historical-all-local', action='store_true', help='Use bundled isolated measurements even if Fig78 results exist')
    p.add_argument('--output-dir', '--results', dest='output_dir', type=Path, default=ROOT / 'ae/results/fig9')
    p.add_argument('--mixes', choices=list(data.MIXES), nargs='+', default=list(data.MIXES))
    p.add_argument('--repeats', type=int, choices=[3], default=3)
    p.add_argument('--timeout', type=int, default=14400)
    p.add_argument('--baseline-level', choices=['50', '75'], default='75')
    mode = p.add_mutually_exclusive_group()
    mode.add_argument('--plan', action='store_true', help='Print the experiment without starting a VM')
    mode.add_argument('--run', action='store_true')
    mode.add_argument('--parse-only', action='store_true', help='Rebuild summary and figures from output-dir/raw/report.json')
    a = p.parse_args(argv)
    if a.all_local_summary and a.historical_all_local:
        p.error('Choose one all-local source')
    out = a.output_dir.resolve()
    if a.parse_only:
        finish(out, a.repeats, a.baseline_level)
        return
    default_summary = out.parent / 'fig78/all-local.json'
    baseline = a.all_local_summary or (default_summary if default_summary.exists() and not a.historical_all_local else None)
    if baseline and not baseline.exists() and a.run:
        raise ValueError('All-local results are missing: ' + str(baseline))
    high = frozen_highs(a.config, a.fig78_config, baseline if baseline and baseline.exists() else None)
    name = 'ae9-' + time.strftime('%Y%m%d-%H%M%S')
    command = [sys.executable, str(ROOT / 'benchmarks/scripts/run-chameleon-fig9.py'),
               '--run', '--name', name, '--inventory', str(a.inventory.resolve()),
               '--qualified', str(out / 'config.json'), '--mixes', *a.mixes,
               '--repeats', str(a.repeats), '--timeout', str(a.timeout),
               '--reference-plot', str(ROOT / 'ae/results_baselines/fig9.json'),
               '--output-dir', str(out / 'raw'), '--results-root', str(out / 'raw-apps'),
               '--performance-mode', '--no-plot']
    if not a.run and not a.parse_only:
        plan = {'figure': 'fig9', 'repeats': 3, 'command': command, 'output_dir': str(out),
                'normalization': 'new-fig78-three-run-all-local-mean' if baseline else high['normalization'],
                'all_local_summary': str(baseline) if baseline else None,
                'mixes': {mix: {'applications': data.MIXES[mix], 'simultaneous_vms': 3,
                                'configurations': {case: high['applications'][case]['configuration'] for case in data.MIXES[mix]}}
                          for mix in a.mixes}, 'figures': ['fig9.svg', 'fig9.pdf'],
                'reference_branch': a.baseline_level + '%'}
        print(json.dumps(plan, indent=2)); return
    if a.run:
        if out.exists() and any(out.iterdir()):
            raise ValueError('Choose an empty result directory; existing runs are never overwritten: ' + str(out))
        out.mkdir(parents=True, exist_ok=True)
        save(out / 'config.json', high)
        save(out / 'command.json', command)
        # Inherit the controlling terminal so sudo credentials remain usable.
        subprocess.run(command, cwd=ROOT, check=True)
    finish(out, a.repeats, a.baseline_level)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print('fig9: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
