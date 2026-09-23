#!/usr/bin/env python3
"""Publish measured tracking overhead and unfinished pairs without selecting runs."""
import argparse
import json
from pathlib import Path
import time

ROOT=Path(__file__).resolve().parents[2]


def render(directory):
    state=json.loads((directory/'search.json').read_text())
    lines=['# PEBS + HHH overhead 实测结果','',f"校准队列：`{state['status']}`。回收曲线搜索暂停。",'',
           '相同内核、VM、工作负载，policy关闭；off/on、on/off、off/on三对，每次独立重启VM，并统一Guest文件缓存。',
           '验收：三轮平均≤2%，每轮≤5%；KV同时检查P95。负值原样保留，不能解释为确定加速。',
           '完整协议与无效早期试验：[记录](chameleon-tracking-overhead.md)。','',
           '| PEBS events / HHH ms | 应用 | 三轮 slowdown % | 平均 % | 最大 % | P95 平均/最大 % | 结论 |',
           '|---|---|---|---:|---:|---|---|']
    records=[]
    for candidate in state['candidates']:
        for case,value in candidate['cases'].items():
            records.append((candidate,case,value['report']))
    active=state.get('active')
    if active and Path(active['report']).exists():
        candidate=next(c for c in state['candidates'] if c['sample_period']==active['sample_period'])
        if not any(p==active['report'] for _,_,p in records):records.append((candidate,active['case'],active['report']))
    for candidate,case,path in records:
        raw=json.loads(Path(path).read_text());entry=raw['cases'].get(case,{})
        completed=[p for p in entry.get('pairs',[]) if 'off' in p and 'on' in p and all(p[k].get('status')=='PASS' for k in ['off','on'])]
        vals=[100*(p['on']['performance']['cost']/p['off']['performance']['cost']-1) for p in completed]
        verdict=entry.get('overhead',{}).get('status','RUNNING')
        scores=entry.get('overhead',{}).get('metrics',{})
        score=scores.get('slowdown_percent',{})
        p95=scores.get('p95_slowdown_percent',{})
        def fmt(value):return '—' if value is None else f'{value:+.2f}'
        lines.append(f"| {candidate['sample_period']} / {candidate['hhh_interval_ms']} | {case} | {', '.join(fmt(v) for v in vals) or '—'} | {fmt(score.get('mean_percent'))} | {fmt(score.get('maximum_percent'))} | {fmt(p95.get('mean_percent'))} / {fmt(p95.get('maximum_percent'))} | {verdict} |")
    if active:lines+=['',f"当前应用：{active['case']}；PEBS {active['sample_period']} events。",f"原始报告：[report.json]({active['report']})。"]
    lines+=['','HHH维护活动与活跃候选转换扫描分别记录；没有热候选时可只有aging。未发生拆分时，不将本次低开销当作频繁拆分场景的验收。',
            '采样与HHH低开销不代替后续热度识别、RDMA回收和恢复、图7/8曲线验证。','',
            f'完整调度记录：[search.json]({directory / "search.json"})。','']
    path=ROOT/'progress/chameleon-tracking-overhead-results.md'
    tmp=path.with_suffix('.tmp');tmp.write_text('\n'.join(lines));tmp.replace(path)
    return state['status']


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',type=Path,default=ROOT/'benchmarks/results/chameleon/tracking-calibration-fresh')
    parser.add_argument('--watch',action='store_true')
    a=parser.parse_args()
    while True:
        if a.watch and not (a.directory/'search.json').exists():
            time.sleep(2)
            continue
        status=render(a.directory.resolve())
        if not a.watch or status!='RUNNING':break
        time.sleep(20)


if __name__=='__main__':main()
