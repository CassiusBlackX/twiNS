#!/usr/bin/env python3
"""Live/final tables. No simulator execution; only timing-eligible attempts enter summaries."""
import argparse,collections,datetime,json,statistics
from pathlib import Path
def read(p):
 try:return json.loads(p.read_text())
 except (OSError,ValueError):return {}
def stats(xs,key):
 a=[x[key]/1e9 for x in xs if x.get(key) is not None]
 return dict(n=len(a),median_s=statistics.median(a),min_s=min(a),max_s=max(a)) if a else dict(n=0)
def selected(ps):
 by={}
 for p in sorted(ps):
  v=read(p)
  if v.get('timing_eligible') and v.get('formal_id'):v['local_receipt']=str(p);by.setdefault(v['formal_id'],v)
 return list(by.values())
def cell(v):return f"{v['median_s']:.6f} [{v['min_s']:.6f}, {v['max_s']:.6f}], n={v['n']}" if v['n'] else '尚无有效点'
def report(E,materials=None):
 X=E/'s50/export';O=E/'reports';O.mkdir(exist_ok=True);now=datetime.datetime.now().astimezone().isoformat();state=read(X/'state.json');ver=read(E/'receipts/archive-verification.json')
 lines=['# 新一轮独占设置实验：实时结果（2026-11-18）','',f'本机更新时间：{now}。',f'当前远端状态：`{state.get("stage","尚无回传")}`，设置 `{state.get("setting",state.get("next_setting","—"))}`。',f'已完成选留文件SHA-256回传检查：{ver.get("verified",0)}/{ver.get("indexed",0)}。', '', '数据根为 `twiNS/out/isolated-evidence-20261118/`；本页只属于新轮次，不混合11/16的并行资源观测。一次仅运行一个被测设置；独立作业的内部并发属于该设置。空闲准入、外部GPU活动及资源样本另存，外部活动造成的污染点保留但不进入本页统计。','','计时区间为中位数及观测最小/最大值，均非置信区间。三轮未齐的组不作最终性能结论。CUBIC最终并列表另使用三轮最小事件循环时间。']
 cubic=selected((X/'cubic').glob('*/result.json'));scans=selected((X/'tcp-scans').glob('*/result.json'));lat=selected((X/'attempts').glob('*/result.json'));batches=selected((X/'ai-batches').glob('*.json'));mtp=selected((X/'mtp').glob('*/result.json'))
 output=dict(updated=now,state=state,cubic=cubic,scans=scans,latency=lat,batches=batches,mtp=mtp,archive_verification=ver)
 lines+=['','## 验收与计时覆盖','','| 分组 | 已有有效计时/预定 | 输出验收 |','|---|---:|---:|',f'| CUBIC | {len(cubic)}/192 | 匹配 {sum(x["accepted"] for x in cubic)}；Unison差异单列 |',f'| TCP两组扫描 | {len(scans)}/21批 | 全部配置通过 {sum(x["all_accepted"] for x in scans)} 批 |',f'| AI延迟及消融 | {len(lat)}/81 | 通过 {sum(x["accepted"] for x in lat)} |',f'| AI吞吐 | {len(batches)}/18批 | 全部作业通过 {sum(x["all_accepted"] for x in batches)} 批 |',f'| MTP正式尝试 | {sum(x.get("phase")!="pilot" for x in mtp)}（18个线程扫描点+条件性最佳复测） | 完成/差异/中止逐项保留 |']
 lines+=['','## CUBIC逐设置结果','','Unison设置即使输出不同也保留其执行器性能；不把差异称为逐流精确。seq/TwiNS不匹配或有flag的点不作为保证成立时的性能。','','| 输入 | 设置 | 完成样本 | 匹配次数 | 最小事件循环秒 | flags次数 |','|---|---|---:|---:|---:|---:|']
 groups=collections.defaultdict(list)
 for x in cubic:groups[x['case'],x['setting']].append(x)
 for (name,setting),xs in sorted(groups.items()):
  vals=[x['event_loop_ns']/1e9 for x in xs if x.get('event_loop_ns') is not None and (setting.startswith('u') or x['accepted'])];lines.append(f'| {name} | {setting} | {len(xs)}/3 | {sum(x["validation"]["equal"] for x in xs)} | '+(f'{min(vals):.9f}' if vals else '—')+f' | {sum(bool(x["final_flagged"]) for x in xs)} |')
 for group in ('scan138','scan1260'):
  lines+=['',f'## {group} 整批墙钟','','| 设置 | 中位数 [最小,最大] 秒 | 全部配置验收批数 | 实际峰值并发 |','|---|---:|---:|---:|']
  gs=collections.defaultdict(list)
  for x in scans:
   if x['group']==group:gs[x['engine'],x['setting']].append(x)
  for (eng,s),xs in sorted(gs.items()):lines.append(f'| {eng} {s} | {cell(stats(xs,"makespan_ns"))} | {sum(x["all_accepted"] for x in xs)}/{len(xs)} | {sorted({x["peak_actual_concurrency"] for x in xs})} |')
 lines+=['','## AI延迟与消融','','| 负载 | 设置 | 中位数 [最小,最大] 秒 | 验收通过/已测 |','|---|---|---:|---:|']
 gs=collections.defaultdict(list)
 for x in lat:gs[x['case'],('ablation '+str(x['options'])) if 'ablation' in x['formal_id'] else x['engine']].append(x)
 for (n,eng),xs in sorted(gs.items()):lines.append(f'| {n} | {eng} | {cell(stats([x for x in xs if x["accepted"]],"wall_ns"))} | {sum(x["accepted"] for x in xs)}/{len(xs)} |')
 lines+=['','GPU/系统层/同步/worker分项保留于逐次JSON的components，worker busy为各worker总和，GPU桶含host launch/copy/synchronize，分项有重叠，不相加当墙钟。新SimAI保存EndToEnd CSV迭代摘要，避免只看完成消息数。','','## AI八作业吞吐','','| 设置 | 中位数 [最小,最大] 秒 | 整批验收 | 实际峰值并发 |','|---|---:|---:|---:|']
 gs=collections.defaultdict(list)
 for x in batches:gs[x['engine'],x['target_concurrency']].append(x)
 for (eng,s),xs in sorted(gs.items()):lines.append(f'| {eng} {s} | {cell(stats(xs,"makespan_ns"))} | {sum(x["all_accepted"] for x in xs)}/{len(xs)} | {sorted({x["peak_actual_concurrency"] for x in xs})} |')
 lines+=['','## MTP：独立调度语义基线','','| 负载 | 阶段/线程/轮 | rc | 完成 | 整文件匹配顺序参照 | wall秒 |','|---|---|---:|---|---|---:|']
 for x in sorted(mtp,key=lambda v:(v['case'],v['phase'],v['threads'],v['round'])):
  if x['phase']=='pilot':continue
  lines.append(f'| {x["case"]} | {x["phase"]}/t{x["threads"]}/r{x["round"]} | {x["rc"]} | {x["complete"]} | {x["validation"].get("byte_equal")} | {x["wall_ns"]/1e9:.9f} |')
 lines+=['','MTP与顺序版本来自同一已打补丁源码，NS3_MTP开关不同；源哈希及编译/链接记录在s50/receipts。t=1仍是MTP调度器，不冒充NS3_MTP=OFF顺序版本。线程扫描保留失败与两小时删失；每负载最快完成设置保留原始点并补两次，输出相同与速度分开解释。','','计划见 [experiment_isolated_plan_20261118.md](experiment_isolated_plan_20261118.md)，上一轮结果见 [experiment_final_20261118.md](experiment_final_20261118.md)。本轮不修改LaTeX、不合并main或发布。']
 (O/'live-data.json').write_text(json.dumps(output,indent=2)+'\n');text='\n'.join(lines)+'\n';(O/'experiment_isolated_live_20261118.md').write_text(text)
 if materials:(materials/'experiment_isolated_live_20261118.md').write_text(text)
 return output
if __name__=='__main__':
 a=argparse.ArgumentParser();a.add_argument('--evidence',type=Path,required=True);a.add_argument('--materials',type=Path);x=a.parse_args();report(x.evidence,x.materials)
