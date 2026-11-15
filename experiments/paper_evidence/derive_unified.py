#!/usr/bin/env python3
"""Orthogonal execution/output/history status; preserve original run receipts."""
import json,re
from collections import Counter,defaultdict
from pathlib import Path
E=Path('/home/cassius/Work/SpecDES/twiNS/out/paper-evidence-20261116');R=E/'reports';inv=json.loads((E/'manifests/scenario-inventory.json').read_text());cachepath=R/'reference-id-audit.json';cache=json.loads(cachepath.read_text()) if cachepath.exists() else {};rows=[];bykey={}
for p in (E/'local/unified').glob('*/result.json'):
 x=json.loads(p.read_text());cfg=x['config'];attempts=[]
 for v in x['attempts']:
  logpath=p.parent/(str(v['capacity_bytes'])+'.log');log=logpath.read_text(errors='replace');parts=re.split(r'^.*running again with[^\n]*\n',log,flags=re.M);terminal=parts[-1]
  flag=bool(re.search(r'were undecided|warning:.*(?:ties the chains|not exact|overflow)|error:',terminal));m=re.findall(r'flows (\d+) completed (\d+)',terminal);rd=re.findall(r'flows done (\d+) of (\d+)',terminal)
  expected,completed=(int(m[-1][0]),int(m[-1][1])) if m else (int(rd[-1][1]),int(rd[-1][0])) if rd else (None,None)
  status='timeout' if v['timeout'] else 'refused_or_error' if v['rc'] not in (0,2) else 'stranded' if v['rc']==2 or (expected is not None and completed<expected) else 'complete'
  refcheck=None
  if cfg['kind']=='rdma' and Path(cfg['reference']).exists():
   ref=cfg['reference'];sha=cfg['reference_sha256']
   if ref not in cache or cache[ref]['sha256']!=sha:
    ids=[];bad=0
    for l in Path(ref).read_text().splitlines():
     q=l.split()
     if len(q)!=3:bad+=1
     elif q:ids.append(q[0])
    cache[ref]=dict(sha256=sha,rows=len(ids),duplicate_ids=len(ids)-len(set(ids)),bad_rows=bad)
   refcheck=cache[ref]
  equal=v['validation'].get('equal',False)
  attempts.append(dict(**v,terminal_history_flag=flag,terminal_overflow=bool(re.search('warning:.*(?:capacity overflow|a capacity overflowed)',terminal)),internal_retry_count=len(parts)-1,internal_pass_walls='unknown; combined process wall is measured',expected_flows=expected,completed_flows=completed,execution_status=status,output_status='match' if equal else 'mismatch_or_missing',reference_id_check=refcheck,actual_ids_unique_by_exact_reference_match=bool(equal and refcheck and not refcheck['duplicate_ids'] and not refcheck['bad_rows'])))
 last=attempts[-1];accepted=last['execution_status']=='complete' and last['output_status']=='match' and not last['terminal_history_flag'] and not last['terminal_overflow']
 row=dict(receipt=str(p.relative_to(E)),config=cfg,attempts=attempts,cumulative_wall_ns=x['cumulative_wall_ns'],original_final_status=x['final_status'],complete_matching_unflagged=accepted,final_execution=last['execution_status'],final_output=last['output_status'],final_history='flagged' if last['terminal_history_flag'] or last['terminal_overflow'] else 'unflagged',final_capacity_bytes=last['capacity_bytes'],scope='excluded_DONS_input_failure_not_a_scientific_measurement' if cfg['case'].startswith('dons_') else 'known_unsupported_luna_NewReno' if cfg['case'] in ('k8_luna50_TcpNewReno','k8_luna70_TcpNewReno') else 'configured_suite');rows.append(row);bykey[cfg['execution_config_sha256']]=row
cachepath.write_text(json.dumps(cache,indent=2)+'\n')
entries=[]
for x in inv:
 run=bykey.get(x['execution_config_sha256']);entries.append(dict(config=x,new_receipt=run['receipt'] if run else None,new_run_status='recorded' if run else 'not scheduled here: AI repetitions have separate engine-specific receipts' if x['kind'].startswith('ai') else 'missing_reference' if x['reference_sha256'] is None else 'pending',historical_pass_cost='unknown unless a separate historical receipt records it'))
data=dict(source='48e0a3a',entries=entries,completed_configurations=rows,scope='configuration keys are conservative operational keys; not a global scientific deduplication or an assertion that all suite entries passed')
(R/'unified-manifest.json').write_text(json.dumps(data,indent=2)+'\n')
groups=defaultdict(list)
for x in rows:
 for suite,case in x['config']['memberships']:groups[suite].append(x)
lines=['## A4 已执行配置的逐项汇总','','执行状态、输出和 history 分开。下表按 suite membership 计数，可有交叠。内部重跑总 wall 已计入，每个内部 pass 的独立 wall 若缺失仍为未知。完整 manifest 含全部 inventory 条目，AI 的重复与历史验证分别保留，未运行项不冒充通过。','','| 集合 | 已记录 | 完成且输出匹配且无 flag | 输出不符/缺失 | 最终有 flag | stranded | refused/error/timeout | 两次显式容量尝试 |','|---|---:|---:|---:|---:|---:|---:|---:|']
for name,xs in sorted(groups.items()):lines.append(f'| {name} | {len(xs)} | {sum(x["complete_matching_unflagged"] for x in xs)} | {sum(x["final_output"]!="match" for x in xs)} | {sum(x["final_history"]=="flagged" for x in xs)} | {sum(x["final_execution"]=="stranded" for x in xs)} | {sum(x["final_execution"] in ("refused_or_error","timeout") for x in xs)} | {sum(len(x["attempts"])>1 for x in xs)} |')
lines+=['','k8 luna50/70 NewReno 是历史已知不支持项（旧104门禁明确排除）；本清单保留其失败而不称为新回归。DCQCN 超长 slot 属明确拒绝。DONS 两条仅发生 missing-topology 输入拒绝，未运行模型，不使用其数值且不补跑，后续排程已排除。内部 overflow 后成功回退的终态与前一 pass 的诊断分列，combined wall 包括所有内部 pass。','','RDMA 的全部三字段精确匹配会保留行多重性；参照 ID 唯一性另查，见 `reports/reference-id-audit.json`。TCP 比较按行保留输入对象顺序与前六个字段，未将重复 flow tuple 擅自当坏 ID。完整状态/每尝试/累计时间在 `reports/unified-manifest.json`。']
(R/'unified-summary.md').write_text('\n'.join(lines)+'\n')
