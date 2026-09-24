#!/usr/bin/env python3
"""Run targeted physical-RDMA microbenchmarks sequentially on one existing VM."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
CASES = {
    'split': ['--scenario','split','--gib','2','--reclaim-mib','512',
              '--pattern','striped','--stripe-hot-pages','64','--hot-access-ppm','1000000',
              '--sampling-period','512',
              '--cooling-samples','16777216','--warmup-seconds','60',
              '--split-warm-seconds','60','--run-seconds','60','--reclaim-timeout','120'],
    'batch-deferred': ['--scenario','batch','--ept-mode','deferred','--ept-batch-pages','4096'],
    'batch-immediate': ['--scenario','batch','--ept-mode','immediate','--ept-batch-pages','4096'],
    'psi': ['--scenario','psi','--psi-ppm','10000','--hot-access-ppm','1000000'],
    'hotspot': ['--scenario','hotspot','--hot-access-ppm','1000000',
                '--gib','4','--reclaim-mib','3072','--hot-percent','25',
                '--warmup-seconds','60','--run-seconds','90','--reclaim-timeout','180',
                '--sampling-period','512','--cooling-samples','1048576'],
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--name', default='micro-suite-'+time.strftime('%Y%m%d-%H%M%S'))
    parser.add_argument('--vm', default='guest-tools-final')
    parser.add_argument('--netdev', default='ibp1s0')
    parser.add_argument('--cases', nargs='+', choices=CASES, default=list(CASES))
    args = parser.parse_args()
    if not args.name or len(args.name)>50 or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in args.name):
        parser.error('name must be a unique simple identifier of at most 50 characters')
    output = ROOT/'results'/args.name
    output.mkdir()
    report = {'status':'RUNNING','scope':'Functional mechanism checks; no runtime/slowdown performance claim',
              'vm':args.vm,'cases':{},'comparisons':{}}
    def save():
        (output/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    save()
    for name in args.cases:
        label=args.name+'-'+name
        command=[sys.executable,str(ROOT/'scripts/test-physical-micro.py'),
                 '--vm',args.vm,'--netdev',args.netdev,'--name',label,
                 '--gib','8','--threads','4','--reclaim-mib','1024',
                 '--warmup-seconds','30','--run-seconds','45','--reclaim-timeout','120',
                 '--cooling-samples','131072','--sample-seconds','5']+CASES[name]
        record=report['cases'][name]={'command':command,'report':str(ROOT/'results'/('vm-'+label)/'report.json')}
        save()
        print('START '+name,flush=True)
        with (output/(name+'.log')).open('w') as log:
            result=subprocess.run(command,stdout=log,stderr=subprocess.STDOUT,cwd=ROOT)
        record['exit_code']=result.returncode
        path=Path(record['report'])
        if path.is_file():
            leaf=json.loads(path.read_text())
            record['status']=leaf['status']
            record['checks']=leaf.get('checks',{})
            record['summary']=leaf.get('summary',{})
            evidence=path.parent/'mechanism-evidence.json'
            if evidence.is_file(): record['evidence']=json.loads(evidence.read_text())
            if leaf.get('cleanup_errors'):
                report['stopped_after_cleanup_error']=name
                save()
                break
        else:
            record['status']='FAIL'
            record['error']='Runner did not produce a report; inspect '+str(output/(name+'.log'))
        print(name+' '+record['status'],flush=True)
        save()
    if all(n in report['cases'] and 'evidence' in report['cases'][n] for n in ('batch-deferred','batch-immediate')):
        deferred,immediate=(report['cases'][n]['evidence']['phase_intervals']
                            ['baseline->active_reclaim_end']['ept']
                            for n in ('batch-deferred','batch-immediate'))
        report['comparisons']['batching']={
            'deferred_ranges_per_flush':deferred['ranges_per_flush'],
            'immediate_ranges_per_flush':immediate['ranges_per_flush'],
            'pass':deferred['multi_range_begin_count']>0 and immediate['maximum_ranges']==1 and
                   deferred['ranges_per_flush']>immediate['ranges_per_flush'],
            'scope':'Initial reclaim phase with fixed 4096-page trigger, before tail drain; not a timing speedup.'}
    report['status']='PASS' if (len(report['cases'])==len(args.cases) and
        all(c.get('status')=='PASS' and c.get('exit_code')==0 for c in report['cases'].values()) and
        all(c['pass'] for c in report['comparisons'].values())) else 'FAIL'
    save()
    print(str(output/'report.json'),flush=True)
    return 0 if report['status']=='PASS' else 1


if __name__=='__main__':
    raise SystemExit(main())
