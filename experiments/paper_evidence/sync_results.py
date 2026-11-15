#!/usr/bin/env python3
"""Pull only exported receipts, checksums, small inputs and build records; never remote-raw."""
import datetime,hashlib,json,statistics,subprocess,time
from pathlib import Path
HERE=Path('/home/cassius/Work/SpecDES/twiNS/out/paper-evidence-20261116');DEST=HERE/'s50';DEST.mkdir(parents=True,exist_ok=True)
REMOTE='s50:Videos/twins-paper-evidence-20261116/out/paper-evidence-20261116/'
def read(p,default=None):
 try:return json.loads(p.read_text())
 except (OSError,ValueError):return default
verified={}
while True:
 for part in ('export','receipts'):
  rc=subprocess.run(['rsync','-a','--timeout=30','-e','ssh -F /home/cassius/.ssh/config -o BatchMode=yes -o ConnectTimeout=10',REMOTE+part+'/',str(DEST/part)+'/'],capture_output=True,text=True)
  with (HERE/'receipts/sync.jsonl').open('a') as f:f.write(json.dumps(dict(at=datetime.datetime.now().astimezone().isoformat(),part=part,rc=rc.returncode,stderr=rc.stderr[:2000]))+'\n')
 index=read(DEST/'export/local-archive-index.json',{});missing=[];bad=[];checked=0
 for name,v in index.get('files',{}).items():
  p=DEST/'export'/name
  if not p.is_file():missing.append(name);continue
  st=p.stat();key=(st.st_mtime_ns,st.st_size,v['sha256'])
  if verified.get(name)!=key:
   h=hashlib.sha256()
   with p.open('rb') as f:
    while b:=f.read(1<<20):h.update(b)
   if h.hexdigest()!=v['sha256']:bad.append(name);continue
   verified[name]=key
  checked+=1
 check=dict(utc_ns=time.time_ns(),remote_index_utc_ns=index.get('utc_ns'),indexed_files=len(index.get('files',{})),verified_files=checked,missing=missing,hash_mismatches=bad,scope=index.get('scope'),complete=bool(index) and not missing and not bad)
 (HERE/'receipts/local-archive-verification.json').write_text(json.dumps(check,indent=2)+'\n')
 results=[x for p in (DEST/'export/attempts').glob('*/result.json') if (x:=read(p))];groups={};ablations={}
 for x in results:
  if x['label'].startswith('latency-'):groups.setdefault((x['case'],x['engine']),[]).append(x)
  elif x['label'].startswith('ablation-'):ablations.setdefault(tuple(x['options']),[]).append(x)
 now=datetime.datetime.now().astimezone().isoformat();lines=['# TwiNS s50 实验结果（自动更新）','',f'本机同步时间：{now}。','',f'已回传 {len(results)} 次尝试，{sum(bool(x["accepted"]) for x in results)} 次通过对应输出/flags 验收。校准、候选门禁与正式 A3 分组分别保存。',f'已完成 export 证据的 SHA-256 回传检查：{checked}/{check["indexed_files"]} 个文件；缺失 {len(missing)}，哈希差异 {len(bad)}。检查仅覆盖远端索引列出的已完成记录，不表示所有实验已经完成。','','| 负载 | 设置 | 完成/通过 | 中位数 s | 范围 s | 干扰疑似数 |','|---|---|---:|---:|---:|---:|']
 for (n,e),xs in sorted(groups.items()):
  ts=[x['wall_ns']/1e9 for x in xs if x['accepted']];sus=sum(bool(x.get('interference',{}).get('suspected')) for x in xs)
  lines.append(f'| {n} | {e} | {len(xs)}/{len(ts)} | '+(f'{statistics.median(ts):.9f} | {min(ts):.9f}–{max(ts):.9f}' if ts else '— | —')+f' | {sus} |')
 lines+=['','未齐三次的分组不能用于波动结论。原始点全部保留；当前早期分组的干扰分类将在延迟交接时统一用 consecutive-v2 复核。底层原始资源采样始终保留。','','## 消融','','| 参数 | 完成/通过 | 中位数 s |','|---|---:|---:|']
 for opts,xs in sorted(ablations.items()):
  ts=[x['wall_ns']/1e9 for x in xs if x['accepted']];lines.append(f'| {" ".join(opts)} | {len(xs)}/{len(ts)} | '+(f'{statistics.median(ts):.9f}' if ts else '—')+' |')
 lines+=['','## 真实整批吞吐','','验证和 provenance 在 makespan 之外。CPU cap 从已验收 A3 cgroup peak 冻结：ceil(1.10×最大峰值+512MiB)，至少 1GiB；总预算 70GiB。目标并发不能代替实际并发。','','| 批次 | 目标/实际峰值并发 | makespan s | 验证 s | 全部通过 |','|---|---:|---:|---:|---|']
 batches=[x for p in (DEST/'export/batches-v2').glob('*.json') if (x:=read(p)) and 'makespan_ns' in x]
 for x in sorted(batches,key=lambda v:v['label']):lines.append(f'| {x["label"]} | {x["target_concurrency"]}/{x["peak_actual_concurrency"]} | {x["makespan_ns"]/1e9:.9f} | {x["validation_ns"]/1e9:.9f} | {x["all_accepted"]} |')
 if not batches:lines+=['','尚无已完成的正式批次。']
 gate=read(DEST/'export/candidate-gates/summary.json');health=read(DEST/'export/health.json',{})
 lines+=['','## 队列与版本','','候选 CUDA 门禁：'+(str(gate.get('status')) if gate else '尚无最终验收摘要')+'。A3 固定 A1 二进制与后续修复候选分别归档，不混合求中位数。',f'最近远端空闲空间：{health.get("free_bytes",0)/2**30:.2f} GiB（健康采样 utc_ns={health.get("utc_ns")}）。','','```text',health.get('services','尚未回传健康采样'),'```','','计划见 [experiment_plan_20261116.md](experiment_plan_20261116.md)，已验收解释见 [experiment_results_20261116.md](experiment_results_20261116.md)。原始纳秒、哈希、校验、资源采样保存在本机 `twiNS/out/paper-evidence-20261116/s50/export/`。大体积 trace 未回传。']
 content='\n'.join(lines)+'\n';(HERE/'reports/experiment_results_live_20261116.md').write_text(content);Path('/home/cassius/Projects/TwiNS/experiment_results_live_20261116.md').write_text(content)
 if (DEST/'export/all-finished.json').exists():break
 time.sleep(30)
