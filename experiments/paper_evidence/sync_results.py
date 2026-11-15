#!/usr/bin/env python3
"""Local pull loop. Allowlist is remote export/ + receipts/; never remote-raw/."""
import datetime,json,statistics,subprocess,time
from pathlib import Path
HERE=Path('/home/cassius/Work/SpecDES/twiNS/out/paper-evidence-20261116')
DEST=HERE/'s50'; DEST.mkdir(parents=True,exist_ok=True)
REMOTE='s50:Videos/twins-paper-evidence-20261116/out/paper-evidence-20261116/'
while True:
 for part in ('export','receipts'):
  rc=subprocess.run(['rsync','-a','--timeout=30','-e','ssh -F /home/cassius/.ssh/config -o BatchMode=yes -o ConnectTimeout=10',REMOTE+part+'/',str(DEST/part)+'/'],capture_output=True,text=True)
  with (HERE/'receipts/sync.jsonl').open('a') as f:f.write(json.dumps(dict(at=datetime.datetime.now().astimezone().isoformat(),part=part,rc=rc.returncode,stderr=rc.stderr[:2000]))+'\n')
 results=[]
 for p in (DEST/'export/attempts').glob('*/result.json'):
  try:results.append(json.loads(p.read_text()))
  except (OSError,ValueError):pass
 groups={}
 for x in results:
  if x['label'].startswith('latency-'):groups.setdefault((x['case'],x['engine']),[]).append(x)
 lines=['# TwiNS 新实验结果（自动更新）','',f'本机同步时间：{datetime.datetime.now().astimezone().isoformat()}。', '', '本页仅由已回传的 result.json 生成；校准不计正式重复，启动队列不计完成。详细计划见 [experiment_plan_20261116.md](experiment_plan_20261116.md)。','',f'本机已保存 {len(results)} 次尝试，其中 {sum(x["accepted"] for x in results)} 次通过相应验收。','', '| 负载 | 设置 | 已完成/通过 | 中位数秒 | 范围秒 |','|---|---|---:|---:|---:|']
 for (n,e),xs in sorted(groups.items()):
  ts=[x['wall_ns']/1e9 for x in xs if x['accepted']]
  lines.append(f'| {n} | {e} | {len(xs)}/{len(ts)} | '+(f'{statistics.median(ts):.9f} | {min(ts):.9f}–{max(ts):.9f} |' if ts else '— | — |'))
 lines+=['','原始纳秒测量、版本/输入/参照哈希、逐次命令、校验与资源采样：本机 `twiNS/out/paper-evidence-20261116/s50/export/`。大体积 trace 未回传。','', 'A1 固定底数小负载校准已通过；理论前提审计另见手工报告。A2/A4/A6 的完成状态以手工审计报告为准，本页不自动将其勾选。', '', '三次正式重复未齐的分组不能用于波动结论；历史单次数字仍保留原来源。']
 text='\n'.join(lines)+'\n';(HERE/'reports/experiment_results_live_20261116.md').write_text(text)
 Path('/home/cassius/Projects/TwiNS/experiment_results_live_20261116.md').write_text(text)
 if (DEST/'export/all-finished.json').exists():break
 time.sleep(30)
