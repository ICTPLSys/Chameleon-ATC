#!/usr/bin/env python3
"""Validate a shared PEBS/HHH profile before resuming reclaim-curve tuning."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time

HERE=Path(__file__).resolve().parent
REPO=HERE.parents[1]
RESULTS=REPO/'benchmarks/results/chameleon'


def save(path,value):
    temp=path.with_suffix('.tmp');temp.write_text(json.dumps(value,indent=2)+'\n');temp.replace(path)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seed-report',type=Path)
    parser.add_argument('--directory',type=Path,default=RESULTS/'tracking-calibration-fresh')
    a=parser.parse_args();a.directory.mkdir(exist_ok=True,parents=True)
    lock=(a.directory/'lock').open('w');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    plan=json.loads((REPO/'benchmarks/config/chameleon-feedback-search-v3.json').read_text())
    path=a.directory/'search.json'
    state=json.loads(path.read_text()) if path.exists() else {
        'status':'RUNNING','purpose':'Global PEBS+HHH overhead calibration; reclaim search remains paused',
        'acceptance':{'pairs':3,'mean_percent':2,'single_percent':5,'kv_p95_required':True},
        'candidates':[],'accepted':None}
    state.update(status='RUNNING',pid=os.getpid());save(path,state)
    try:
        for period in [65536,131072,262144,524288]:
            found=[c for c in state['candidates'] if c['sample_period']==period]
            if found:
                candidate=found[0]
            else:
                candidate={'sample_period':period,'cooling_samples':131072,'hhh_interval_ms':15000,'cases':{}}
                state['candidates'].append(candidate)
            failed=False
            for case,app in plan['applications'].items():
                if (a.directory/'STOP').exists():
                    state['status']='STOPPED_BETWEEN_TRIALS';save(path,state);return 0
                if candidate['cases'].get(case,{}).get('status')=='PASS':continue
                name='trackfresh'+str(period)+'-'+case
                report=RESULTS/name/'report.json'
                if period==65536 and case=='spark-kmeans' and a.seed_report:
                    report=a.seed_report
                    state['active']={'case':case,'sample_period':period,'report':str(report),'phase':'waiting_for_seed_restoration'};save(path,state)
                    # The already-running seed owns the VM until its restoration completes.
                    while True:
                        seed=json.loads(report.read_text())
                        if seed.get('restoration') or seed.get('restoration_error'):break
                        time.sleep(5)
                else:
                    if report.exists():
                        existing=json.loads(report.read_text())
                        if not existing.get('restoration'):
                            raise RuntimeError('Inspect interrupted or active trial before resuming: '+str(report))
                    else:
                        argv=[sys.executable,str(HERE/'run-sized-chameleon-apps.py'),'--name',name,'--cases',case,
                              '--vm-memory-mib',str(app['memory_steps_mib'][0]),'--tracking-calibration',
                              '--sample-period',str(period),'--hhh-interval-ms','15000','--sample-seconds','1','--timeout','3600']
                        state['active']={'case':case,'sample_period':period,'report':str(report),'command':argv}
                        save(path,state);print('START',case,period,flush=True)
                        with (a.directory/(name+'.log')).open('w') as log:
                            process=subprocess.Popen(argv,stdout=log,stderr=subprocess.STDOUT)
                            state['active']['pid']=process.pid;save(path,state)
                            process.wait()
                raw=json.loads(report.read_text());entry=raw['cases'][case]
                if not entry.get('cache_protocol') or not entry.get('fresh_vm_per_execution'):raise RuntimeError('Missing controlled cache protocol: '+str(report))
                config=raw['configuration']
                if config.get('tracking_components','both')!='both' or not config.get('tracking_calibration') or config['sample_period']!=period or config['hhh_interval_ms']!=15000 or config['cooling_samples']!=131072 or config['vm_memory_mib']!=app['memory_steps_mib'][0]:
                    raise RuntimeError('Calibration configuration mismatch: '+str(report))
                value={'status':entry.get('overhead',{}).get('status','INVALID'),'report':str(report),
                       'overhead':entry.get('overhead'),'memory_mib':app['memory_steps_mib'][0]}
                if raw.get('restoration_error') or not raw.get('restoration'):
                    raise RuntimeError('VM restoration failed: '+str(report))
                if raw.get('error') or entry.get('error'):
                    value['status']='INVALID';value['error']=raw.get('error',entry.get('error'))
                candidate['cases'][case]=value;state.pop('active',None);save(path,state)
                print('END',case,period,json.dumps(value),flush=True)
                if value['status']=='INVALID':
                    state['status']='NEEDS_DIAGNOSIS';save(path,state);return 1
                if value['status']!='PASS':
                    failed=True;break
            if not failed:
                state.update(status='PASS',accepted=candidate);save(path,state)
                save(REPO/'benchmarks/config/chameleon-tracking-calibrated.json',{
                    'status':'PASS','sample_period':period,'cooling_samples':131072,'hhh_interval_ms':15000,
                    'evidence':str(path),'applications':candidate['cases'],
                    'scope':'All-local overhead only, at recorded VM sizes. Revalidate if workload/capacity changes.'})
                return 0
        state['status']='NO_SHARED_PROFILE_FOUND';save(path,state);return 1
    except BaseException as error:
        state.update(status='INTERRUPTED',error=repr(error));save(path,state);raise


if __name__=='__main__':raise SystemExit(main())
