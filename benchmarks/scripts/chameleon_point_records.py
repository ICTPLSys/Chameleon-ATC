"""Export reproducible point configurations without hiding failed repeats."""
import csv
import io
import json
import os
import shlex
from datetime import datetime, timezone
from pathlib import Path


def configuration(trial):
    requested=trial.get('parameters_requested') or {}
    actual=trial.get('parameters') or {}
    tracking=trial.get('tracking_profile') or {}
    floor=actual.get('minimum_local_bytes')
    return {
        'vm_memory_mib':trial.get('memory_mib'),
        'cpu_affinity_profile':trial.get('cpu_affinity_profile'),
        'minimum_local_mib':floor/(2**20) if floor is not None else requested.get('minimum_local_mib'),
        'psi_ppm':actual.get('psi_ppm',requested.get('psi_ppm')),
        'epoch_us':actual.get('epoch_us',requested.get('epoch_us')),
        'cold_folios':actual.get('cold_folios',requested.get('cold_folios')),
        'sample_period':tracking.get('sampling',requested.get('sample_period')),
        'cooling_samples':tracking.get('cooling',requested.get('cooling_samples')),
        'hhh_interval_ms':tracking.get('hhh_interval_ms',requested.get('hhh_interval_ms')),
        'free_pages':actual.get('free_pages'),
        'pre_reclaim_headroom_mib':(trial.get('pre_reclaim') or {}).get('headroom_mib',trial.get('pre_reclaim_headroom_mib')),
        'pre_reclaim_epoch_us':(trial.get('pre_reclaim') or {}).get('epoch_us',trial.get('pre_reclaim_epoch_us',requested.get('epoch_us'))),
    }


def point_pass(trial,tolerance):
    if trial.get('status')!='PASS' or trial.get('comparison_error'):return False
    if trial.get('reference_status')=='PASS':return True
    margins=[v for fig in trial.get('reference_margin_pp',{}).values() for v in fig.values()]
    return bool(margins) and tolerance>0 and all(v>=-tolerance for v in margins)


def replay(command,name):
    args=list(command or [])
    if '--name' in args:args[args.index('--name')+1]=name
    if (any(Path(v).name=='run-sized-chameleon-apps.py' for v in args) and
        '--cpu-pinning' not in args and '--no-cpu-pinning' not in args):
        args.append('--no-cpu-pinning')  # Preserve historical affinity semantics.
    return shlex.join(args) if args else None


def collect(data):
    tolerance=data.get('plan',{}).get('reference_tolerance_pp',0)
    result=[];accepted_curves=[]
    for case,entry in data['cases'].items():
        groups={}
        accepted_ids={tag for rep in entry.get('accepted',{}).get('validation',{}).get('rounds',[])
                      for tag in rep['trial_ids']} if entry.get('status')=='ACCEPTED' else set()
        for tag,trial in entry['trials'].items():
            if trial.get('mode')!='chameleon' or trial.get('status') not in ('PASS','FAIL','ERROR','TIMEOUT'):continue
            config=configuration(trial)
            identity={k:trial.get(k) for k in ('baseline_protocol','baseline_tracking_profile','performance_reference','workload_identity','workload_configuration')}
            key=json.dumps([config,identity],sort_keys=True)
            group=groups.setdefault(key,{'configuration_id':case+'/'+tag,'application':case,
                'configuration':config,**identity,'measurements':[],
                'full_curve_status':'NOT_YET_ACCEPTED','replay_command':replay(trial.get('command'),'CHOOSE_UNIQUE_RUN_NAME')})
            # Validation may reference a search trial; do not count the same report twice.
            if trial.get('report') and any(r.get('report')==trial['report'] for r in group['measurements']):continue
            observation={k:trial.get(k) for k in ('status','reference_status','reclaim_percent','slowdown_percent',
                'p95_slowdown_percent','p99_slowdown_percent','performance','reference_margin_pp','reference_point_coverage','reference_comparison_mode','counters',
                'report','baseline_report','matched_tracking_baseline_report','matched_tracking_baseline_scope',
                'tracking_only_overhead_percent','reclaim_over_matched_tracking_percent',
                'capacity_context','started_utc','finished_utc')}
            observation.update(trial_id=tag,point_pass=point_pass(trial,tolerance),
                               in_current_accepted_curve=tag in accepted_ids)
            group['measurements'].append(observation)
            if tag in accepted_ids:group['full_curve_status']='ACCEPTED_UNDER_CURRENT_CRITERION'
            baseline=next((r for r in entry['trials'].values() if r.get('report') and r.get('report')==trial.get('baseline_report')),None)
            if baseline:
                observation['baseline_replay_command']=replay(baseline.get('command'),'CHOOSE_UNIQUE_ALL_LOCAL_NAME')
        for group in groups.values():
            count=sum(r['point_pass'] for r in group['measurements'])
            if not count:continue
            group.update(point_pass_runs=count,completed_runs=len(group['measurements']))
            result.append(group)
        if entry.get('status')=='ACCEPTED':
            accepted=entry['accepted'];validation=accepted['validation']
            accepted_curves.append({'application':case,'vm_memory_mib':accepted.get('memory_mib',entry.get('memory_mib')),
                'candidate_ids':validation.get('candidate_ids',[]),
                'rounds':validation.get('rounds',[]),'aggregate_check':validation.get('aggregate_check'),
                'measurement_basis':validation.get('measurement_basis'),
                'scope':'Only the explicitly listed rounds support this acceptance; other same-configuration observations remain available separately.'})
    return {'generated_utc':datetime.now(timezone.utc).isoformat(),
            'scope':'Configurations with at least one qualifying point. All completed repeats, including failures, are retained; point qualification is not full-curve acceptance.',
            'reference_kind':'User-supplied estimated target curves, not measured baseline results',
            'plan_version':data.get('plan',{}).get('version'),
            'remote_pool_mib':data.get('plan',{}).get('remote_pool_mib'),
            'units':{'memory':'MiB','psi':'ppm (10000 ppm = 1%)','epoch':'microseconds','hhh_interval':'milliseconds','sample_period':'events'},
            'reference_tolerance_pp':tolerance,'required_measurements':data.get('plan',{}).get('validation_repeats',3),
            'full_curve_rule':{'reclaim_range_required':data.get('plan',{}).get('version',0)>=8,
                'page_out_required':False,'mean_reversal_limit_pp':data.get('plan',{}).get('max_mean_reversal_pp')},
            'accepted_curves':accepted_curves,'configurations':result}


def write(path,content):
    path=Path(path);tmp=path.with_name(path.name+'.'+str(os.getpid())+'.tmp')
    tmp.write_text(content);tmp.replace(path)


def accepted_configurations(data):
    """Freeze only the low/middle/high points of currently accepted curves."""
    applications={}
    for case,entry in data['cases'].items():
        accepted=entry.get('accepted') or {};validation=accepted.get('validation') or {}
        aggregate=validation.get('aggregate_check') or {}
        if entry.get('status')!='ACCEPTED' or validation.get('status')!='PASS' or aggregate.get('status')!='PASS':continue
        means=aggregate.get('mean_points',[]);ids=validation['candidate_ids'];rounds=validation['rounds']
        if len(ids)<3 or len(means)!=len(ids):raise ValueError('Incomplete accepted curve: '+case)
        points=[];baselines=[]
        for rep in rounds:
            baseline=entry['trials'][rep['baseline']]
            baselines.append({'repeat':rep['repeat'],'trial_id':rep['baseline'],
                'report':baseline['report'],'performance':baseline.get('performance'),
                'raw_reclaim_percent':baseline.get('reclaim_percent'),
                'tracking_profile':baseline.get('tracking_profile'),'checks':baseline.get('checks'),
                'cpu_affinity_profile':baseline.get('cpu_affinity_profile'),
                'replay_command':replay(baseline.get('command'),'CHOOSE_UNIQUE_ALL_LOCAL_NAME')})
        for i,(candidate,mean) in enumerate(zip(ids,means)):
            trial=entry['trials'][candidate];role='low' if i==0 else 'high' if i==len(ids)-1 else 'middle' if len(ids)==3 else 'middle_'+str(i)
            measurements=[]
            for rep in rounds:
                tag=rep['trial_ids'][i];row=entry['trials'][tag]
                if configuration(row)!=configuration(trial):raise ValueError('Frozen parameter mismatch: '+case+'/'+tag)
                measurements.append({'repeat':rep['repeat'],'trial_id':tag,
                    **{k:row.get(k) for k in ('report','baseline_report','matched_tracking_baseline_report',
                        'reclaim_percent','slowdown_percent','p95_slowdown_percent','p99_slowdown_percent',
                        'reference_status','reference_margin_pp','reference_point_coverage','reference_comparison_mode','performance','counters',
                        'normalization_protocol','per_repeat_normalization','fixed_normalization_baseline_id','per_repeat_baseline_id')},
                    'verification_reuse':rep.get('verification_reuse')})
            if len({m['report'] for m in measurements})!=len(rounds):raise ValueError('Duplicate observation in accepted point: '+case+'/'+candidate)
            points.append({'role':role,'candidate_id':candidate,'configuration':configuration(trial),
                'mean':{k:mean.get(k) for k in ('reclaim_percent','slowdown_percent','p95_slowdown_percent')},
                'observed_ranges':aggregate.get('observed_ranges',[])[i],
                'measurements':measurements,'replay_command':replay(trial.get('command'),'CHOOSE_UNIQUE_RUN_NAME')})
        first=entry['trials'][ids[0]];profile=first.get('baseline_tracking_profile') or {}
        applications[case]={'vm_memory_mib':accepted['memory_mib'],'status':'ACCEPTED',
            'throughput_interpretation':('User-approved near-flat inverse-goodput curve at fixed offered load; not a maximum service-capacity measurement. P95 trend remains required.' if aggregate.get('mean_curve_check',{}).get('near_flat_throughput') else None),
            'cpu_affinity_profile':first.get('cpu_affinity_profile'),
            'workload_identity':first.get('workload_identity'),'workload_configuration':first.get('workload_configuration'),
            'all_local':{'policy_enabled':False,'pebs_enabled':True,'hhh_enabled':True,
                'normalization_protocol':first.get('normalization_protocol'),
                'fixed_main_baseline_report':first.get('baseline_report') if first.get('normalization_protocol') else None,
                'sample_period':profile.get('sampling'),'cooling_samples':profile.get('cooling'),
                'hhh_interval_ms':profile.get('hhh_interval_ms'),'normalized_point':{'reclaim_percent':0,'slowdown_percent':0},
                'measurements':baselines},'points':points,
            'qualification':{'aggregate_check':aggregate,'repeat_checks':[r['check'] for r in rounds],
                'measurement_basis':validation.get('measurement_basis'),'verification_reuse':validation.get('verification_reuse')}}
    return {'schema_version':1,'generated_utc':datetime.now(timezone.utc).isoformat(),
        'scope':'Only currently accepted full low/middle/high curves; candidates and failed configurations are in the separate experiment ledger.',
        'reference_kind':'User-supplied estimated target curves; not measured-baseline SOTA evidence',
        'units':{'memory':'MiB','psi':'ppm (10000 ppm = 1%)','epoch':'microseconds','hhh_interval':'milliseconds','sample_period':'events'},
        'remote_pool_mib':data.get('plan',{}).get('remote_pool_mib'),
        'applications':applications}


def export_accepted(data,directory,config_path,progress_path):
    if data.get('plan',{}).get('version',0)<8:return None
    frozen=accepted_configurations(data);directory=Path(directory)
    frozen['source_search']=str((directory/'search.json').resolve())
    frozen['user_selected_configurations']={case:entry['selected_curve']
        for case,entry in data['cases'].items() if entry.get('selected_curve')}
    content=json.dumps(frozen,indent=2)+'\n'
    write(directory/'qualified-curve-configs.json',content)
    write(config_path,content)
    lines=['# 已通过验收的低、中、高点配置','',f"更新于 {frozen['generated_utc']}。",'',
        f"[可复用 JSON 配置]({Path(config_path).resolve()}) · [全部候选和失败测量](chameleon-qualified-points.md)",'',
        '仅收录完整曲线已通过当前验收的应用。每个应用固定一个 VM 容量，每个非零点有两次实测；复用观测保留原始 trial ID。参考为指定绘图脚本的估计曲线。','',
        'all-local 开启 PEBS 和 HHH，关闭回收；(0%,0%) 为归一化原点，原始 RSS 另存。','']
    for case,app in frozen['applications'].items():
        baseline=app['all_local']
        common=[]
        for key,label in [('cooling_samples','cooling samples'),('free_pages','free 页/轮'),('pre_reclaim_headroom_mib','预回收余量 MiB'),('pre_reclaim_epoch_us','预回收周期 µs')]:
            values=[p['configuration'][key] for p in app['points']]
            common.append(label+'='+str(values[0]) if len(set(values))==1 else label+'（低→高）='+str(values))
        lines += [f"## {case}：VM {app['vm_memory_mib']} MiB",'',
            ('CPU 绑定：'+json.dumps(app['cpu_affinity_profile'],ensure_ascii=False) if app['cpu_affinity_profile'] else
             'CPU 绑定：历史实验仅限制 Host NUMA 节点；尚无逐 vCPU / Guest 显式绑核验收。后续绑核结果使用独立基准，不与这些测量混合。'),'',
            '回收点配置：'+'；'.join(common)+'。','',
            '| 点 | 本地下限 MiB | PSI ppm | 控制周期 ms | 冷 folio/轮 | PEBS 周期 | HHH 秒 | 平均回收 % | 平均 slowdown % | 平均 P95 slowdown % |',
            '|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|',
            f"| all-local | — | — | — | 回收关闭 | {baseline['sample_period']} | {baseline['hhh_interval_ms']/1000:g} | 0 | 0 | {'0' if case in ('memcached','cassandra') else '—'} |"]
        for point in app['points']:
            c=point['configuration'];mean=point['mean'];label={'low':'低','middle':'中','high':'高'}.get(point['role'],point['role'])
            p95='—' if mean['p95_slowdown_percent'] is None else f"{mean['p95_slowdown_percent']:.4f}"
            lines.append(f"| {label} ({point['candidate_id']}) | {c['minimum_local_mib']:g} | {c['psi_ppm']} | {c['epoch_us']/1000:g} | {c['cold_folios']} | {c['sample_period']} | {c['hhh_interval_ms']/1000:g} | {mean['reclaim_percent']:.4f} | {mean['slowdown_percent']:.4f} | {p95} |")
        lines += ['','两次测量来源（回收率 / slowdown）：','']
        for point in app['points']:
            values=[f"[{m['trial_id']}]({m['report']})：{m['reclaim_percent']:.4f}% / {m['slowdown_percent']:.4f}%" for m in point['measurements']]
            lines += [f"- {point['role']}："+'；'.join(values)]
        lines += ['','all-local 基准：'+ '；'.join(f"[{b['trial_id']}]({b['report']})" for b in baseline['measurements'])+'。',
                  '验收包含两次参考比较及均值趋势；单次允许轻微波动，原始曲线不改写。','']
        if baseline.get('fixed_main_baseline_report'):
            lines += [f"主分母固定使用[同容量首次 tracked all-local]({baseline['fixed_main_baseline_report']})；各轮新 all-local 和逐轮归一化结果在 JSON 的 per_repeat_normalization 中保留。",'']
        if app.get('throughput_interpretation'):
            lines += ['Memcached 使用固定请求速率，按用户确认允许吞吐近乎水平；P95 仍需上升。此吞吐曲线不表示最大服务能力未下降。','']
        aggregate=app['qualification']['aggregate_check']
        if aggregate.get('strict_mean_curve_check',{}).get('status')=='FAIL' and aggregate.get('mean_reference_tolerance_pp',0):
            lines += [f"均值连线最大局部越线 {aggregate['mean_curve_check']['max_reference_excess_pp']:.4f} 个百分点，在允许的 {aggregate['mean_reference_tolerance_pp']:g} 个百分点内；严格零越线判定仍为 FAIL 并保留。",'']
        if app['qualification'].get('verification_reuse'):
            lines += ['本组合复用了同配置、同基准组的中高点观测；详见 JSON 的 verification_reuse，每次观测只计一次。','']
    if not frozen['applications']:lines += ['尚无通过当前完整曲线验收的配置。','']
    if frozen['user_selected_configurations']:
        lines += ['## 用户选定的配置','',
            '以下配置按用户要求保存；观测次数与原始检查结果见各自记录，不计入上面的两次完整曲线验收。','']
        for case,selection in frozen['user_selected_configurations'].items():
            lines += [f"- {case}：[配置与原始结果]({selection['configuration_file']})；点：{', '.join(selection['candidate_ids'])}；状态：{selection['status']}。"]
        lines += ['']
    lines += ['JSON 保存每个点的完整参数、数据规模、复跑命令模板和两次结果。重新运行时替换命令中的唯一名称，并使用配套新 all-local 结果计算 slowdown。','']
    write(progress_path,'\n'.join(lines))
    return frozen


def export(data,directory,progress_path):
    ledger=collect(data);directory=Path(directory)
    write(directory/'qualified-point-configs.json',json.dumps(ledger,indent=2)+'\n')
    flat=[]
    for group in ledger['configurations']:
        for row in group['measurements']:
            flat.append({'configuration_id':group['configuration_id'],'application':group['application'],
                **group['configuration'],'full_curve_status':group['full_curve_status'],
                **{k:row.get(k) for k in ('trial_id','point_pass','in_current_accepted_curve','status','reference_status','reclaim_percent','slowdown_percent','p95_slowdown_percent','report','baseline_report','matched_tracking_baseline_report')},
                'replay_command':group['replay_command'],'baseline_replay_command':row.get('baseline_replay_command')})
    stream=io.StringIO()
    if flat:
        writer=csv.DictWriter(stream,fieldnames=list(flat[0]));writer.writeheader();writer.writerows(flat)
    write(directory/'qualified-point-configs.csv',stream.getvalue())
    lines=['# Chameleon 合格点参数清单','',
        f"更新于 {ledger['generated_utc']}。",'',
        '自动随实验记录更新。点通过次数与完整低—中—高曲线验收分开记录；同参数失败测量不会隐藏。',
        f"[完整配置与每次观测]({directory.resolve()}/qualified-point-configs.json) · [CSV]({directory.resolve()}/qualified-point-configs.csv)",'',
        'all-local保持PEBS+HHH开启，主曲线固定跟踪基准为65536 events / 15000 ms。零点为相对自身(0%,0%)，原始RSS另存。换入换出不是门槛。','',
        f"点检查允许最多 {ledger['reference_tolerance_pp']:g} 个百分点越线，JSON保留严格参考判定；参考为用户提供的绘图脚本估计值。PSI 10000 ppm = 1%。通过次数不代表完整曲线通过。",'',
        '## 完成当前曲线验收的组合','']
    for curve in ledger['accepted_curves']:
        lines += [f"### {curve['application']}：VM {curve['vm_memory_mib']} MiB",'',
                  '以下只汇总本次验收采用的两次测量，其他同配置观测仍列在后面。all-local 原点为 (0%, 0%)。','',
                  '| 候选 | 平均回收 % | 平均 slowdown % | slowdown 实测范围 % | 平均 P95 slowdown % | 原始 trial |',
                  '|---|---:|---:|---|---:|---|']
        aggregate=curve.get('aggregate_check') or {}
        for i,mean in enumerate(aggregate.get('mean_points',[])):
            observations=[rep['trial_ids'][i] for rep in curve['rounds']]
            bounds=aggregate['observed_ranges'][i]['slowdown_percent']
            p95=f"{mean['p95_slowdown_percent']:.4f}" if 'p95_slowdown_percent' in mean else '—'
            lines.append(f"| {mean['trial_id'].removeprefix('mean:')} | {mean['reclaim_percent']:.4f} | {mean['slowdown_percent']:.4f} | {bounds['min']:.4f}–{bounds['max']:.4f} | {p95} | {', '.join(observations)} |")
        lines += ['','每轮原始曲线和均值分别保留；均值递增不代表每次测量均严格单调。','']
    if not ledger['accepted_curves']:lines += ['尚无完成当前完整范围验收的组合。','']
    lines += ['## 所有合格点参数','',
        '| 配置 | VM MiB | 本地下限 MiB | PSI ppm | 周期 µs | 冷folio/轮 | PEBS周期 | cooling | HHH ms | free页/轮 | 预回收余量 MiB | 点通过/已测 | 当前整条曲线 |',
        '|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|']
    keys=['vm_memory_mib','minimum_local_mib','psi_ppm','epoch_us','cold_folios','sample_period','cooling_samples','hhh_interval_ms','free_pages','pre_reclaim_headroom_mib','pre_reclaim_epoch_us']
    for group in ledger['configurations']:
        values=[str(group['configuration'].get(k)) for k in keys]
        status='通过' if group['full_curve_status'].startswith('ACCEPTED') else '未完成当前验收'
        lines.append('| '+group['configuration_id']+' | '+' | '.join(values)+f" | {group['point_pass_runs']}/{group['completed_runs']} | {status} |")
    if not ledger['configurations']:lines+=['','尚无合格点；不填入预计参数。']
    lines+=['','## 每次测量','', '| 配置 / trial | 回收 % | slowdown % | P95 slowdown % | 点检查 | 当前曲线采用 | 原始报告 |','|---|---:|---:|---:|---|---|---|']
    def number(x):return '—' if x is None else f'{x:.4f}'
    for group in ledger['configurations']:
        for row in group['measurements']:
            report=f"[report]({row['report']})" if row.get('report') else '—'
            lines.append('| '+group['configuration_id']+' / '+row['trial_id']+' | '+' | '.join(number(row[k]) for k in ('reclaim_percent','slowdown_percent','p95_slowdown_percent'))+f" | {'PASS' if row['point_pass'] else 'FAIL'} | {'是' if row['in_current_accepted_curve'] else '否'} | {report} |")
    lines+=['','JSON/CSV包含原始运行命令模板和配套all-local命令模板；复跑前替换唯一运行名称。命令只用于复跑该配置，新的slowdown应使用配套新all-local测量。','']
    write(progress_path,'\n'.join(lines))
    return ledger
