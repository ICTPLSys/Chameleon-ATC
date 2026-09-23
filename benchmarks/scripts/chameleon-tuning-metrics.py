#!/usr/bin/env python3
"""Extract real run metrics and compare against explicitly supplied target curves."""
import csv, importlib.util, itertools, json, math, re
from pathlib import Path
HERE=Path(__file__).resolve().parent
MECHANISM_CRITERION='measured-reclaim-free-only-allowed-v3'
TREND_CRITERION='three-repeat-mean-with-observed-variation-v1'
REFERENCE_TOLERANCE_PP=1.0
REFERENCE_CRITERION='repeat-excess-at-most-1pp-mean-strictly-below-v1'
spec=importlib.util.spec_from_file_location('checks',HERE/'summarize-chameleon-apps.py')
checks=importlib.util.module_from_spec(spec);spec.loader.exec_module(checks)

def mechanism_observed(run):
    """Factual cold/RDMA observation, not a per-point acceptance requirement."""
    rdma=run.get('counters',{}).get('rdma',{})
    return (run.get('cold_peak_mib',0)>0 and
            rdma.get('write_bytes',0)>0 and rdma.get('read_bytes',0)>0)

def elapsed(path):
    lines=[s for s in path.read_text().splitlines() if 'Elapsed (wall clock)' in s]
    if len(lines)!=1:raise ValueError('Missing/ambiguous GNU elapsed time: '+str(path))
    parts=[float(v) for v in lines[0].rsplit(': ',1)[1].split(':')]
    return sum(v*60**i for i,v in enumerate(reversed(parts)))

def performance(case,directory):
    app=directory/'application'
    if case=='memcached':
        rows=[]
        for p in app.rglob('generator.csv'):
            with p.open() as stream:
                rows.extend(csv.DictReader(stream))
        if len(rows)!=1:raise ValueError('Expected one complete Memcached sample')
        r=rows[0];goodput=float(r['goodput_mops'])
        if goodput<=0:raise ValueError('No Memcached goodput')
        return {'runtime_seconds':None,'cost':1/goodput,'cost_metric':'inverse completed-request throughput; fixed offered rate can mask capacity changes',
                'throughput_ops':goodput*1e6,'mean_latency_us':float(r['completed_mean_us']),
                'p95_us':float(r['completed_p95_us']),'p99_us':float(r['completed_p99_us']),
                'load_valid':r['load_valid'],'schedule_complete':r['schedule_complete']}
    if case=='cassandra':
        files=list(app.rglob('run.log'))
        if len(files)!=1:raise ValueError('Expected one YCSB read phase')
        text=files[0].read_text()
        def metric(op,key):
            matches=re.findall(r'^\['+op+r'\], '+re.escape(key)+r', (\S+)$',text,re.M)
            if len(matches)!=1:raise ValueError('Missing YCSB '+key)
            return float(matches[0])
        seconds=metric('OVERALL','RunTime(ms)')/1000
        validations=list(app.rglob('load-validation.json'))
        load_validation=json.loads(validations[0].read_text()) if len(validations)==1 else None
        return {'runtime_seconds':seconds,'cost':seconds,'cost_metric':'YCSB fixed-count read-phase runtime',
                'load_validation':load_validation,
                'throughput_ops':metric('OVERALL','Throughput(ops/sec)'),
                'mean_latency_us':metric('READ','AverageLatency(us)'),
                'p95_us':metric('READ','95thPercentileLatency(us)'),
                'p99_us':metric('READ','99thPercentileLatency(us)')}
    files=sorted(app.rglob('*time.txt'))
    if not files:raise ValueError('No Guest GNU time evidence')
    seconds=sum(elapsed(p) for p in files)
    return {'runtime_seconds':seconds,'cost':seconds,'cost_metric':'sum of Guest timed application executions, including initialization; Liblinear train+predict',
            'time_files':[str(p) for p in files]}

def extract(directory,case):
    report=json.loads((directory/'report.json').read_text());r=report['cases'][case]
    result={'report':str(directory/'report.json'),'status':'FAIL','case':case}
    try:
        if report['status']!='PASS' or r['status']!='PASS':raise ValueError('Incomplete/failed application or infrastructure')
        verification=checks.correctness(case,directory/case)
        if verification['status'] not in ['PASS','EXIT_ONLY']:raise ValueError('Application correctness check failed')
        if not r['summary']['window_complete']:raise ValueError('Incomplete measurement window')
        b,e=r['baseline'],r['end'];active=[v for v in r['samples'] if v['phase']=='running']
        result.update(status='PASS',performance=performance(case,directory/case),application_check=verification,
            memory_mib=r['summary']['configured_memory_bytes']//2**20,window_duration_seconds=r['summary']['duration_seconds'],
            reclaim_percent=r['summary']['mean_reclaim_percent'],
            cold_peak_mib=max(v['retired_bytes'] for v in active)/2**20,
            cold_mean_percent=r['summary']['mean_backing_reclaim_percent'],
            free_mean_mib=r['summary'].get('mean_free_reclaimed_bytes',0)/2**20,
            counters={g:{k:e[g][k]-b[g][k] for k in keys} for g,keys in {
                'rdma':['write_bytes','read_bytes','write_completions','read_completions','transfer_errors','map_failures'],
                'shadow':['demand_fault_successes','demand_fault_failures','data_save_failure','data_load_failure'],
                'policy':['epochs','low_epochs','high_epochs','psi_some_ns','psi_full_ns','action_errors']}.items()},
            parameters=report.get('effective_policy',{}),mode=report['configuration']['mode'],
            workload_configuration=r.get('workload_configuration'),checks=r['checks'],
            physical_rdma=report['physical_rdma'])
        result['baseline_protocol']=report.get('baseline_protocol','all-local-controls-off-legacy')
        result['cpu_affinity_profile']=report.get('cpu_affinity',{}).get('profile')
        result['tracking_profile']={k:report['settings'].get(k) for k in ['sampling','cooling','hhh_interval_ms']}
        if case=='spark-kmeans':
            metadata=list((directory/case/'application').rglob('metadata.tsv'))
            if len(metadata)!=1:raise ValueError('Expected one Spark metadata file')
            fields=dict(line.split('\t',1) for line in metadata[0].read_text().splitlines() if '\t' in line)
            result['workload_identity']={k:fields.get(k) for k in ['initialization_seed','application_source_sha256','input_sha256']}
            stdout=list((directory/case/'application').rglob('stdout.log'))
            if len(stdout)==1:
                text=stdout[0].read_text()
                for key,pattern in {
                    'spark_initialization_seconds':r'Initialization with k-means\|\| took ([0-9.]+) seconds',
                    'spark_iterations_seconds':r'KMeans: Iterations took ([0-9.]+) seconds',
                    'spark_within_set_sse':r'Within Set Sum of Squared Errors = ([0-9.Ee+-]+)'}.items():
                    found=re.findall(pattern,text)
                    if len(found)==1:result['performance'][key]=float(found[0])
        if result['mode']=='chameleon':
            result['mechanism_observed']=mechanism_observed(result)
            result['mechanism_criterion']=MECHANISM_CRITERION
    except (ValueError,KeyError) as ex:result.update(status='FAIL',error=str(ex))
    return result

def compare_shared_baseline(run, fixed, paired, matched, reference):
    """Use the first tracked all-local as denominator; retain each round's alternative."""
    clean=dict(run)
    clean.pop('per_repeat_normalization',None)
    clean.pop('comparison_error',None)
    value=compare_fixed_tracking(clean,fixed,matched,reference)
    alternative=compare_fixed_tracking(clean,paired,matched,reference)
    value['normalization_protocol']='fixed-first-capacity-all-local-v1'
    value['per_repeat_normalization']={k:alternative.get(k) for k in (
        'status','baseline_report','slowdown_percent','p95_slowdown_percent','p99_slowdown_percent',
        'reference_status','reference_margin_pp','reference_point_coverage','reference_comparison_mode','comparison_error','tracking_only_overhead_percent',
        'reclaim_over_matched_tracking_percent')}
    value['per_repeat_normalization']['baseline_performance']=paired.get('performance')
    value['per_repeat_normalization']['scope']='Diagnostic normalization using the newly measured round all-local; main results use the fixed first same-capacity tracked all-local.'
    return value


def interpolate(series,x):
    if not series[0][0]<=x<=series[-1][0]:raise ValueError('Outside reference interpolation domain')
    for (a,b),(c,d) in zip(series,series[1:]):
        if a<=x<=c:return b+(d-b)*(x-a)/(c-a)
    return series[-1][1]

def reference_point(value,reference):
    overlap=reference.get('comparison_domains',{}).get(value['case'])=='per-baseline-overlap'
    margins={};coverage={};known=False
    for fig,metric in [('fig7','slowdown_percent'),('fig8','p95_slowdown_percent')]:
        if value['case'] not in reference[fig]:continue
        margins[fig]={};coverage[fig]={}
        for name,points in reference[fig][value['case']].items():
            known=True
            inside=points[0][0]<=value['reclaim_percent']<=points[-1][0]
            coverage[fig][name]={'reference_domain':[points[0][0],points[-1][0]],
                                 'status':'COMPARED' if inside else 'OUTSIDE_REFERENCE_DOMAIN'}
            if not inside and overlap:continue
            margins[fig][name]=interpolate(points,value['reclaim_percent'])-value[metric]
    values=[v for fig in margins.values() for v in fig.values()]
    status=('PASS' if all(v>0 for v in values) else 'FAIL') if values else (
        'OUTSIDE_DOMAIN' if overlap and known else 'FAIL')
    return {'reference_status':status,'reference_margin_pp':margins,
            'reference_point_coverage':coverage,
            'reference_comparison_mode':'per-baseline-overlap' if overlap else 'full-point-domain'}

def compare(run,baseline,reference,allow_tracking_mismatch=False):
    value=dict(run);value['reference_status']='FAIL'
    value['mechanism_observed']=mechanism_observed(run)
    value['mechanism_criterion']=MECHANISM_CRITERION
    if run['status']!='PASS' or baseline['status']!='PASS':return value
    if run.get('baseline_protocol')!=baseline.get('baseline_protocol') or (not allow_tracking_mismatch and run.get('tracking_profile')!=baseline.get('tracking_profile')):
        value['comparison_error']='Baseline tracking protocol/profile differs';return value
    if run.get('cpu_affinity_profile')!=baseline.get('cpu_affinity_profile'):
        value['comparison_error']='Baseline CPU affinity differs';return value
    if run['memory_mib']!=baseline['memory_mib'] or run['workload_configuration']!=baseline['workload_configuration'] or run.get('workload_identity')!=baseline.get('workload_identity'):
        value['comparison_error']='Baseline workload/VM differs';return value
    value['slowdown_percent']=100*(run['performance']['cost']/baseline['performance']['cost']-1)
    value['incremental_reclaim_pp']=run['reclaim_percent']-baseline['reclaim_percent']
    value['baseline_report']=baseline['report']
    if 'p95_us' in run['performance']:
        value['p95_slowdown_percent']=100*(run['performance']['p95_us']/baseline['performance']['p95_us']-1)
        value['p99_slowdown_percent']=100*(run['performance']['p99_us']/baseline['performance']['p99_us']-1)
    try:
        value.update(reference_point(value,reference))
        # Free-only points are valid measurements of the enabled policy at its
        # local floor. Keep the actual cold/RDMA counters, including zeros.
    except ValueError as e:value['comparison_error']=str(e)
    return value

def compare_fixed_tracking(run,baseline,matched,reference):
    """Total cost vs fixed tracked baseline; separate matched-profile diagnostic."""
    value=compare(run,baseline,reference,allow_tracking_mismatch=True)
    value['performance_reference']='fixed-tracking-with-matched-diagnostic-v1'
    value['baseline_tracking_profile']=baseline.get('tracking_profile')
    if run['status']!='PASS' or baseline['status']!='PASS' or value.get('comparison_error'):return value
    keys=('memory_mib','workload_configuration','workload_identity','baseline_protocol','tracking_profile','cpu_affinity_profile')
    if matched.get('status')!='PASS' or any(run.get(k)!=matched.get(k) for k in keys):
        value.update(status='FAIL',reference_status='FAIL',comparison_error='Matched tracking baseline failed or differs')
        return value
    value['matched_tracking_baseline_report']=matched['report']
    value['tracking_only_overhead_percent']=100*(matched['performance']['cost']/baseline['performance']['cost']-1)
    value['reclaim_over_matched_tracking_percent']=100*(run['performance']['cost']/matched['performance']['cost']-1)
    value['decomposition_scope']='(1+total)=(1+tracking_only)*(1+reclaim_over_matched), using fractional slowdowns; timing variability retained'
    if 'p95_us' in run['performance']:
        value['tracking_only_p95_overhead_percent']=100*(matched['performance']['p95_us']/baseline['performance']['p95_us']-1)
        value['reclaim_over_matched_tracking_p95_percent']=100*(run['performance']['p95_us']/matched['performance']['p95_us']-1)
    return value

def trend_criterion(repeats):
    return TREND_CRITERION if repeats==3 else 'two-repeat-mean-with-observed-variation-v1'

def reference_acceptable(run,tolerance_pp=0):
    if run.get('status','PASS')!='PASS' or run.get('comparison_error'):return False
    if run.get('reference_status')=='PASS':return True
    if run.get('reference_status')=='OUTSIDE_DOMAIN' and run.get('reference_comparison_mode')=='per-baseline-overlap':return True
    margins=[v for fig in run.get('reference_margin_pp',{}).values() for v in fig.values()]
    return tolerance_pp>0 and bool(margins) and all(v>=-tolerance_pp for v in margins)

def covers_reclaim_range(rows,limits):
    if not limits:return True
    xs=sorted(r['reclaim_percent'] for r in rows)
    return (len(xs)>=3 and 0<xs[0]<=limits['low_max_percent'] and
            any(limits['middle_min_percent']<=x<=limits['middle_max_percent'] for x in xs[1:-1]) and
            xs[-1]>=limits['high_min_percent'])

def near_flat_memcached_throughput(rows, tolerance_pp):
    if tolerance_pp <= 0 or not rows or any(r['case'] != 'memcached' for r in rows):
        return False
    values = [r['slowdown_percent'] for r in rows]
    return max(map(abs, values)) <= tolerance_pp and max(values)-min(values) <= tolerance_pp


def curve_check(rows,reference,check_trend=True,reference_tolerance_pp=0,include_all_local=False,reclaim_range=None,trend_from_reclaimed_only=False,flat_throughput_tolerance_pp=0):
    """Check every point AND intervening reference knots, without extrapolation."""
    if len(rows)<3:return {'status':'FAIL','reason':'Need at least three measured points'}
    if len({r['memory_mib'] for r in rows})!=1:return {'status':'FAIL','reason':'VM sizes differ'}
    if any(not reference_acceptable(r,reference_tolerance_pp) for r in rows):return {'status':'FAIL','reason':'A point failed functional or reference acceptance'}
    excess=max([0]+[-v for r in rows for fig in r.get('reference_margin_pp',{}).values() for v in fig.values()])
    policies=[r.get('parameters') or r.get('parameters_requested') or {} for r in rows]
    keys=('psi_ppm','epoch_us','cold_folios')
    if any(any(k not in p for k in keys) for p in policies):return {'status':'FAIL','reason':'Missing policy parameters'}
    if len({tuple(p[k] for k in keys)+(r.get('tracking_profile',{}).get('sampling'),r.get('tracking_profile',{}).get('hhh_interval_ms'))
            for p,r in zip(policies,rows)})<2:return {'status':'FAIL','reason':'Only local capacity changes; require a policy or tracking parameter change'}
    rows=sorted(rows,key=lambda r:r['reclaim_percent']);case=rows[0]['case']
    if not covers_reclaim_range(rows,reclaim_range):return {'status':'FAIL','reason':'Missing measured low/middle/high reclamation coverage'}
    metrics=['slowdown_percent']+(['p95_slowdown_percent'] if case in reference['fig8'] else [])
    flat_throughput = case in reference['fig8'] and near_flat_memcached_throughput(rows,flat_throughput_tolerance_pp)
    for a,b in zip(rows,rows[1:]):
        if b['reclaim_percent']-a['reclaim_percent']<1:return {'status':'FAIL','reason':'Reclaim spacing below 1 percentage point'}
    if rows[-1]['reclaim_percent']-rows[0]['reclaim_percent']<3:return {'status':'FAIL','reason':'Reclaim span below 3 percentage points'}
    curve_rows=([{'reclaim_percent':0,**{k:0 for k in metrics}}]+rows if include_all_local else rows)
    if include_all_local and rows[0]['reclaim_percent']<=0:return {'status':'FAIL','reason':'Reclaimed points must be distinct from all-local origin'}
    if check_trend:
        trend_rows=rows if trend_from_reclaimed_only else curve_rows
        xs=[r['reclaim_percent'] for r in trend_rows];xm=sum(xs)/len(xs)
        for metric in metrics:
            if metric == 'slowdown_percent' and flat_throughput:
                continue
            ys=[r[metric] for r in trend_rows];ym=sum(ys)/len(ys)
            if ys[-1]<=ys[0] or sum((x-xm)*(y-ym) for x,y in zip(xs,ys))<=0:
                return {'status':'FAIL','reason':'No overall increasing trend in '+metric}
    intervals={}
    overlap_mode=reference.get('comparison_domains',{}).get(case)=='per-baseline-overlap'
    for fig,metric in [('fig7','slowdown_percent'),('fig8','p95_slowdown_percent')]:
        if case not in reference[fig]:continue
        points=[(r['reclaim_percent'],r[metric]) for r in curve_rows]
        intervals[fig]={}
        for name,target in reference[fig][case].items():
            lo=max(points[0][0],target[0][0]);hi=min(points[-1][0],target[-1][0])
            if overlap_mode and lo>=hi:return {'status':'FAIL','reason':'No nonzero overlap with '+name+' in '+fig}
            intervals[fig][name]={'compared_domain':[lo,hi],
                                  'reference_domain':[target[0][0],target[-1][0]],'extrapolated':False}
            knots=sorted({lo,hi}|{x for x,_ in points+list(target) if lo<=x<=hi}) if overlap_mode else [x for x,_ in target]
            for x in knots:
                if lo<=x<=hi:
                    difference=interpolate(points,x)-interpolate(target,x)
                    excess=max(excess,difference)
                    origin_equal=include_all_local and x==0 and difference==0
                    if not origin_equal and (difference>reference_tolerance_pp or (reference_tolerance_pp==0 and difference==0)):
                        return {'status':'FAIL','reason':'Curve crosses '+name+' beyond allowed tolerance in '+fig}
    return {'status':'PASS','ordered_trial_ids':[r['trial_id'] for r in rows],
            'reference_comparison_mode':'per-baseline-overlap' if overlap_mode else 'full-point-domain',
            'reference_compared_intervals':intervals,
            'reference_tolerance_pp':reference_tolerance_pp,'max_reference_excess_pp':excess,
            'includes_all_local_origin':include_all_local,'curve_point_count':len(curve_rows),
            'reclaim_range':reclaim_range,
            'flat_throughput_tolerance_pp':flat_throughput_tolerance_pp,
            'near_flat_throughput':bool(flat_throughput),
            'trend_assessment':('near_flat_throughput_with_increasing_p95' if flat_throughput else 'overall_increase') if check_trend else 'deferred_to_repeat_mean'}

def repeated_curve_check(rounds,reference,check_trend=True,reference_tolerance_pp=0,required_repeats=3,include_all_local=False,reclaim_range=None,max_mean_reversal_pp=None,trend_from_reclaimed_only=False,flat_throughput_tolerance_pp=0,mean_reference_tolerance_pp=0):
    """Allow bounded repeat fluctuations; keep the measured repeat mean below targets.

    Min/max ranges describe observed variability, not confidence intervals.
    A small reversal of adjacent means is allowed when repeat ranges overlap.
    """
    if required_repeats not in (2,3) or len(rounds)!=required_repeats:return {'status':'FAIL','reason':f'Need exactly {required_repeats} complete repeats'}
    ordered=[];repeat_checks=[]
    for rows in rounds:
        check=curve_check(rows,reference,check_trend=False,reference_tolerance_pp=reference_tolerance_pp,include_all_local=include_all_local,reclaim_range=reclaim_range)
        if check['status']!='PASS':return {'status':'FAIL','reason':'A repeat failed curve/reference checks','repeat_check':check}
        repeat_checks.append(check)
        ordered.append(sorted(rows,key=lambda r:r['reclaim_percent']))
    if len({len(rows) for rows in ordered})!=1:return {'status':'FAIL','reason':'Repeat point counts differ'}
    for rows in ordered[1:]:
        for a,b in zip(ordered[0],rows):
            if (a['case']!=b['case'] or a['memory_mib']!=b['memory_mib'] or
                    (a.get('parameters') or a.get('parameters_requested'))!=(b.get('parameters') or b.get('parameters_requested'))):
                return {'status':'FAIL','reason':'Configuration or ordering changed between repeats'}
            for key in ('baseline_protocol','tracking_profile','workload_configuration','workload_identity','performance_reference','baseline_tracking_profile','cpu_affinity_profile','normalization_protocol'):
                if a.get(key)!=b.get(key):return {'status':'FAIL','reason':'Repeat '+key+' differs'}
            if a.get('normalization_protocol') and a.get('baseline_report')!=b.get('baseline_report'):
                return {'status':'FAIL','reason':'Fixed normalization baseline differs between repeats'}
            for key, field in [('pre_reclaim_headroom_mib','headroom_mib'),('pre_reclaim_epoch_us','epoch_us')]:
                values=[(r.get('pre_reclaim') or {}).get(field,r.get(key)) for r in (a,b)]
                if values[0]!=values[1]:return {'status':'FAIL','reason':'Repeat '+key+' differs'}
    case=ordered[0][0]['case']
    metrics=['slowdown_percent']+(['p95_slowdown_percent'] if case in reference['fig8'] else [])
    means=[];ranges=[]
    for group in zip(*ordered):
        row=dict(group[0]);row['trial_id']='mean:'+group[0]['trial_id']
        row.update({k:sum(r[k] for r in group)/required_repeats for k in ['reclaim_percent']+metrics})
        row['reference_margin_pp']={}
        try:
            row.update(reference_point(row,reference))
        except ValueError as error:
            row['reference_status']='FAIL'
            row['comparison_error']=str(error)
        means.append(row)
        ranges.append({k:{'min':min(r[k] for r in group),'max':max(r[k] for r in group)} for k in metrics})
    # The near-flat exception must hold in every observation, not only after averaging.
    flat_tolerance=flat_throughput_tolerance_pp if all(near_flat_memcached_throughput(r,flat_throughput_tolerance_pp) for r in rounds) else 0
    strict_check=curve_check(means,reference,check_trend=check_trend,include_all_local=include_all_local,reclaim_range=reclaim_range,trend_from_reclaimed_only=trend_from_reclaimed_only,flat_throughput_tolerance_pp=flat_tolerance)
    check=curve_check(means,reference,check_trend=check_trend,include_all_local=include_all_local,reclaim_range=reclaim_range,trend_from_reclaimed_only=trend_from_reclaimed_only,flat_throughput_tolerance_pp=flat_tolerance,reference_tolerance_pp=mean_reference_tolerance_pp)
    result={'status':check['status'],'trend_criterion':trend_criterion(required_repeats),'mean_curve_check':check,
            'required_repeats':required_repeats,'includes_all_local_origin':include_all_local,
            'max_mean_reversal_pp':max_mean_reversal_pp,
            'flat_throughput_tolerance_pp':flat_throughput_tolerance_pp,
            'mean_reference_tolerance_pp':mean_reference_tolerance_pp,
            'strict_mean_curve_check':strict_check,
            'reference_tolerance_pp':reference_tolerance_pp,'repeat_checks':repeat_checks,
            'mean_points':[{k:r[k] for k in ['trial_id','reclaim_percent','parameters']+metrics if k in r} for r in means],
            'observed_ranges':ranges,'adjacent_mean_reversals':[],
            'scope':f'Arithmetic mean of all {required_repeats} repeats for each frozen configuration; observed min/max are not confidence intervals; raw points are never sorted by slowdown or changed',
            'overall_trend_required':check_trend}
    for i,(a,b) in enumerate(zip(means,means[1:])):
        for metric in metrics:
            if b[metric]<a[metric]:
                overlap=ranges[i+1][metric]['max']>=ranges[i][metric]['min']
                result['adjacent_mean_reversals'].append({'left_index':i,'metric':metric,'drop_pp':a[metric]-b[metric],'observed_ranges_overlap':overlap})
                if metric == 'slowdown_percent' and check.get('near_flat_throughput'):
                    continue
                if check_trend and not overlap:
                    result.update(status='FAIL',reason='Adjacent mean reversal exceeds observed repeat overlap in '+metric)
                if check_trend and max_mean_reversal_pp is not None and a[metric]-b[metric]>max_mean_reversal_pp:
                    result.update(status='FAIL',reason='Adjacent mean reversal exceeds allowed small fluctuation in '+metric)
    return result

def find_curve(rows,reference,excluded=(),required_targets=None,check_trend=True,include_all_local=False,reclaim_range=None,trend_from_reclaimed_only=False,reference_tolerance_pp=0,flat_throughput_tolerance_pp=0):
    valid=[r for r in rows if reference_acceptable(r,reference_tolerance_pp)]
    for group in itertools.combinations(valid,3):
        if required_targets and {r.get('coverage_target_percent') for r in group}!=set(required_targets):continue
        if any(set(r['trial_id'] for r in group)==set(ids) for ids in excluded):continue
        result=curve_check(group,reference,check_trend=check_trend,include_all_local=include_all_local,reclaim_range=reclaim_range,trend_from_reclaimed_only=trend_from_reclaimed_only,reference_tolerance_pp=reference_tolerance_pp,flat_throughput_tolerance_pp=flat_throughput_tolerance_pp)
        if result['status']=='PASS':return sorted(group,key=lambda r:r['reclaim_percent'])
    return None
