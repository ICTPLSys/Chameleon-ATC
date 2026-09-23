#!/usr/bin/env python3
"""Summarize completed physical application runs; retain failed experiments."""
import argparse
import csv
import json
from pathlib import Path
import re
import math
import importlib.util
spec=importlib.util.spec_from_file_location('app_metrics',Path(__file__).with_name('run-chameleon-apps.py'))
metrics=importlib.util.module_from_spec(spec);spec.loader.exec_module(metrics)
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt


def content(directory, name):
    return '\n'.join(p.read_text(errors='replace') for p in (directory/'application').rglob(name))


def backing_evidence(directory):
    queries=retired=0
    errors=[]
    for path in sorted(directory.glob('qmp-*.json')):
        state=json.loads(path.read_text()); ch=state['chameleon']; queries+=1
        if ch['error-pages']: errors.append(path.name+': Host error pages')
        for r in ch['ranges']:
            if r['state']!=5: continue
            retired+=1
            if not (r['residency-status']==0 and r['resident-pages']==0 and r['flags']==2 and r['dma-unmapped'] and not r['host-installed']):
                errors.append(path.name+': retired range without saved/nonresident/DMA-unmapped state')
    return {'status':'PASS' if queries and not errors else 'FAIL','qmp_snapshots':queries,
            'retired_range_observations':retired,'errors':errors,
            'scope':'Repeated observations, not unique objects or transfer volume'}


def correctness(case, directory):
    saved=directory/'report.json'
    profile=json.loads(saved.read_text()).get('workload_configuration',{}) if saved.exists() else {}
    args=profile.get('args',[])
    def argument(name,default):
        return args[args.index(name)+1] if name in args else default
    if case=='idle': return {'status':'N/A','scope':'Idle reference'}
    if case=='xsbench':
        text=content(directory,'stdout.log'); expected=argument('--expected-checksum','952131')
        values=re.findall(r'Verification checksum: (\d+)',text)
        valid=bool(values) and all(v==expected for v in values)
        return {'status':'PASS' if valid else 'FAIL','scope':'Profile reference checksum' if profile else 'Upstream default event/large checksum','expected':expected,'checksum':values}
    if case=='liblinear':
        text=content(directory,'predict.log'); values=re.findall(r'Accuracy = ([0-9.]+)%',text)
        expected=profile.get('reference_accuracy_percent',94.7725 if not profile else None)
        if expected is None:
            return {'status':'EXIT_ONLY','scope':'New input: prediction output retained; exact baseline not yet recorded','accuracy_percent':values}
        tolerance=profile.get('reference_accuracy_tolerance_pp',0)
        valid=math.isfinite(tolerance) and tolerance>=0 and bool(values) and all(abs(float(v)-float(expected))<=tolerance+1e-12 for v in values)
        counts=[(float(v),int(n),int(total)) for v,n,total in re.findall(r'Accuracy = ([0-9.]+)% \((\d+)/(\d+)\)',text)]
        expected_records=profile.get('prepare',{}).get('records') if tolerance else None
        if tolerance:
            valid=valid and len(counts)==len(values) and all(total>0 and 0<=n<=total and
                abs(v-100*n/total)<=0.00005+1e-12 and
                (expected_records is None or total==expected_records) for v,n,total in counts)
        return {'status':'PASS' if valid else 'FAIL',
                'scope':'Prediction accuracy agrees with profile within explicit numerical tolerance; not bit-identical model validation' if tolerance else
                        ('Matches recorded accuracy for this profile; repeatability reference' if profile else 'Same KDD12 200K prefix accuracy as previous execution'),
                'accuracy_percent':values,'reference_accuracy_percent':expected,
                'accuracy_tolerance_pp':tolerance,'prediction_counts':counts,'expected_prediction_records':expected_records}
    if case=='graph500':
        text=content(directory,'stdout.log')
        metadata=dict(line.split('\t',1) for line in content(directory,'metadata.tsv').splitlines() if '\t' in line)
        expected=int(argument('--bfs-iterations',metadata.get('bfs_iterations',64)))
        ids=[int(v) for v in re.findall(r'Verifying bfs (\d+)\.\.\.done',text)]
        teams=[int(v) for v in re.findall(r'OpenMP team size: (\d+)',text)]
        cached='--graph-cache' in args or bool(metadata.get('graph_cache'))
        threads=int(argument('--threads',metadata.get('threads',8)))
        cache_loaded='Loading CSR checkpoint... done.' in text
        valid=ids==list(range(expected)) and (not cached or (teams==[threads] and cache_loaded))
        return {'status':'PASS' if valid else 'FAIL','validated_bfs':len(ids),
                'expected_bfs':expected,'openmp_team_sizes':teams,'expected_threads':threads,
                'cache_required':cached,'cache_loaded':cache_loaded}
    if case=='pvc':
        text=content(directory,'stdout.log'); expected={'stage1_unique':40265318,'stage2_urls':2677734,'total_views':40265318,'checksum':935682566953571500}
        if profile:
            expected=profile.get('reference_counts')
            if not expected:return {'status':'EXIT_ONLY','scope':'New input: deterministic counts retained; baseline not yet recorded'}
        values={k:[int(v) for v in re.findall(r'^'+k+r'=(\d+)$',text,re.M)] for k in expected}
        return {'status':'PASS' if all(values[k] and all(v==want for v in values[k]) for k,want in expected.items()) else 'FAIL','scope':('Matches recorded counts for this profile; repeatability reference' if profile else 'Same 1 GiB input matches previous deterministic result'),'values':values}
    if case=='spark-kmeans':
        text=content(directory,'stdout.log')
        values=[float(v) for v in re.findall(r'^Within Set Sum of Squared Errors = (\S+)',text,re.M)]
        costs=[float(v) for v in re.findall(r'^Cost: (\S+)',text,re.M)]
        centers=[[float(v) for v in s.split(',')] for s in re.findall(r'^\[([-0-9.eE+, ]+)\]$',text,re.M)]
        valid=(bool(values) and len(values)==len(costs) and len(centers)==4 and
               all(len(c)==2 and all(math.isfinite(v) for v in c) for c in centers) and
               all(math.isfinite(v) and v>=0 and math.isclose(v,c,rel_tol=1e-12) for v,c in zip(values,costs)))
        return {'status':'PASS' if valid else 'FAIL','scope':'Four finite centers and two same-run cached-data cost scans agree. Driver does not fix initialization seed; no independent exact expected SSE is claimed.','sse':values,'cost':costs,'centers':centers}
    if case=='cassandra':
        load=content(directory,'load.log'); run=content(directory,'run.log')
        insert=re.findall(r'^\[INSERT\], Return=OK, (\d+)',load,re.M); read=re.findall(r'^\[READ\], Return=OK, (\d+)',run,re.M)
        validations=list((directory/'application').rglob('load-validation.json'))
        validation=json.loads(validations[0].read_text()) if len(validations)==1 else {}
        tolerated=(validation.get('status')=='PASS' and validation.get('acceptance_policy')=='load-insert-errors-nonfatal-v1' and
                   bool(validation.get('warnings')) and insert==[str(validation.get('insert_ok'))])
        return {'status':'PASS' if (insert==[argument('--records','100000')] or tolerated) and read==[argument('--operations','1000000')] else 'FAIL','scope':'YCSB read success; user permits load INSERT errors. Actual load counts retained; not an independent full database-content scan','insert_ok':insert,'read_ok':read,'load_validation':validation}
    if case=='memcached':
        rows=[]
        for path in (directory/'application').rglob('generator.csv'):
            with path.open() as f: rows.extend(csv.DictReader(f))
        return {'status':'PASS' if rows and all(r.get('load_valid')=='1' and r.get('schedule_complete')=='1' for r in rows) else 'FAIL','scope':'Generator validity/completion; preserves miss, timeout and latency counters','generator_rows':rows}
    return {'status':'EXIT_ONLY','scope':'GraphChi execution completed; PageRank output retained, no independent numerical oracle'}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('runs',nargs='+',type=Path)
    p.add_argument('--output',required=True,type=Path)
    a=p.parse_args(); a.output.mkdir(parents=True,exist_ok=True)
    selected={}; history=[]; suites=[]; runs=[]
    for run in a.runs:
        data=json.loads((run/'report.json').read_text())
        if 'restarts' in data:
            runs.extend(Path(c['chameleon_report']).parent for c in data['cases'].values() if c.get('chameleon_report'))
        else:
            runs.append(run)
    if not runs:
        raise ValueError('No completed application runs to summarize')
    for run in runs:
        suite=json.loads((run/'report.json').read_text())
        if suite['configuration'].get('mode','chameleon')!='chameleon':
            raise ValueError('Supply Chameleon runs only; all-local profiles are sizing evidence')
        total=suite['before']['qmp']['chameleon']['policy']['total-bytes']
        after=suite.get('after',{}); before=suite.get('before',{})
        ch=after.get('qmp',{}).get('chameleon',{})
        health={
            'restoration_recorded':bool(suite.get('restoration')),
            'no_cleanup_errors':not suite.get('cleanup_errors'),
            'same_guest_boot':bool(after) and after['rdma']['boot_id']==before['rdma']['boot_id'],
            'same_qemu_process':bool(after) and after['host']['pid']==before['host']['pid'],
            'free_capacity_restored':bool(ch) and ch.get('policy',{}).get('hard-reclaimed-bytes')==before.get('qmp',{}).get('chameleon',{}).get('policy',{}).get('hard-reclaimed-bytes'),
            'host_resources_empty':bool(ch) and all(ch[k]==0 for k in ['range-records','retired-pages','registered-pages','ready-pages','blocked-pages','error-pages']),
        }
        suites.append({'report':str(run/'report.json'),'original_status':suite['status'],
                       'restoration_checks':health,'cleanup_errors':suite.get('cleanup_errors',[])})
        for case,record in suite['cases'].items():
            if record['status']=='RUNNING': continue
            directory=run/case
            complete='end' in record and 'exit_code' in record
            record=dict(record,summary=dict(metrics.summarize(record['samples'],total),window_complete=complete))
            check=correctness(case,directory)
            entry={'case':case,'status':record['status'],'report':str(directory/'report.json'),'settings':suite.get('settings',{}),'memory_plan':suite.get('memory_plan'),'application_check':check,'summary':record.get('summary',{}),'checks':record['checks'],'counter_delta':record.get('counter_delta',{}),'reclamation_observed':record.get('reclamation_observed',False),'physical_rdma_io_observed':record.get('physical_rdma_io_observed',False)}
            active=[s for s in record['samples'] if s['phase']=='running']
            entry['measurement_checks']={
                'complete_application_window':complete,
                'controls_enabled':bool(active) and all(all(s[g]['enabled']==1 for g in ['tracker','manager','policy']) for s in active),
                'no_synthetic_heat':bool(active) and all(s['tracker']['synthetic_samples']==0 for s in active),
                'free_page_configuration_matches':bool(active) and all(s['policy']['free_pages']==suite.get('settings',{}).get('free_pages',0) for s in active),
                'hardware_sampling':case=='idle' or record.get('counter_delta',{}).get('tracker',{}).get('hardware_samples',0)>0,
            }
            if not all(entry['measurement_checks'].values()): entry['status']='FAIL'
            entry['backing_evidence']=backing_evidence(directory)
            if entry['backing_evidence']['status']=='FAIL': entry['status']='FAIL'
            if check['status']=='FAIL': entry['status']='FAIL'
            history.append(entry)
            if case not in selected or entry['status']=='PASS' or selected[case][0]['status']!='PASS': selected[case]=(entry,record)
    result={'status':'PASS' if set(metrics.APPS)<=set(selected) and all(e['status']=='PASS' for e,r in selected.values()) and all(all(s['restoration_checks'].values()) for s in suites) else 'INCOMPLETE_OR_FAILED',
       'metric':'Paper section 6: 100*(1-arithmetic_mean(QEMU RSS samples)/initial configured VM RAM). Per-run denominators; no clipping or overhead subtraction. Peak ratio is supplemental. Backing retirement is a separate diagnostic.',
       'limitations':['Scaled applications and existing inputs; not paper datasets/configurations.','Cassandra and Memcached request traffic uses existing SSH/user-network paths; Hermit far memory uses physical mlx5 RDMA.','QEMU RSS includes Guest memory and QEMU overhead; these are whole-VM ratios, not application-only ratios.','No all-local timing baseline or calibrated slowdown claim.','Sampled peaks may miss sub-interval transients.'],
       'cases':{k:e for k,(e,r) in selected.items()},'history':history,'suites':suites,'spec_cpu2017':'NOT RUN: no licensed runnable installation supplied'}
    (a.output/'summary.json').write_text(json.dumps(result,indent=2)+'\n')
    with (a.output/'summary.csv').open('w') as f:
        w=csv.writer(f); w.writerow(['application','status','mean_reclaim_percent','peak_reclaim_percent','configured_memory_mib','mean_qemu_rss_mib','mean_backing_reclaim_percent','peak_backing_reclaim_percent','mean_retired_mib','peak_retired_mib','window_seconds','application_check','window_complete','mean_free_reclaimed_mib','peak_free_reclaimed_mib','free_reclaimed_total_mib','free_returned_total_mib'])
        for name,(e,r) in selected.items():
            s=e['summary']; mean=s.get('mean_retired_bytes')
            w.writerow([name,e['status'],s.get('mean_reclaim_percent'),s.get('peak_reclaim_percent'),s['configured_memory_bytes']/2**20,s['mean_qemu_rss_bytes']/2**20 if s['mean_qemu_rss_bytes'] is not None else None,s['mean_backing_reclaim_percent'],s['peak_backing_reclaim_percent'],None if mean is None else mean/2**20,s.get('peak_retired_bytes',0)/2**20,s.get('duration_seconds'),e['application_check']['status'],s['window_complete'],None if s['mean_free_reclaimed_bytes'] is None else s['mean_free_reclaimed_bytes']/2**20,s['peak_free_reclaimed_bytes']/2**20,e['counter_delta'].get('policy',{}).get('free_reclaimed_bytes',0)/2**20,e['counter_delta'].get('policy',{}).get('free_returned_bytes',0)/2**20])
    fig,axes=plt.subplots(3,3,figsize=(12,8),layout='constrained')
    for ax,(name,(entry,record)) in zip(axes.flat,selected.items()):
        rows=[r for r in record['samples'] if r['phase']=='running']
        if rows:
            ax.plot([r['seconds']-rows[0]['seconds'] for r in rows],[100*(1-r['compute_memory']['Rss']/entry['summary']['configured_memory_bytes']) for r in rows],color='#286a9b')
        else:
            ax.text(.5,.5,'No valid running samples',transform=ax.transAxes,ha='center')
        ax.set_title(name+' / '+entry['status']+(' / partial' if not entry['summary']['window_complete'] else '')); ax.set_xlabel('Elapsed seconds'); ax.set_ylabel('RSS reclamation (%)')
        ax.axhline(0,color='gray',linewidth=.6); ax.grid(axis='y',alpha=.25)
    for ax in list(axes.flat)[len(selected):]: ax.set_visible(False)
    fig.suptitle('Chameleon + physical RDMA: QEMU RSS reclamation / configured VM RAM')
    for ext in ['svg','png']: fig.savefig(a.output/('reclamation-curves.'+ext),dpi=150)
    plt.close(fig)
    names=list(selected); means=[selected[n][0]['summary'].get('mean_reclaim_percent') if selected[n][0]['summary'].get('mean_reclaim_percent') is not None else float('nan') for n in names]; peaks=[selected[n][0]['summary'].get('peak_reclaim_percent') if selected[n][0]['summary'].get('peak_reclaim_percent') is not None else float('nan') for n in names]
    fig,ax=plt.subplots(figsize=(10,4.5),layout='constrained'); x=list(range(len(names)))
    ax.bar([v-.18 for v in x],means,.36,label='Paper mean (RSS samples)',color='#286a9b'); ax.bar([v+.18 for v in x],peaks,.36,label='Sampled peak',color='#91b4cc')
    labels=[n if selected[n][0]['status']=='PASS' else n+(' (FAIL, partial)' if not selected[n][0]['summary']['window_complete'] else ' (FAIL)') for n in names]
    ax.set_xticks(x,labels,rotation=25,ha='right'); ax.set_ylabel('1 - QEMU RSS / configured VM RAM (%)'); ax.legend(frameon=False); ax.grid(axis='y',alpha=.2)
    for ext in ['svg','png']: fig.savefig(a.output/('reclamation-rates.'+ext),dpi=150)
    plt.close(fig)
    print(json.dumps({'status':result['status'],'cases':{n:e['status'] for n,(e,r) in selected.items()}},indent=2))

if __name__=='__main__': main()
