#!/usr/bin/env python3
"""Export observed skew-test counters as CSV and a standalone memory timeline."""
import argparse
import csv
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path)
    args = parser.parse_args()
    report = json.loads(args.report.read_text())
    samples = report['samples']
    output = args.report.parent
    gib = 1 << 30
    columns = ['seconds', 'phase', 'compute_rss_gib', 'remote_gib', 'bytes_read_gib',
               'hardware_samples', 'pebs_reload_repairs', 'commit_rollbacks', 'transport_errors']
    rows = [[s['seconds'], s['phase'], s['compute_memory']['Rss'] / gib,
             s['shadow']['host_reclaimed_pages'] * 4096 / gib, s['hermit']['bytes_read'] / gib,
             s['tracker']['hardware_samples'], s['tracker'].get('pebs_reload_repairs', 0),
             s['shadow'].get('commit_rollback_success', 0), s.get('rdma', {}).get('transfer_errors', 0)]
            for s in samples]
    with (output / 'samples.csv').open('w') as stream:
        writer = csv.writer(stream)
        writer.writerow(columns)
        writer.writerows(rows)
    start = report.get('phases', {}).get('baseline', {}).get('seconds', 0)
    selected = [r for r in rows if r[0] >= start]
    if not selected:
        raise SystemExit('No samples after baseline')
    seconds = [r[0] - start for r in selected]
    fig, axes = plt.subplots(3, 1, sharex=True, figsize=(9, 6.8), layout='constrained')
    axes[0].plot(seconds, [r[2] for r in selected], color='#2367a0', linewidth=1.8)
    axes[0].set_ylabel('Compute RSS (GiB)')
    axes[1].plot(seconds, [r[3] for r in selected], color='#c47727', linewidth=1.8)
    target = report['configuration']['reclaim_mib'] / 1024
    axes[1].axhline(target, linestyle='--', color='0.5', linewidth=1, label=f'Target: {target:g} GiB')
    axes[1].set_ylabel('Remote resident (GiB)')
    axes[1].set_ylim(bottom=0)
    axes[1].legend(loc='lower right', frameon=False)
    axes[2].plot(seconds, [r[5] / 1000 for r in selected], color='#39794e', linewidth=1.8)
    axes[2].set_ylabel('PEBS records (thousands)')
    axes[2].set_xlabel('Seconds since baseline')
    labels = [('shifted_hotspot', 'Hotspot shift')]
    for phase, label in labels:
        match = next((r for r in selected if r[1] == phase), None)
        if match:
            at = match[0] - start
            for ax in axes:
                ax.axvline(at, color='0.65', linewidth=0.8, linestyle=':')
            axes[0].annotate(label, (at, 1), xycoords=('data', 'axes fraction'),
                             xytext=(3, -5), textcoords='offset points', rotation=90,
                             va='top', fontsize=8)
    # The synchronous disable has no intermediate samples; mark the entire
    # restore/verify interval instead of inventing an instantaneous transition.
    phases = report.get('phases', {})
    if 'shifted' in phases and 'restored' in phases:
        begin, end = (phases[name]['seconds'] - start for name in ('shifted', 'restored'))
        for ax in axes:
            ax.axvspan(begin, end, color='0.5', alpha=0.08)
        axes[0].annotate('Restore / verify', (begin, 1), xycoords=('data', 'axes fraction'),
                         xytext=(3, -5), textcoords='offset points', rotation=90,
                         va='top', fontsize=8)
    for ax in axes:
        ax.spines[['top', 'right']].set_visible(False)
        ax.grid(axis='y', color='0.9', linewidth=0.7)
    cfg = report['configuration']
    fig.suptitle(f"{cfg['gib']} GiB / {cfg['threads']} workers; hot-region choice "
                 f"{cfg['hot_access_ppm'] / 10000:g}% — {report['status']}", fontsize=12)
    fig.savefig(output / 'memory.svg')
    fig.savefig(output / 'memory.png', dpi=150)
    plt.close(fig)
    print(output / 'memory.svg')


if __name__ == '__main__':
    main()
