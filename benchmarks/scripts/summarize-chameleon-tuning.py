#!/usr/bin/env python3
"""Publish the current search ledger, including every failed candidate."""
import argparse,csv,json
from pathlib import Path
import chameleon_point_records
ROOT=Path(__file__).resolve().parents[2]
def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--directory',type=Path,default=ROOT/'benchmarks/results/chameleon/tuning-fig78-tracked-v4');a=p.parse_args()
 data=json.loads((a.directory/'search.json').read_text());lines=['# Chameleon 图 7 / 图 8 调参进度','',f"队列状态：`{data['status']}`。",'',
 '参考目标采用用户确认的绘图脚本估计值；实测图 8 使用 P95。原始失败和未达标点均保留。',
 ('新协议：[README-tuning-tracked-v4.md](../benchmarks/README-tuning-tracked-v4.md)。all-local 与 Chameleon 均开启 PEBS+HHH。' if data.get('protocol')=='tracked-all-local-pre-reclaim-v4' else '新协议：[README-tuning-feedback-v3.md](../benchmarks/README-tuning-feedback-v3.md)。旧结果：[第二轮停止记录](chameleon-tuning-fig78-v2-stopped.md)。' if data.get('protocol')=='feedback-pre-reclaim-v3' else '新协议：[README-tuning-capacity-v2.md](../benchmarks/README-tuning-capacity-v2.md)。旧搜索：[已停止记录](chameleon-tuning-fig78-v1-stopped.md)。' if data.get('protocol')=='capacity-v2' else '完整协议见 [README-tuning-fig78.md](../benchmarks/README-tuning-fig78.md)。'),'',
 '| 应用 | VM MiB | 本轮已完成 / 可用次数 | 低于两个参考的候选点 | 三轮曲线验收 |','|---|---:|---:|---:|---|']
 repeats=data.get('plan',{}).get('validation_repeats',data.get('validation_repeats',3))
 chameleon_point_records.export(data,a.directory,ROOT/'progress/chameleon-qualified-points.md')
 chameleon_point_records.export_accepted(data,a.directory,ROOT/'benchmarks/config/chameleon-qualified-curves.json',ROOT/'progress/chameleon-qualified-curves.md')
 lines[2:2]=['已验收的低/中/高配置：[冻结参数表](chameleon-qualified-curves.md) · [可复用JSON](../benchmarks/config/chameleon-qualified-curves.json)。','']
 lines[2:2]=['合格点与可复跑参数：[参数清单](chameleon-qualified-points.md)。','']
 lines=[line.replace('三轮曲线验收',f'{repeats}轮曲线验收') for line in lines]
 accepted={}
 full_range=data.get('plan',{}).get('version',0)>=8
 tracking_search=data.get('plan',{}).get('strategy')=='tracking_range_coverage'
 coverage=data.get('plan',{}).get('strategy') in ('low_range_coverage','tracking_range_coverage')
 if full_range:
  lines[2:2]=['当前阶段：[固定VM的低—中—高回收率搜索](../benchmarks/README-tuning-range-v8.md)。历史高区通过记录单独保留，不算当前完整范围通过；换入换出只作诊断。','']
 elif coverage:
  lines[2:2]=[('当前阶段：高回收率下PEBS/HHH频率调参，协议见[采样调参说明](../benchmarks/README-tuning-tracking-v6.md)。' if tracking_search else '当前阶段：补测低/中回收率。')+'此前高区记录：[高区进度](chameleon-tuning-highrange-v4.md)。低区与高区尚未作为同一组冻结配置联合复测，不提前宣称完整范围通过。','']
 if data.get('plan',{}).get('include_all_local_anchor'):
  lines[2:2]=['当前采用[两轮四点快速搜索](../benchmarks/README-tuning-fast-v7.md)：每条曲线含all-local (0%,0%)及至少三个实测回收点。每应用固定一个VM容量，基准保持PEBS和HHH开启。','']
 for case in ['spark-kmeans','pvc','xsbench','memcached','cassandra','liblinear','graphchi','graph500']:
  entry=data['cases'].get(case,{})
  trials=entry.get('trials',{});finished=[r for tag,r in trials.items() if tag.startswith('d') and r['status']!='RUNNING']
  passed=[r for r in finished if r.get('reference_status')=='PASS']
  allowance=data.get('plan',{}).get('applications',{}).get(case,{}).get('remaining_candidates',16)
  display_status=entry.get('status','NOT_STARTED')
  if display_status=='STOPPED_BY_USER' and data['status']=='RUNNING' and case in data.get('requested_cases',[]):display_status='QUEUED_FOR_RESUME'
  lines.append(f"| {case} | {entry.get('memory_mib','—')} | {len(finished)} / {allowance} | {len(passed)} | {display_status} |")
  if entry.get('status')=='ACCEPTED':accepted[case]=entry['accepted']
 if data.get('protocol') in ['feedback-pre-reclaim-v3','tracked-all-local-pre-reclaim-v4']:
  lines+=['',(f"补点阶段每应用最多16组新候选、每个目标区间最多5次；VM固定，预回收到本地下限+{data['plan']['pre_reclaim_headroom_mib']}MiB后启动应用。按实测回收率确认区间覆盖，不用目标值代替测量。" if coverage else '先预回收到目标本地容量再启动应用；预回收不计入运行窗口。Spark 第二轮已用 8 组，本轮最多再用 8 组，其他应用最多 16 组。'),f'监控：[live-monitor.json](../benchmarks/results/chameleon/{a.directory.name}/live-monitor.json)。每轮结束后的诊断和下一组调整原因保存在 search.json。']
 if data.get('protocol')=='tracked-all-local-pre-reclaim-v4':
  if coverage and data.get('plan',{}).get('coverage_tolerance_pp',0):
   lines+=['',f"搜索区间边缘允许 {data['plan']['coverage_tolerance_pp']:g} 个百分点回收率波动；实际点间距与跨度仍独立检查，原始回收率不改写。"]
  lines+=['',('主slowdown固定相对PEBS=65536、HHH=15000ms、cooling=131072的all-local；候选采样参数逐点记录，并补测同采样配置的all-local分解跟踪与回收开销。' if data.get('plan',{}).get('performance_reference')=='fixed-tracking-with-matched-diagnostic-v1' else '固定 PEBS=65536 events、HHH=15000ms、cooling=131072 samples；all-local仅关闭回收。slowdown相对同容量、相同跟踪参数的新all-local。旧的关闭跟踪对照不混用。')]
  lines+=['','人工观察及反馈规则调整：[现场观察记录](chameleon-tuning-monitoring.md)。']
  if data.get('mechanism_criterion')=='real-cold-rdma-no-volume-minimum-v2':
   lines+=['','按用户要求放宽冷页门槛：允许主要回收空闲页，只要求非零冷页驻留及真实RDMA写入/读回；不要求冷页占比逐点增加。同一曲线仍固定VM并包含策略参数变化，功能检查及三轮复测保持。',f'旧、新判定记录：[规则重评](../benchmarks/results/chameleon/{a.directory.name}/reassessment-real-cold-rdma-v2.json)。']
  if data.get('mechanism_criterion')=='measured-reclaim-free-only-allowed-v3':
   lines+=['','允许纯空闲页回收点进入曲线：达到本地下限后冷页预算为零属于正常策略行为，不要求每个点产生冷页RDMA读写。保留并披露零计数；RDMA环境就绪不等于该点实际使用远端内存。同一曲线仍固定VM并包含策略参数变化，功能检查、参考比较及三轮复测保持。']
  if data.get('trend_criterion') in ('three-repeat-mean-with-observed-variation-v1','two-repeat-mean-with-observed-variation-v1'):
   mean_rule=(f"均值及均值连线允许最多{data['plan']['mean_reference_tolerance_pp']:g}个百分点轻微越线" if data.get('plan',{}).get('mean_reference_tolerance_pp',0) else '三轮均值及均值连线仍须严格低于两个参考')
   rule=(f"单轮允许最多{data['plan']['reference_tolerance_pp']:g}个百分点轻微越线，{mean_rule}；P95同样检查。严格参考判定和越线幅度保留。" if data.get('plan',{}).get('reference_tolerance_pp',0) else '每轮及连线均须低于两个参考，P95同样检查。')
   trend_note=(f"允许运行波动：趋势看均值；局部均值回落需要实测范围重叠，且不超过{data['plan']['max_mean_reversal_pp']:g}个百分点。三个非零回收点整体须上升，不能仅靠加入原点制造上升趋势。" if full_range else '允许运行波动：趋势看三轮均值；局部均值回落在三次实测范围重叠时允许，均值整体须上升。')
   lines+=['',('低区补点不单独要求slowdown上升，水平段及波动均保留。'+rule+'这里只验证低区覆盖与重复性，完整低到高趋势须联合复测。' if coverage and not tracking_search else trend_note+rule+'原始点不改写。')]
 lines=[line.replace('三轮',f'{repeats}轮').replace('三次',f'{repeats}次') for line in lines]
 if data.get('plan',{}).get('count_initial_measurement'):
  lines+=['','两次测量计数：首次完整实测加一次冻结配置复测；保留初测来源，不称为两次额外独立确认。']
 if data.get('plan',{}).get('applications',{}).get('memcached',{}).get('flat_throughput_tolerance_pp'):
  tol=data['plan']['applications']['memcached']['flat_throughput_tolerance_pp']
  lines+=['',f'用户确认的Memcached例外：固定请求速率下，吞吐slowdown允许近乎水平；每次观测的绝对值和点间跨度均不超过{tol:g}个百分点。P95仍须随回收率上升，两图参考比较、两次观测及功能检查保持。水平吞吐不能解释为最大服务能力没有下降。']
 if data.get('plan',{}).get('normalization_protocol')=='fixed-first-capacity-all-local-v1':
  lines+=['','归一化口径更新：当前后续试验以同应用/同容量/同绑核的首次 tracked all-local 为固定主分母；新测每轮 all-local 保留作诊断，逐轮归一化位于 per_repeat_normalization。旧已验收曲线保留其原口径，见逐点记录。']
 overlap_cases=[case for case,settings in data.get('plan',{}).get('applications',{}).items() if settings.get('reference_comparison_mode')=='per-baseline-overlap']
 if overlap_cases:
  lines+=['','用户确认的参考范围口径（'+', '.join(overlap_cases)+'）：分别在与各baseline重叠的回收率范围内检查完整连线，包含截断边界和双方折点；域外只展示实测点，不外推baseline。逐点coverage与曲线compared_intervals保留实际比较范围，不能声称域外优势。']
 if coverage and not data.get('plan',{}).get('require_coverage_bands',True):
  lines+=['','搜索目标区间仅辅助选点，不作为硬性验收门槛。每条曲线至少含all-local零点与三个实测回收点，检查原点起的完整连线。']
  if full_range:lines+=['','当前另外检查实测低、中、高范围覆盖；精确目标值不必命中，但不能用三个高回收点代替低中高。各应用范围见计划中的 reclaim_range。']
 lines+=['','SPEC gcc：缺少可运行安装，未测试。','',
 'Memcached 与 Cassandra 两图共享同一 trial 的参数和回收率；P95 与吞吐/运行时间均来自该 trial。',
 '每次新启动 Guest，同容量 all-local 对照；回收率不截断负值。仅完整通过的运行进入曲线比较。',
 f'有候选曲线时冻结三个回收点，完成{repeats}次合格测量后写入 accepted-parameters.json；新协议加上带原始报告的all-local (0%,0%)。',
 '当 `applications` 为空时，表示尚未找到符合要求的组合，不使用估计的 Chameleon 点补齐。','',
 '## 每次候选参数与结果','',
 '| 应用 / 候选 | VM MiB | PSI ppm | 周期 µs | 冷 folio/轮 | 本地下限 MiB | 预回收余量 MiB | 回收 % | slowdown % | P95 slowdown % | 运行 / 参考比较 |',
 '|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|']
 def f(v):return '—' if v is None else f'{v:.3f}'
 for case,entry in data['cases'].items():
  for tag,r in entry['trials'].items():
   if not tag.startswith('d'):continue
   c=r.get('parameters_requested') or {}
   headroom=(r.get('pre_reclaim') or {}).get('headroom_mib',r.get('pre_reclaim_headroom_mib','—'))
   lines.append(f"| {case}/{tag} | {r.get('memory_mib',entry['memory_mib'])} | {c.get('psi_ppm','—')} | {c.get('epoch_us','—')} | {c.get('cold_folios','—')} | {c.get('minimum_local_mib','—')} | {headroom} | {f(r.get('reclaim_percent'))} | {f(r.get('slowdown_percent'))} | {f(r.get('p95_slowdown_percent'))} | {r['status']} / {r.get('reference_status','—')} |")
 lines+=['','## 冻结配置复测','']
 for case,entry in data['cases'].items():
  for tag,r in entry['trials'].items():
   if not tag.startswith('d') or r.get('status')!='PASS':continue
   rdma=r.get('counters',{}).get('rdma',{})
   lines.append(f"{case}/{tag}：平均空闲页回收 {f(r.get('free_mean_mib'))} MiB，冷页峰值 {f(r.get('cold_peak_mib'))} MiB；RDMA 写入/读回 {rdma.get('write_bytes','—')} / {rdma.get('read_bytes','—')} 字节。")
   if r.get('tracking_profile'):
    t=r['tracking_profile'];lines.append(f"PEBS周期 {t.get('sampling')}，HHH周期 {t.get('hhh_interval_ms')} ms；跟踪配置额外开销 {f(r.get('tracking_only_overhead_percent'))}%，相对匹配跟踪配置的回收开销 {f(r.get('reclaim_over_matched_tracking_percent'))}%。")
 for case,entry in data['cases'].items():
  for i,v in enumerate(entry.get('validation_sets',[]),1):
   lines+=[f"{case}/v{i}：`{v['status']}`，冻结配置 {', '.join(v['candidate_ids'])}。",'',
           '| 轮次 | 检查 | 各点（回收% / slowdown%） | 说明 |','|---|---|---|---|']
   for round_ in v['rounds']:
    rows=[entry['trials'][t] for t in round_['trial_ids']]
    values='；'.join(f"{f(r.get('reclaim_percent'))} / {f(r.get('slowdown_percent'))}"+(f"（P95 {f(r['p95_slowdown_percent'])}%）" if 'p95_slowdown_percent' in r else '') for r in rows)
    check=round_['check'];note=check.get('reason') or check.get('trend_assessment','')
    if check.get('max_reference_excess_pp',0)>0:note+=f"；最大越线 {check['max_reference_excess_pp']:.3f} 个百分点（容差 {check.get('reference_tolerance_pp',0):g}）"
    lines.append(f"| {round_['repeat']} | {check['status']} | {values} | {note} |")
   count=v.get('required_repeats',v.get('planned_repeats',repeats))
   aggregate=v.get('aggregate_check')
   if v.get('early_stop_reason'):lines+=['',f"提前结束：完成 {len(v['rounds'])}/{count} 轮；{v['early_stop_reason']}"]
   label=f'{count}轮低区覆盖与参考比较' if coverage and not tracking_search else f'{count}轮均值趋势'
   lines+=['',f"{label}：{aggregate['status']}。" if aggregate else f'{label}：尚未完成全部复测。','']
 lines+=['',f'完整记录：[search.json](../benchmarks/results/chameleon/{a.directory.name}/search.json)。',
 '每条记录包含启动命令、参数、原始报告路径、校验、RDMA 计数、性能值和参考曲线 margin。',
 f'图由 `plot-chameleon-tuning.py` 从该记录生成，搜索点与通过验证的{repeats}条曲线分开显示。','']
 (ROOT/'progress/chameleon-tuning-fig78.md').write_text('\n'.join(lines))
 # Read existing snapshots only; do not add monitoring work inside the Guest.
 diagnostics=[]
 for case,entry in data['cases'].items():
  for tag,r in entry['trials'].items():
   if r['status']=='RUNNING' or not r.get('report'):continue
   raw=json.loads(Path(r['report']).read_text())['cases'][case]
   if not all(k in raw for k in ['baseline','end','summary']):continue
   b,e=raw['baseline'],raw['end']
   row={'case':case,'trial_id':tag,'status':r['status'],'memory_mib':r['memory_mib'],
        'window_seconds':raw['summary']['duration_seconds']}
   for group,keys in {'tracker':['worker_cpu_ns','processed_samples'],
                      'manager':['split_cpu_ns','select_cpu_ns','split_ok','selected'],
                      'policy':['worker_ns','epochs','high_epochs','psi_some_ns'],
                      'rdma':['write_bytes','read_bytes','transfer_errors','map_failures'],
                      'shadow':['demand_fault_successes','demand_fault_failures']}.items():
    for key in keys:
     row[group+'_'+key]=e[group][key]-b[group][key]
   row['report']=r['report'];diagnostics.append(row)
 if diagnostics:
  with (a.directory/'mechanism-diagnostics.csv').open('w') as f:
   writer=csv.DictWriter(f,fieldnames=list(diagnostics[0]));writer.writeheader();writer.writerows(diagnostics)
 (a.directory/'accepted-parameters.json').write_text(json.dumps({'status':data['status'],'reference_kind':'user-supplied estimated target curves','mechanism_criterion':data.get('mechanism_criterion'),'trend_criterion':data.get('trend_criterion'),'applications':accepted},indent=2)+'\n')
 candidates={case:[r for tag,r in entry['trials'].items() if tag.startswith('d') and
                    r.get('status')=='PASS' and r.get('reference_status')=='PASS']
             for case,entry in data['cases'].items()}
 (a.directory/'candidate-points.json').write_text(json.dumps({'scope':'Completed single-run candidates below supplied targets; repetition acceptance is separate in accepted-parameters.json',
     'tracking_defaults':data.get('plan',{}).get('tracking'),'pre_reclaim_headroom_mib':data.get('plan',{}).get('pre_reclaim_headroom_mib'),
     'free_pages_per_epoch':data.get('plan',{}).get('free_pages'),'applications':candidates},indent=2)+'\n')
 print(json.dumps({'status':data['status'],'cases':{k:v['status'] for k,v in data['cases'].items()},'accepted':list(accepted)}))
if __name__=='__main__':main()
