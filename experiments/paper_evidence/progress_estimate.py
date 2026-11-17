"""Stage-aware wall-clock budget from completed batch receipts; no experiment control."""
import datetime,json,re,statistics,time
from pathlib import Path

def read(p,default=None):
 try:return json.loads(p.read_text())
 except (OSError,ValueError):return default

def estimate(root,now_ns=None):
 now_ns=now_ns or time.time_ns();exp=root/'s50/export';batchroot=exp/'batches-v2';done={}
 for p in batchroot.glob('*.json'):
  x=read(p)
  if x and 'makespan_ns' in x:done[x['label']]=x
 plan=[f'cpu-r{r}-c{c}' for c in (1,2,4,8) for r in (1,2,3)]+[f'{e}-r{r}' for e in ('p0','p16') for r in (1,2,3)]
 pending=[x for x in plan if x not in done];groups={}
 def kind(label):
  m=re.fullmatch(r'cpu-r\d+-c(\d+)',label);return ('simai',int(m[1])) if m else (label.split('-r')[0],1)
 for label,x in done.items():
  if x['all_accepted']:groups.setdefault(kind(label),[]).append(x['makespan_ns']/1e9)
 state=read(exp/'parallel/batches-active.json',{});active=state.get('active',[]) if now_ns-state.get('utc_ns',0)<120e9 else [];predictions=[]
 for label in pending:
  k=kind(label);key=k if k in groups else min((g for g in groups if g[0]==k[0]),key=lambda g:abs(g[1]-k[1]),default=None)
  if key is None:continue
  seconds=statistics.median(groups[key]);elapsed=0
  if label in active:
   states=[v for p in batchroot.glob(label+'-trial*/state.json') if (v:=read(p)) and v.get('status')=='running']
   if states:elapsed=max(0,(now_ns-max(v['utc_ns'] for v in states))/1e9)
  predictions.append(dict(label=label,active=label in active,elapsed_s=elapsed,reference_group=key,expected_batch_s=seconds,remaining_s=max(0,seconds-elapsed)))
 known=len(predictions)==len(pending)
 remaining=(max((x['remaining_s'] for x in predictions if x['active']),default=0)+sum(x['remaining_s'] for x in predictions if not x['active'])) if known else None
 # The current tail consists of c4/c8 batches that each reserve the full70GiB.
 tail_serial=all(kind(label)[0]=='simai' and kind(label)[1]>=4 for label in pending)
 latency=read(exp/'latency-finished.json');gate=read(exp/'candidate-gates/summary.json')
 return dict(updated_utc_ns=now_ns,latency_finished_utc_ns=latency.get('utc_ns') if latency else None,cuda_gate_status=gate.get('status') if gate else None,batches_completed=len(done),batches_accepted=sum(x['all_accepted'] for x in done.values()),batches_total=18,pending=predictions,remaining_s=remaining,estimated_finish_utc_ns=int(now_ns+remaining*1e9) if remaining is not None else None,buffered_finish_utc_ns=int(now_ns+(remaining*1.15+(900 if remaining else 0))*1e9) if remaining is not None else None,forecast_scope='remaining full-memory c4/c8 tail; c8 uses observed c4 duration until c8 results exist' if tail_serial else 'conservative serialized future-batch budget; overlap can finish earlier',not_a_confidence_interval=True)

def local_time(ns):
 return datetime.datetime.fromtimestamp(ns/1e9).astimezone().strftime('%m月%d日 %H:%M') if ns else '未知'

def markdown(x):
 lines=['# 实验当前阶段与剩余时间','',f'自动更新时间：{local_time(x["updated_utc_ns"])}（UTC+8）。','','| 阶段 | 实际状态 |','|---|---|',f'| 本机A2/A4/A6及HIP候选 | 已于11月16日07:40全部执行结束 |',f'| A3及消融 | '+(f'已于{local_time(x["latency_finished_utc_ns"])}完成' if x['latency_finished_utc_ns'] else '进行中')+' |',f'| CUDA候选门禁 | {x["cuda_gate_status"] or "尚无最终结果"} |',f'| 吞吐固定矩阵 | {x["batches_completed"]}/18批完成，{x["batches_accepted"]}批通过 |']
 if x['remaining_s'] is not None:
  lines+=['',f'吞吐剩余预算约 **{x["remaining_s"]/3600:.1f}小时**；按当前实测推算结束约 **{local_time(x["estimated_finish_utc_ns"])}**，加15%与15分钟余量约至 **{local_time(x["buffered_finish_utc_ns"])}**。这是工程排期，不能当统计置信区间。']
 lines+=['','| 剩余批次 | 状态 | 预计剩余小时 | 参照时长来源 |','|---|---|---:|---|']
 for v in x['pending']:lines.append(f'| {v["label"]} | '+('运行中' if v['active'] else '排队')+f' | {v["remaining_s"]/3600:.2f} | {v["reference_group"]} |')
 lines+=['','最后c4/c8批次各自需要完整70GiB预留预算，批内已按目标并发运行；每批又被约2.5小时的单线程长作业拖住。单张GPU的全部P0/P16吞吐批次已经完成时，GPU空闲不代表CPU实验卡住。','',f'估算口径：{x["forecast_scope"]}。当前固定矩阵结束后没有新增模型实验接续；还需最终校验与本机归档同步，不自动合并main或发布。','','Astra可使用已经完成的A2/A3/A4/A6及候选验证数据推进改稿，最后吞吐数据继续自动补入。新旧并行资源条件分别保留，不能称全部来自独占整机。普通大trace不回传。','','[完整结果](experiment_results_20261116.md) · [实时计时](experiment_results_live_20261116.md) · [执行总状态](experiment_execution_status_20261116.md)']
 return '\n'.join(lines)+'\n'
