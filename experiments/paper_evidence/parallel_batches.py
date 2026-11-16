#!/usr/bin/env python3
"""Parallel replicas on disjoint physical cores, with guaranteed remaining-memory quotas.
No per-job admission is delayed by another batch: each reserves a worst-case bound for
all of its unfinished jobs. P16 retains its original whole-core affinity exclusively.
"""
import concurrent.futures as cf,fcntl,json,os,signal,subprocess as sp,threading,time
from pathlib import Path
import batches as b
import campaign as c
P=c.EXPORT/'parallel';P.mkdir(exist_ok=True)
CORE_ORDER=[1,9,2,10,3,11,4,12,5,13,6,14,7,8] # core0: P0; core15: housekeeping; no SMT siblings
BUDGET=70;GPU_HOST=10
CANCEL=threading.Event()

def run():
 policy=b.cap_manifest();jobs=[]
 # Low-concurrency replicas can overlap without stealing another batch's admitted capacity.
 for con in (1,2,4,8):
  for r in (1,2,3):jobs.append(dict(label=f'cpu-r{r}-c{con}',engine='simai',con=con))
 for e in ('p0','p16'):
  for r in (1,2,3):jobs.append(dict(label=f'{e}-r{r}',engine=e,con=1))
 jobs=[j for j in jobs if not (b.BROOT/(j['label']+'.json')).exists()]
 allocations={};active={};mutex=threading.Lock()
 def state(action,label):
  c.append(P/'batch-dispatch.jsonl',dict(utc_ns=time.time_ns(),action=action,label=label,allocations={k:dict(v) for k,v in allocations.items()},budget_gib=BUDGET))
 def execute(j,cores,mem):
  # Provenance, monitors and validation use a housekeeping core; simulated processes have explicit masks.
  os.sched_setaffinity(0,{15})
  def update(bound,keep):
   with mutex:
    old=allocations[j['label']]
    if bound>old['memory'] or not set(keep)<=set(old['cores']):raise RuntimeError('quota must only shrink')
    if bound!=old['memory'] or keep!=old['cores']:old.update(memory=bound,cores=keep);state('shrink',j['label'])
  ctx=dict(policy='parallel-20261116',co_running_batches_allowed=j['engine']!='p16',initial_memory_reservation_gib=mem,guarantee='worst-case sum of top concurrency caps among unfinished jobs; total reservations<=70GiB; physical cores disjoint',dispatch_log='export/parallel/batch-dispatch.jsonl',note='shared RAM bandwidth remains shared; record co-runners; no claim that parallel replicas were isolated whole-machine runs')
  b.batch(j['label'],j['engine'],j['con'],policy,cores,ctx,update if j['engine']=='simai' else None,CANCEL)
 with cf.ThreadPoolExecutor(max_workers=16) as pool:
  while jobs or active:
   for f in list(active):
    if f.done():
     j=active.pop(f)
     try:f.result()
     finally:
      with mutex:allocations.pop(j['label'],None);state('completed',j['label'])
   with mutex:
    used={i for v in allocations.values() for i in v['cores']};reserved=sum(v['memory'] for v in allocations.values());gpu=any(v['gpu'] for v in allocations.values())
    # A fully drained boundary is used for P16; it never overlaps our CPU/P0 batches.
    p16=next((j for j in jobs if j['engine']=='p16'),None)
    if p16 and not active:candidates=[p16]
    elif any(v['exclusive'] for v in allocations.values()):candidates=[]
    else:candidates=sorted([j for j in jobs if j['engine']!='p16'],key=lambda j:(0 if j['engine']=='p0' else 1,j['con'],j['label']))
    for j in candidates:
     e=j['engine'];con=j['con']
     if e!='simai' and gpu:continue
     if e=='p16' and active:continue
     mem=min(BUDGET,sum(sorted((v['cap_gib'] for v in policy['jobs'].values()),reverse=True)[:con])) if e=='simai' else GPU_HOST
     available=[i for i in CORE_ORDER if i not in used]
     cores=available[:con] if e=='simai' else list(range(17)) if e=='p16' else [0]
     if len(cores)<con or set(cores)&used or reserved+mem>BUDGET:continue
     meminfo=Path('/proc/meminfo').read_text();import re
     # The reservations already include10%+512MiB per job; retain4GiB additional physical slack.
     if int(re.search(r'^MemAvailable:\s+(\d+)',meminfo,re.M)[1])/2**20<mem+4:continue
     allocations[j['label']]=dict(memory=mem,cores=cores,gpu=e!='simai',exclusive=e=='p16');state('start',j['label']);reserved+=mem;used.update(cores);gpu|=e!='simai'
     active[pool.submit(execute,j,cores,mem)]=j;jobs.remove(j)
   c.save(P/'batches-active.json',dict(utc_ns=time.time_ns(),active=[j['label'] for j in active.values()],queued=[j['label'] for j in jobs],budget_gib=BUDGET))
   time.sleep(.2)
 c.save(c.EXPORT/'all-finished.json',dict(utc_ns=time.time_ns(),stage='latency+throughput',policy='parallel-20261116'))

def main():
 os.sched_setaffinity(0,{15})
 with open(c.OUT/'campaign.lock','w') as f:
  fcntl.flock(f,fcntl.LOCK_EX)
  def terminate(signum,frame):
   CANCEL.set()
   ids=sp.check_output(['docker','ps','-q','--filter','label=twins.evidence.campaign=20261116','--filter',f'label=twins.evidence.owner={os.getuid()}','--filter','label=twins.evidence.role=throughput-v2'],text=True).split()
   for cid in ids:sp.run(['docker','stop','-t','3',cid],stdout=sp.DEVNULL,stderr=sp.DEVNULL)
   raise KeyboardInterrupt('signal '+str(signum))
  signal.signal(signal.SIGTERM,terminate)
  old=sp.check_output(['docker','ps','-q','--filter','label=twins.evidence.campaign=20261116','--filter',f'label=twins.evidence.owner={os.getuid()}','--filter','label=twins.evidence.role=throughput-v2'],text=True).split()
  for cid in old:sp.run(['docker','stop','-t','3',cid],check=True,stdout=sp.DEVNULL)
  for p in b.BROOT.glob('*-trial*/state.json'):
   v=b.saved(p)
   if v.get('status')=='running':v.update(status='interrupted',reason='previous controller ended before a completed batch receipt',utc_ns=time.time_ns());c.save(p,v)
  # This service starts only after the latency/gate driver releases the campaign lock.
  run()
if __name__=='__main__':main()
