#!/usr/bin/env python3
"""Plot observed mechanism counters; lines connect samples, not exact transitions."""
import argparse
import json
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report',type=Path)
    args=parser.parse_args()
    report=json.loads(args.report.read_text())
    baseline=report['phases']['baseline']
    points=[s for s in report['samples'] if s['seconds']>=baseline['seconds']]
    seconds=[s['seconds']-baseline['seconds'] for s in points]
    fig,axes=plt.subplots(4,1,sharex=True,figsize=(10,9),layout='constrained')
    axes[0].plot(seconds,[s['shadow']['host_reclaimed_pages']/256 for s in points],color='#2367a0')
    axes[0].axhline(report['configuration']['reclaim_mib'],ls='--',color='0.5',label='Configured target')
    axes[0].set_ylabel('Retired backing (MiB)')
    axes[0].legend(frameon=False,loc='upper right')
    axes[1].plot(seconds,[s['manager']['split_ok']-baseline['manager']['split_ok'] for s in points],color='#39794e')
    axes[1].set_ylabel('Physical folio splits')
    for key,label,color in [('load_demand_attempts','Demand LOAD','#2367a0'),
                            ('load_background_attempts','Background LOAD','#c47727')]:
        axes[2].plot(seconds,[s['shadow'][key]-baseline['shadow'][key] for s in points],label=label,color=color)
    axes[2].set_ylabel('Cumulative LOAD attempts')
    axes[2].legend(frameon=False,loc='upper left')
    for key,label,color in [('high_epochs','High-pressure decisions','#c47727'),
                            ('low_epochs','Low-pressure decisions','#39794e')]:
        axes[3].plot(seconds,[s['policy'][key]-baseline['policy'][key] for s in points],label=label,color=color)
    axes[3].set_ylabel('Cumulative policy decisions')
    axes[3].legend(frameon=False,loc='upper left')
    labels={'skew_reclaim':'Reclaim enabled',
            'shifted_hotspot':'Uniform access' if report['configuration']['scenario']=='psi' else 'Hotspot shifted',
            'cooled_hotspot':'Hot region only','returned_hotspot':'Hotspot returned',
            'policy_restore':'Explicit restore'}
    for phase,label in labels.items():
        match=next((p for p in points if p['phase']==phase),None)
        if match is None: continue
        x=match['seconds']-baseline['seconds']
        for ax in axes: ax.axvline(x,ls=':',color='0.65',lw=0.8)
        axes[0].annotate(label,(x,1),xycoords=('data','axes fraction'),xytext=(3,-5),
                         textcoords='offset points',rotation=90,va='top',fontsize=8)
    # Restore is synchronous; its start is the final active checkpoint.
    last=next((report['phases'][p] for p in ('cooled','returned','shifted') if p in report['phases']),None)
    restored=report['phases'].get('restored')
    if last and restored:
        for ax in axes:
            ax.axvspan(last['seconds']-baseline['seconds'],restored['seconds']-baseline['seconds'],color='0.5',alpha=.1)
    for ax in axes:
        ax.spines[['top','right']].set_visible(False)
        ax.grid(axis='y',color='0.9',lw=.7)
        ax.set_ylim(bottom=0)
    axes[-1].set_xlabel('Seconds since baseline; shaded interval = explicit restore / verification')
    c=report['configuration']
    fig.suptitle(f"{c['scenario']} / {c['ept_mode']}: {c['gib']} GiB, {c['threads']} workers — {report['status']}")
    for extension in ('svg','png'):
        fig.savefig(args.report.parent/('mechanisms.'+extension),dpi=150)
    plt.close(fig)


if __name__=='__main__':
    main()
