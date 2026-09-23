#!/usr/bin/env python3
"""Render AE Figures 7, 8 and 9 from measured three-run summaries, without error bars.

Reference colors, line/marker sizes and serif typography follow the original
paper figures. Baseline values come only from results_baselines;
Chameleon points come only from the selected measurement summary.
"""
import argparse
import copy
import json
import math
from pathlib import Path
import statistics

ROOT = Path(__file__).resolve().parents[2]
BASELINES = ROOT / 'ae/results_baselines'
ORDER = ['memcached', 'liblinear', 'pvc', 'graphchi', 'graph500', 'xsbench', 'spark-kmeans', 'cassandra']
COLORS = {'Chameleon': '#5B7FA6', 'HyperAlloc': '#B86A6A',
          'HyperAlloc+Memtis': '#6F9E72', 'Static': '#E3A64D'}
MARKERS = {'Chameleon': 'o', 'HyperAlloc': 's', 'HyperAlloc+Memtis': '^'}


def number(value):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise ValueError('Missing or nonfinite plotted value: ' + repr(value))
    return float(value)


def measured_mean(point, key):
    rows = point.get('runs', [])
    if len(rows) != 3 or any(row.get('status') != 'PASS' for row in rows):
        raise ValueError('Each plotted point requires three successful measurements')
    values = [number(row[key]) for row in rows]
    value = number(point[key])
    if point['name'] == 'all_local':
        if value != 0:
            raise ValueError('All-local origin must equal zero')
    elif not math.isclose(value, statistics.mean(values), rel_tol=1e-9, abs_tol=1e-8):
        raise ValueError('Summary differs from the three-run mean for ' + key)
    return value


def prepare(fig, summary, reference):
    if summary.get('status') != 'PASS' or summary.get('repeats') != 3:
        raise ValueError('Plot requires a completed three-repetition summary')
    result = {'schema_version': 1, 'figure': fig, 'repeats': 3,
              'error_bars': False, 'reference': copy.deepcopy(reference),
              'chameleon_source': 'reviewer measurements supplied in the input summary'}
    if fig in ('fig7', 'fig8'):
        result['metric'] = 'P95 latency slowdown (%)' if fig == 'fig8' else 'Runtime/throughput slowdown (%)'
        result['applications'] = {}
        metric = 'p95_slowdown_pct' if fig == 'fig8' else 'slowdown_pct'
        cases = ['memcached', 'cassandra'] if fig == 'fig8' else ORDER
        for case in cases:
            if case not in summary['applications']:
                continue
            app = summary['applications'][case]
            points = app['points']
            if [point['name'] for point in points] != ['all_local', 'low', 'medium', 'high']:
                raise ValueError('Expected all-local/low/medium/high points for ' + case)
            observed = [[measured_mean(point, 'reclamation_pct'), measured_mean(point, metric)] for point in points]
            if any(x < 0 or x > 100 for x, _ in observed):
                raise ValueError('Reclamation percentage outside [0,100]')
            result['applications'][case] = {
                'display_name': app['display_name'], 'vm_memory_mib': app['vm_memory_mib'],
                'curves': {'Chameleon': observed, **copy.deepcopy(reference['applications'][case]['curves'])},
                'point_configurations': [{k: copy.deepcopy(point.get(k)) for k in ('name', 'configuration')} for point in points],
                'observations': copy.deepcopy(points)}
        if not result['applications']:
            raise ValueError('No completed applications for ' + fig)
    else:
        level = str(summary.get('baseline_level', '75')).rstrip('%') + '%'
        if level not in ('50%', '75%'):
            raise ValueError('Unknown reference branch')
        result.update(metric='Mean application slowdown (%)', baseline_level=level, mixes={})
        for mix in ('mix1', 'mix2', 'mix3', 'mix4'):
            if mix not in summary['mixes']:
                continue
            group = summary['mixes'][mix]
            values = group['repetition_values']
            if group.get('status') != 'PASS' or len(values) != 3:
                raise ValueError('Three completed mix repetitions required')
            value = number(group['slowdown_pct'])
            if not math.isclose(value, statistics.mean(map(number, values)), rel_tol=1e-9, abs_tol=1e-8):
                raise ValueError('Mix mean differs from its three repetitions')
            result['mixes'][mix] = {'values': {'Chameleon': value, **{
                system: number(row['slowdown_percent']) for system, row in reference['mixes'][mix][level].items()}},
                'repetition_values': values, 'observations': copy.deepcopy(group['runs'])}
        if not result['mixes']:
            raise ValueError('No completed mixes')
    return result


def render(data, directory):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch
    from matplotlib.ticker import MaxNLocator
    style = {'font.family': 'serif', 'font.serif': ['DejaVu Serif'],
             'pdf.fonttype': 42, 'ps.fonttype': 42, 'svg.fonttype': 'none',
             'text.usetex': False, 'axes.labelsize': 11.7, 'xtick.labelsize': 10.4,
             'ytick.labelsize': 10.4, 'legend.fontsize': 11.05,
             'savefig.transparent': False, 'figure.facecolor': 'white'}
    with plt.rc_context(style):
        if data['figure'] in ('fig7', 'fig8'):
            paired = data['figure'] == 'fig8'
            fig, axes = plt.subplots(1 if paired else 3, 2 if paired else 3,
                                     figsize=(7.16, 2.6 if paired else 6.15), sharex=True, sharey=True,
                                     squeeze=False)
            systems = ['Chameleon', 'HyperAlloc', 'HyperAlloc+Memtis']
            values = [xy for app in data['applications'].values() for series in app['curves'].values() for xy in series]
            xmax = min(100, max(x for x, _ in values) + 5)
            ymin = min(0, math.floor(min(y for _, y in values) / 5) * 5)
            ymax = max(100 if paired else 80, math.ceil(max(y for _, y in values) / 20) * 20)
            for ax, app in zip(axes.flat, data['applications'].values()):
                for system in systems:
                    # Keep every measured point; sort only their drawing order.
                    series = sorted(app['curves'][system], key=lambda row: row[0])
                    ax.plot([v[0] for v in series], [v[1] for v in series],
                            color=COLORS[system], marker=MARKERS[system], markersize=4.2,
                            linewidth=1.4, label=system, zorder=3)
                ax.set_title(app['display_name'], fontsize=13, fontweight='bold', pad=2)
                ax.set_xlim(0, xmax); ax.set_ylim(ymin, ymax)
                ax.xaxis.set_major_locator(MaxNLocator(nbins=4, integer=True))
                ax.yaxis.set_major_locator(MaxNLocator(nbins=4))
                ax.tick_params(axis='both', pad=1)
                ax.grid(axis='y', linestyle='--', linewidth=.4, alpha=.45)
                ax.set_axisbelow(True)
            for ax in list(axes.flat)[len(data['applications']):]:
                ax.set_visible(False)
            fig.supxlabel('Reclamation Ratio (%)', fontsize=11.7, y=.015 if paired else .025)
            fig.supylabel('P95 Slowdown (%)' if paired else 'Slowdown (%)', fontsize=11.7, x=.012)
            handles = [Line2D([], [], color=COLORS[s], marker=MARKERS[s], linewidth=1.4, markersize=4.2, label=s) for s in systems]
            fig.legend(handles=handles, ncol=3, loc='upper center', bbox_to_anchor=(.5, 1),
                       frameon=False, columnspacing=.7, handlelength=1.4, handletextpad=.35)
            fig.subplots_adjust(left=.10, right=.995, top=.78 if paired else .91,
                                bottom=.26 if paired else .115, wspace=.18, hspace=.40)
        else:
            fig, axes = plt.subplots(2, 2, figsize=(7.16, 4.15), squeeze=False)
            systems = ['Chameleon', 'HyperAlloc', 'HyperAlloc+Memtis', 'Static']
            for i, (ax, (mix, group)) in enumerate(zip(axes.flat, data['mixes'].items())):
                values = [group['values'][s] for s in systems]
                low, high = min(0, min(values)), max(10, max(values))
                span = max(high - low, 1)
                for j, (system, value) in enumerate(zip(systems, values)):
                    ax.bar(j, value, width=.64, color=COLORS[system], edgecolor='black', linewidth=.45)
                    ax.annotate(f'{value:.1f}', (j, value), xytext=(0, 3 if value >= 0 else -3),
                                textcoords='offset points', ha='center', va='bottom' if value >= 0 else 'top', fontsize=9)
                ax.set_ylim(low - (.18 * span if low < 0 else 0), high + .27 * span)
                ax.set_xticks([])
                ax.yaxis.set_major_locator(MaxNLocator(nbins=4))
                ax.yaxis.grid(True, linestyle='--', linewidth=.35, alpha=.35)
                ax.set_axisbelow(True)
                ax.text(.5, -.11, f'({chr(97+i)}) Mix {mix[3:]}', transform=ax.transAxes,
                        ha='center', va='top', fontsize=11.7)
            for ax in list(axes.flat)[len(data['mixes']):]:
                ax.set_visible(False)
            fig.supylabel('Slowdown (%)', fontsize=11.7, x=.012)
            handles = [Patch(facecolor=COLORS[s], edgecolor='black', linewidth=.45, label=s) for s in systems]
            fig.legend(handles=handles, ncol=4, loc='upper center', bbox_to_anchor=(.5, .99),
                       frameon=False, columnspacing=.55, handlelength=1.1, handletextpad=.28)
            fig.subplots_adjust(left=.095, right=.99, top=.82, bottom=.105, wspace=.24, hspace=.43)
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        for extension in ('svg', 'pdf'):
            fig.savefig(directory / f"{data['figure']}.{extension}")
        plt.close(fig)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--figure', choices=['fig78', 'fig7', 'fig8', 'fig9'], required=True)
    p.add_argument('--input', '--summary', dest='input', type=Path, required=True)
    p.add_argument('--output-dir', '--results', dest='output_dir', type=Path, required=True)
    p.add_argument('--baselines', type=Path, default=BASELINES)
    p.add_argument('--plan', action='store_true')
    a = p.parse_args(argv)
    figures = ['fig7', 'fig8'] if a.figure == 'fig78' else [a.figure]
    if a.plan:
        print(json.dumps({'figures': figures, 'input': str(a.input), 'output_dir': str(a.output_dir),
                          'outputs': [f'{fig}.{ext}' for fig in figures for ext in ('svg', 'pdf', 'plot-data.json')],
                          'error_bars': False, 'repeats': 3}, indent=2)); return
    summary = json.loads(a.input.read_text())
    for fig in figures:
        if fig == 'fig8' and not any(case in summary.get('applications', {}) for case in ('memcached', 'cassandra')):
            continue
        data = prepare(fig, summary, json.loads((a.baselines / f'{fig}.json').read_text()))
        data['source_summary'] = str(a.input.resolve())
        render(data, a.output_dir)
        (a.output_dir / f'{fig}.plot-data.json').write_text(json.dumps(data, indent=2, allow_nan=False) + '\n')
        print('PLOT ' + str(a.output_dir / f'{fig}.pdf'))


if __name__ == '__main__':
    main()
