#!/usr/bin/env python3
"""Render measured search points and validated repeats, separate from target curves."""
import argparse,csv,json,math
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.ticker import MaxNLocator
ROOT=Path(__file__).resolve().parents[2]
NAMES={'memcached':'Memcached','liblinear':'Liblinear','pvc':'Metis','graphchi':'GraphChi PR','graph500':'Graph500','spec602-gcc':'602.gcc\_s','xsbench':'XSBench','spark-kmeans':'Spark-KMeans','cassandra':'Cassandra'}
def curve_points(entry, repeat, include_all_local=False):
 points=sorted([entry['trials'][tag] for tag in repeat['trial_ids']],key=lambda r:r['reclaim_percent'])
 if include_all_local:
  baseline=entry['trials'][repeat['baseline']]
  if baseline.get('status')!='PASS' or not baseline.get('checks',{}).get('all_local_tracking_active'):
   raise ValueError('All-local origin requires a completed PEBS+HHH baseline')
  anchor={**baseline,'point_kind':'all-local-reference','raw_reclaim_percent':baseline['reclaim_percent'],
          'reclaim_percent':0.0,'slowdown_percent':0.0,'p95_slowdown_percent':0.0,
          'origin_definition':'Relative all-local reference; raw RSS measurement retained separately'}
  points=[anchor]+points
 return points

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--directory',type=Path,default=ROOT/'benchmarks/results/chameleon/tuning-fig78-tracked-v4');a=p.parse_args()
 data=json.loads((a.directory/'search.json').read_text());reference=json.loads((ROOT/'benchmarks/config/chameleon-tuning-reference.json').read_text())
 plt.rcParams.update({'text.usetex':True,'text.latex.preamble':r'\usepackage[T1]{fontenc}\usepackage{libertine}\usepackage{zi4}',
  'font.family':'sans-serif','font.size':10,'axes.labelsize':10,'xtick.labelsize':8,'ytick.labelsize':8,
  'savefig.bbox':None,'savefig.transparent':True,'figure.facecolor':'none','axes.facecolor':'none',
  'axes.linewidth':0.5,'axes.spines.top':False,'axes.spines.right':False,'legend.fontsize':8,'legend.frameon':False,'lines.linewidth':0.8})
 # Both plots read exactly these trial records. No separate pair-specific tuning.
 rows=[];accepted={}
 for case,entry in data['cases'].items():
  for tag,r in entry['trials'].items():
   row={'case':case,'trial_id':tag,'status':r['status'],'reference_status':r.get('reference_status'),'memory_mib':r.get('memory_mib',entry['memory_mib']),**(r.get('parameters_requested') or {})}
   for k in ['reclaim_percent','cold_mean_percent','slowdown_percent','p95_slowdown_percent','p99_slowdown_percent','free_mean_mib','cold_peak_mib','tracking_only_overhead_percent','reclaim_over_matched_tracking_percent','tracking_only_p95_overhead_percent','reclaim_over_matched_tracking_p95_percent']:row[k]=r.get(k)
   for key,source in [('sample_period','sampling'),('cooling_samples','cooling'),('hhh_interval_ms','hhh_interval_ms')]:
    row[key]=r.get('tracking_profile',{}).get(source,r.get('tracking_requested',{}).get(key,row.get(key)))
   for key in ['write_bytes','read_bytes']:row['rdma_'+key]=r.get('counters',{}).get('rdma',{}).get(key)
   row['report']=r.get('report')
   for key in ['baseline_report','matched_tracking_baseline_report','matched_tracking_baseline_id','matched_tracking_baseline_reused','matched_tracking_baseline_scope']:row[key]=r.get(key)
   rows.append(row)
  if entry.get('status')=='ACCEPTED':accepted[case]=entry['accepted']
 fields=['case','trial_id','status','reference_status','memory_mib','psi_ppm','epoch_us','cold_folios','minimum_local_mib','sample_period','cooling_samples','hhh_interval_ms','reclaim_percent','cold_mean_percent','slowdown_percent','p95_slowdown_percent','p99_slowdown_percent','free_mean_mib','cold_peak_mib','tracking_only_overhead_percent','reclaim_over_matched_tracking_percent','tracking_only_p95_overhead_percent','reclaim_over_matched_tracking_p95_percent','rdma_write_bytes','rdma_read_bytes','report']
 fields+=['baseline_report','matched_tracking_baseline_report','matched_tracking_baseline_id','matched_tracking_baseline_reused','matched_tracking_baseline_scope']
 with (a.directory/'all-trials.csv').open('w') as f:
  w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)
 curves={case:[{'repeat':rep['repeat'],'points':curve_points(data['cases'][case],rep,
             value['validation'].get('includes_all_local_origin',False))} for rep in value['validation']['rounds']]
         for case,value in accepted.items()}
 (a.directory/'accepted-parameters.json').write_text(json.dumps({'status':data['status'],'reference_kind':'estimated numeric targets supplied by user','applications':accepted},indent=2)+'\n')
 (a.directory/'accepted-curves.json').write_text(json.dumps({'applications':curves,'all_local_origin':'Reference normalization only; raw_reclaim_percent preserves measured RSS'},indent=2)+'\n')
 for figure,metric,cases,shape,size in [('fig7','slowdown_percent',list(NAMES),(3,3),(7,4.2)),('fig8','p95_slowdown_percent',['memcached','cassandra'],(1,2),(7,2.3333333333))]:
  fig,axes=plt.subplots(*shape,figsize=size,squeeze=False)
  for ax,case in zip(axes.flat,cases):
   ax.text(0.03,0.97,NAMES[case],ha='left',va='top',transform=ax.transAxes,fontsize=9)
   entry=data['cases'].get(case,{})
   for (name,points),color,marker in zip(reference[figure].get(case,{}).items(),['#B86A6A','#6F9E72'],['s','^']):
    ax.plot([v[0] for v in points],[v[1] for v in points],linestyle='--',color=color,marker=marker,markersize=2,label=name+' target')
   trials=[r for r in rows if r['case']==case and r['trial_id'].startswith('d') and r['status']=='PASS' and r.get(metric) is not None]
   ax.scatter([r['reclaim_percent'] for r in trials],[r[metric] for r in trials],marker='x',color='#555555',s=15,label='Search runs')
   if case in accepted:
    for i,rep in enumerate(accepted[case]['validation']['rounds']):
     points=curves[case][i]['points'];count=len(accepted[case]['validation']['rounds'])
     ax.plot([r['reclaim_percent'] for r in points],[r[metric] for r in points],marker='o',markersize=3,color='#0072B2',alpha=max(.3,1-.3*i),label=f'Chameleon ({count} repeats)' if i==0 else None)
   else:
    note='Unavailable' if case=='spec602-gcc' else ('Not run' if not entry else ('No accepted curve' if entry.get('status')!='RUNNING' else 'Search in progress'))
    ax.text(.97,.04,note,ha='right',va='bottom',transform=ax.transAxes,fontsize=8,color='#555555')
   values=[r[metric] for r in rows if r['case']==case and r['status']=='PASS' and r.get(metric) is not None]
   ax.set_ylim(min([0]+values)-2,max([80 if figure=='fig7' else 100]+values)*1.08)
   ax.xaxis.set_major_locator(MaxNLocator(nbins=4));ax.yaxis.set_major_locator(MaxNLocator(nbins=4))
   ax.grid(axis='y',linestyle='--',linewidth=.4,alpha=.4)
   if case=='spec602-gcc':ax.set_axis_off()
  # Collect across panels: an accepted curve need not occur in the first app.
  legend={}
  for ax in axes.flat:
   handles,labels=ax.get_legend_handles_labels()
   for handle,label in zip(handles,labels):legend.setdefault(label,handle)
  if figure=='fig7':
   axes.flat[cases.index('spec602-gcc')].legend(list(legend.values()),list(legend),loc='center',fontsize=8)
  else:axes.flat[0].legend(list(legend.values()),list(legend),loc='upper left',bbox_to_anchor=(.02,.83),fontsize=8)
  fig.supxlabel(r'Reclamation ratio (\%)',fontsize=10,y=.01)
  fig.supylabel(r'Performance slowdown (\%)' if figure=='fig7' else r'P95 latency slowdown (\%)',fontsize=10,x=.005)
  fig.subplots_adjust(left=.095,right=.995,bottom=.13 if figure=='fig7' else .23,top=.98,wspace=.27,hspace=.36)
  fig.savefig(a.directory/(figure+'-measured.svg'),bbox_inches=None,transparent=True)
  fig.savefig(a.directory/(figure+'-measured.png'),dpi=180,bbox_inches=None,transparent=True)
  plt.close(fig)
 print('Rendered measured data and supplied targets to '+str(a.directory))
if __name__=='__main__':main()
