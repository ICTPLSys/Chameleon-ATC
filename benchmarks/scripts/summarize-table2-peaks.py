#!/usr/bin/env python3
"""Accept completed, unreclaimed Guest application peaks within the profile tolerance."""
import argparse,importlib.util,json,math,re
from pathlib import Path
HERE=Path(__file__).resolve().parent
s=importlib.util.spec_from_file_location('sized',HERE/'run-sized-chameleon-apps.py');sized=importlib.util.module_from_spec(s);s.loader.exec_module(sized)
s=importlib.util.spec_from_file_location('checks',HERE/'summarize-chameleon-apps.py');checks=importlib.util.module_from_spec(s);s.loader.exec_module(checks)
def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('runs',nargs='+',type=Path)
 p.add_argument('--config',type=Path,default=HERE.parent/'config/chameleon-table2-workloads.json')
 p.add_argument('--output',type=Path,required=True);p.add_argument('--record-config',action='store_true');a=p.parse_args()
 config=json.loads(a.config.read_text());a.output.mkdir(parents=True,exist_ok=True);history=[];selected={};plans={}
 for directory in a.runs:
  report=json.loads((directory/'report.json').read_text())
  paths=[Path(c['baseline_report']).parent for c in report['cases'].values() if c.get('baseline_report')] if 'restarts' in report else [directory]
  for path in paths:
   run=json.loads((path/'report.json').read_text())
   for case,record in run['cases'].items():
    target=config['applications'][case];row={'case':case,'run':str(path),'target_peak_gib':target['target_peak_gib'],'tolerance_percent':config['tolerance_percent'],'status':'FAIL'}
    try:
     if run['status']!='PASS' or record['status']!='PASS':raise ValueError('Run did not pass')
     if not record.get('summary',{}).get('window_complete'):raise ValueError('Incomplete execution window')
     plan=sized.sizing(case,run,path/case)
     plan['workload_args']=target['args'];plan['workload_service_args']=target.get('service_args',[])
     peak=plan['peak_application_bytes']/sized.app.GIB
     error=100*(peak/target['target_peak_gib']-1)
     verification=checks.correctness(case,path/case)
     row.update(observed_peak_gib=peak,error_percent=error,within_target=sized.within_target(plan['peak_application_bytes'],target['target_peak_gib'],config['tolerance_percent']),memory_mib=plan['memory_mib'],application_check=verification)
     measured=record.get('workload_configuration',{})
     if measured.get('args')!=target['args'] or measured.get('service_args',[])!=target.get('service_args',[]):
      raise ValueError('Current arguments differ from this completed measurement; candidate superseded')
     row['status']='PASS' if row['within_target'] and verification['status'] in ['PASS','EXIT_ONLY'] else 'ADJUST_REQUIRED'
     if row['status']=='PASS':plans[case]=plan
     if a.record_config:
      target.update(status='measured-pass' if row['status']=='PASS' else 'adjust-required',measurement=row)
      # These are reproducibility references for later runs, not independent numerical proofs.
      if case=='liblinear' and verification.get('accuracy_percent'):target['reference_accuracy_percent']=float(verification['accuracy_percent'][-1])
      if case=='pvc':
       text=checks.content(path/case,'stdout.log');values={k:[int(v) for v in re.findall(r'^'+k+r'=(\d+)$',text,re.M)] for k in ['stage1_unique','stage2_urls','total_views','checksum']}
       if all(values.values()):target['reference_counts']={k:v[-1] for k,v in values.items()}
    except (ValueError,KeyError) as e:row['error']=str(e)
    history.append(row)
    if case not in selected or row['status']=='PASS' or selected[case]['status']!='PASS':selected[case]=row
 result={'target_definition':config['target_definition'],'tolerance_percent':config['tolerance_percent'],'metric':'max(all-local Guest process VmHWM, GNU time max RSS); application footprint, excludes Host clients','cases':selected,'history':history,'unavailable':config.get('unavailable',{}),'status':'PASS' if set(selected)==set(config['applications']) and all(r['status']=='PASS' for r in selected.values()) else 'INCOMPLETE'}
 (a.output/'summary.json').write_text(json.dumps(result,indent=2)+'\n')
 (a.output/'memory-config.json').write_text(json.dumps({'profile':config['name'],'headroom_mib':2048,'applications':plans},indent=2)+'\n')
 if a.record_config:a.config.write_text(json.dumps(config,indent=2)+'\n')
 print(json.dumps(result,indent=2))
if __name__=='__main__':main()
