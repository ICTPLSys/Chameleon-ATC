#!/usr/bin/env python3
"""Sequential real-RDMA parameter search with complete trial ledger and held-out repeats."""
import argparse,importlib.util,json,subprocess,sys,time
from pathlib import Path
HERE=Path(__file__).resolve().parent;ROOT=HERE.parents[1]
s=importlib.util.spec_from_file_location('tm',HERE/'chameleon-tuning-metrics.py');m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
MEMORY={'spark-kmeans':18432,'pvc':20480,'xsbench':57344,'memcached':38912,'cassandra':53248,'liblinear':36864,'graphchi':28672,'graph500':24576}
def save(path,value):
 tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(value,indent=2)+'\n');tmp.replace(path)
def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--cases',nargs='+',choices=list(MEMORY),default=list(MEMORY));p.add_argument('--prefix',default='f78');p.add_argument('--resume',action='store_true');p.add_argument('--adopt-spark-initial',action='store_true');a=p.parse_args()
 out=ROOT/'benchmarks/results/chameleon/tuning-fig78';out.mkdir(exist_ok=True);path=out/'search.json'
 reference=json.loads((ROOT/'benchmarks/config/chameleon-tuning-reference.json').read_text())
 state=json.loads(path.read_text()) if a.resume and path.exists() else {'status':'RUNNING','reference':'benchmarks/config/chameleon-tuning-reference.json','max_distinct_candidates_per_app':16,'validation_repeats':3,'minimum_curve_points':3,'cases':{},'unavailable':{'spec602-gcc':'No licensed runnable installation'}}
 if state.get('status')=='STOPPED_BY_USER':raise RuntimeError('Old search was stopped by the user; use run-chameleon-capacity-search.py for the replacement search')
 if path.exists() and not a.resume:raise ValueError('Use --resume to retain previous trials')
 if state.get('error'):state.setdefault('interruptions',[]).append(state.pop('error'))
 state['status']='RUNNING'
 def persist():save(path,state)
 def execute(case,tag,mode,params=None,adopt=None):
  entry=state['cases'][case];name=adopt or f'{a.prefix}-{case}-{tag}';directory=ROOT/'benchmarks/results/chameleon'/name
  memory=entry['memory_mib']
  timeout=14400
  if mode=='chameleon' and 'b0' in entry['trials']:
   baseline=entry['trials'][entry.get('baseline_id','b0')]
   window=baseline.get('window_duration_seconds')
   if window is None and baseline.get('report'):
    window=json.loads(Path(baseline['report']).read_text())['cases'][case]['summary']['duration_seconds']
   if window is not None:timeout=max(300,int(window*3+120))
  cmd=[sys.executable,str(HERE/'run-sized-chameleon-apps.py'),'--name',name,'--cases',case,'--vm-memory-mib',str(memory),'--run-mode',mode,'--sample-seconds','1','--timeout',str(timeout)]
  if params:
   cmd+=['--psi-ppm',str(params['psi_ppm']),'--epoch-us',str(params['epoch_us']),'--cold-folios',str(params['cold_folios']),'--minimum-local-mib',str(params['minimum_local_mib'])]
  trial={'trial_id':tag,'name':name,'case':case,'mode':mode,'memory_mib':memory,'parameters_requested':params,'command':cmd,'status':'RUNNING','started_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())}
  if tag in entry['trials']:
   if entry['trials'][tag]['status']!='RUNNING':return entry['trials'][tag]
   trial=entry['trials'][tag];cmd=trial['command']
  entry['trials'][tag]=trial;persist();print('START '+case+' '+tag+' '+json.dumps(params),flush=True)
  if not directory.exists():
   with (out/(name+'.log')).open('w') as log:
    result=subprocess.run(cmd,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT)
   trial['launcher_exit_code']=result.returncode
  wrapper=json.loads((directory/'report.json').read_text())
  if wrapper['status']=='RUNNING':raise RuntimeError('An existing run is still active: '+name)
  record=wrapper['cases'].get(case,{})
  leaf=record.get('chameleon_report' if mode=='chameleon' else 'baseline_report')
  if leaf:trial.update(m.extract(Path(leaf).parent,case))
  else:trial.update(status='FAIL',error=wrapper.get('error','No leaf report'))
  trial['wrapper_status']=wrapper['status'];trial['finished_utc']=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())
  # A resource/VM restoration failure stops the queue; an application failure does not.
  if wrapper.get('restoration_error') or not wrapper.get('restoration'):
   trial['status']='FAIL';persist();raise RuntimeError('VM restoration incomplete: '+name)
  if mode=='chameleon':
   baseline_id=entry.get('baseline_id','b0') if tag.startswith('d') else tag.split('-')[0]+'-b'
   trial.update(m.compare(trial,entry['trials'][baseline_id],reference))
  entry['trials'][tag]=trial;persist()
  print('END '+case+' '+tag+' '+json.dumps({k:trial.get(k) for k in ['status','reclaim_percent','slowdown_percent','p95_slowdown_percent','reference_status','error']}),flush=True)
  return trial
 try:
  for case in a.cases:
   if state['cases'].get(case,{}).get('status') in ['ACCEPTED','SKIPPED_AFTER_16']:continue
   entry=state['cases'].setdefault(case,{'status':'RUNNING','memory_mib':MEMORY[case],'minimum_local_mib':MEMORY[case]-6144,'trials':{},'validation_sets':[]})
   entry.setdefault('baseline_id','b0')
   entry.setdefault('capacity_baselines',{str(MEMORY[case]):'b0'})
   baseline=execute(case,'b0','all-local',adopt='tune-spark-b0' if a.adopt_spark_initial and case=='spark-kmeans' else None)
   if baseline['status']!='PASS':
    entry['status']='BASELINE_FAILED';persist();continue
   for i in range(1,17):
    plan=json.loads((ROOT/'benchmarks/config/chameleon-tuning-candidates.json').read_text())
    candidate=plan['applications'].get(case,plan['default'])[i-1]
    existing=entry['trials'].get(f'd{i:02}')
    if not existing:
     memory=candidate.get('vm_memory_mib',entry['memory_mib'])
     if memory!=entry['memory_mib']:
      old=entry['memory_mib'];entry['memory_mib']=memory;entry['minimum_local_mib']=memory-6144
      baseline_id=entry['capacity_baselines'].setdefault(str(memory),'b'+str(memory))
      entry['baseline_id']=baseline_id
      entry.setdefault('capacity_changes',[]).append({'before':old,'after':memory,'before_candidate':i,'reason':plan.get('reason')})
      persist();baseline=execute(case,baseline_id,'all-local')
      if baseline['status']!='PASS':entry['status']='BASELINE_FAILED';persist();break
    budget=candidate['reclaim_budget_mib']
    params={k:candidate[k] for k in ['psi_ppm','epoch_us','cold_folios']}
    params['minimum_local_mib']=entry['memory_mib']-budget
    trial=execute(case,f'd{i:02}','chameleon',params,adopt='tune-spark-d01' if a.adopt_spark_initial and case=='spark-kmeans' and i==1 else None)
    discovery=[v for k,v in entry['trials'].items() if k.startswith('d') and v['memory_mib']==entry['memory_mib']]
    curve=m.find_curve(discovery,reference,[v['candidate_ids'] for v in entry['validation_sets']])
    if curve is None:continue
    ids=[r['trial_id'] for r in curve]
    if any(v['candidate_ids']==ids for v in entry['validation_sets']):continue
    validation={'candidate_ids':ids,'status':'RUNNING','rounds':[]};entry['validation_sets'].append(validation);vi=len(entry['validation_sets'])
    # Freeze the three candidates before measuring independent validation runs.
    persist()
    for rep in range(1,4):
     tag=f'v{vi}r{rep}'
     base=execute(case,tag+'-b','all-local')
     rows=[]
     for candidate in curve:rows.append(execute(case,tag+'-'+candidate['trial_id'],'chameleon',candidate['parameters_requested']))
     check=m.curve_check(rows,reference)
     # Preserve candidate ordering too; do not re-sort independently to manufacture a trend.
     if check['status']=='PASS' and [r['trial_id'].split('-')[-1] for r in sorted(rows,key=lambda r:r['reclaim_percent'])]!=ids:
      check={'status':'FAIL','reason':'Candidate order changed between repetitions'}
     validation['rounds'].append({'repeat':rep,'baseline':tag+'-b','trial_ids':[r['trial_id'] for r in rows],'check':check});persist()
    validation['status']='PASS' if all(v['check']['status']=='PASS' for v in validation['rounds']) else 'FAIL'
    if validation['status']=='PASS':
     entry['status']='ACCEPTED';entry['accepted']={'memory_mib':entry['memory_mib'],'points':[r['parameters_requested'] for r in curve],'validation':validation};persist();break
   if entry['status'] not in ['ACCEPTED','BASELINE_FAILED']:entry['status']='SKIPPED_AFTER_16';entry['reason']='No curve with three independent passing repetitions within 16 candidate tuples';persist()
   print('CASE '+case+' '+entry['status'],flush=True)
  state['status']='COMPLETE';persist()
 except BaseException as e:
  state.update(status='INTERRUPTED',error=repr(e));persist();raise
 print('RESULT '+str(path),flush=True)
if __name__=='__main__':main()
