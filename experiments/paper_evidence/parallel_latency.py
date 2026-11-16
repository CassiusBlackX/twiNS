#!/usr/bin/env python3
"""User-authorized parallel reference measurements; unchanged models, explicit resource conditions."""
import concurrent.futures as cf,contextlib,fcntl,json,math,os,re,signal,subprocess as sp,time
from pathlib import Path
import campaign as c
P=c.EXPORT/'parallel';P.mkdir(exist_ok=True)
BUDGET=70;GPU_HOST=10;CPU_CORES=[8,4,12]
def read(p):return json.loads(p.read_text())
def caps():
 rows={}
 for j in c.JOBS:
  peaks=[]
  for d in (c.EXPORT/'attempts').glob('latency-r*-'+j['case']+'-simai'):
   f=d/'internal.wall'
   if f.exists() and (d/'result.json').exists() and read(d/'result.json')['accepted']:
    m=re.search(r'peak=(\d+)',f.read_text())
    if m:peaks.append(int(m[1]))
  cap=max(1,math.ceil((max(peaks)*1.10+(512<<20))/2**30)) if peaks else j['memory_cap_gib']
  rows[j['case']]=dict(cap_gib=cap,accepted_observed_peaks=peaks)
 c.save(P/'latency-memory-policy.json',dict(utc_ns=time.time_ns(),budget_gib=BUDGET,gpu_host_reservation_gib=GPU_HOST,jobs=rows,scope='new parallel attempts only; old cgroup limits remain in old receipts'))
 return rows

def available_gib():
 m=re.search(r'^MemAvailable:\s+(\d+)',Path('/proc/meminfo').read_text(),re.M);return int(m[1])/2**20

def task(label,j,e,opts=()):return dict(label=label,job=j,engine=e,options=opts)
def result_exists(t):return (c.EXPORT/'attempts'/t['label']/'result.json').exists()
def original_tasks():return [task(f'latency-r{r}-{j["case"]}-{e}',j,e) for r in (1,2,3) for j in c.JOBS for e in ('simai','p0','p16')]

def stop_old(current,managed):
 sp.run(['systemctl','--user','stop','twins-evidence-campaign'],check=False)
 sp.run(['systemctl','--user','stop','twins-evidence-after-latency'],check=False)
 for d in (c.EXPORT/'attempts').iterdir():
  if d.name in managed or not (d/'started.json').exists() or (d/'result.json').exists():continue
  name='twins-evidence-'+d.name.replace('_','-').lower()
  info=sp.run(['docker','inspect',name],text=True,capture_output=True)
  if info.returncode==0:
   v=json.loads(info.stdout)[0]
   if any(Path(m.get('Source','/')).is_relative_to(c.RAW) for m in v.get('Mounts',[])):sp.run(['docker','stop','-t','3',name],check=True,stdout=sp.DEVNULL)
  c.save(d/'preempted.json',dict(utc_ns=time.time_ns(),reason='parallel handoff after the preceding measurement completed; this partial timing is excluded'))
  dest=c.EXPORT/'preempted'/(d.name+'-parallel-handoff-'+str(time.time_ns()));dest.parent.mkdir(exist_ok=True);d.rename(dest)
  raw=c.RAW/d.name
  if raw.exists():raw.rename(c.RAW/(dest.name))
 if current:
  p=c.EXPORT/'attempts'/current/'result.json';v=read(p);v['resource_context']=dict(policy='parallel-20261116',co_run_allowed=True,note='began before parallel handoff; overlapping CPU reference start is recorded in parallel/dispatch.jsonl');c.save(p,v)


def reclassify():
 for p in (c.EXPORT/'attempts').glob('latency-*/result.json'):
  v=read(p);log=p.parent/('err.txt' if v['engine']=='simai' else 'process.log')
  if log.exists():
   f=c.flags(log.read_text(errors='replace'))
   if v.get('flags')!=f:v.setdefault('original_flag_classification',v.get('flags'));v['flags']=f
  resources=p.parent/'resources.jsonl'
  if resources.exists():
   samples=[json.loads(l) for l in resources.read_text().splitlines() if l.strip()]
   v.setdefault('original_interference_classification',v.get('interference'));v['interference']=c.audit_interference(samples,v['engine'],int(v['affinity'].split('-')[0]))
  v['accepted']=bool(v['rc']==0 and v['validation'].get('equal') and v.get('dag_validation',{'equal':True})['equal'] and not v['flags']['present']);c.save(p,v)


def phase(tasks,policy,current=None):
 """CPU refs on disjoint physical cores, one P0 on core0; P16 only after CPU refs drain."""
 queued=[t for t in tasks if not result_exists(t) and t['label']!=current];active={};managed=set();old=current;handoff=not bool(current)
 if old:
  start=read(c.EXPORT/'attempts'/old/'started.json');old_engine=start['engine'];oldcap=next(int(x[:-1]) for i,x in enumerate(start['command']) if start['command'][i-1:i]==['--memory']) if old_engine=='simai' else GPU_HOST
 else:oldcap=0;old_engine=None
 with cf.ThreadPoolExecutor(max_workers=4) as pool:
  while queued or active or not handoff:
   if not handoff and (c.EXPORT/'attempts'/old/'result.json').exists():
    stop_old(old,managed);handoff=True;oldcap=0
   for fut in list(active):
    if fut.done():
     t=active.pop(fut);v=fut.result();print(json.dumps(dict(completed=t['label'],seconds=v['wall_ns']/1e9,accepted=v['accepted'])),flush=True)
   mem=sum(t['reserve'] for t in active.values())+oldcap
   used={t['cpu'] for t in active.values() if t['engine']=='simai'}
   busy_gpu=any(t['engine']!='simai' for t in active.values())
   # Preserve GPU P0's original core0; a live old reference also owns core0.
   candidates=sorted(queued,key=lambda t:(0 if handoff and t['engine']=='p0' else 1,-t['job']['old_simai_s'],t['label']))
   for t in candidates:
    e=t['engine']
    if e=='p16':continue
    if not handoff and old_engine!='simai':continue
    if e=='p0' and (not handoff or busy_gpu):continue
    if e=='simai' and len(used)>=len(CPU_CORES):continue
    reserve=policy[t['job']['case']]['cap_gib'] if e=='simai' else GPU_HOST
    if mem+reserve>BUDGET or available_gib()<reserve+8:continue
    cpu=next(i for i in CPU_CORES if i not in used) if e=='simai' else 0
    ctx=dict(policy='parallel-20261116',co_run_allowed=True,physical_cpu=cpu,total_reserved_budget_gib=BUDGET,gpu_host_reservation_gib=GPU_HOST,state_log='export/parallel/dispatch.jsonl',note='other physical cores may run independent references; shared RAM bandwidth is not isolated')
    managed.add(t['label']);t.update(cpu=cpu,reserve=reserve);queued.remove(t)
    future=pool.submit(c.attempt,t['label'],t['job'],e,t['options'],cpu,reserve if e=='simai' else None,False,ctx);active[future]=t;mem+=reserve
    if e=='simai':used.add(cpu)
    else:busy_gpu=True
    c.append(P/'dispatch.jsonl',dict(utc_ns=time.time_ns(),action='start',label=t['label'],engine=e,core=cpu,reserved_gib=reserve,total_reserved_gib=mem,old_measurement=old if not handoff else None,peers=[v['label'] for v in active.values()]))
   if handoff and not active and all(t['engine']=='p16' for t in queued):break
   if handoff and not active and queued and available_gib()<max(8,min(policy[t['job']['case']]['cap_gib'] if t['engine']=='simai' else GPU_HOST for t in queued))+8:
    c.save(P/'admission-wait.json',dict(utc_ns=time.time_ns(),reason='physical MemAvailable guard',available_gib=available_gib()))
   c.save(P/'active.json',dict(utc_ns=time.time_ns(),old_measurement=old if not handoff else None,active=[dict(label=t['label'],engine=t['engine'],cpu=t['cpu'],reserve_gib=t['reserve']) for t in active.values()],queued=len(queued),reserved_gib=mem,budget_gib=BUDGET))
   time.sleep(.1)
  for t in sorted(queued,key=lambda t:t['label']):
   assert t['engine']=='p16'
   c.attempt(t['label'],t['job'],'p16',t['options'],resource_context=dict(policy='parallel-20261116',co_run_allowed=False,note='P16 keeps original0-16 affinity; no own CPU references overlap'))


def main():
 import argparse
 parser=argparse.ArgumentParser();parser.add_argument('--handoff-after');a=parser.parse_args()
 os.sched_setaffinity(0,{15}) # scheduler/validation/observer work; model processes have explicit masks
 with open(c.OUT/'parallel.lock','w') as lock:
  fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  def terminate(signum,frame):
   state=read(P/'active.json') if (P/'active.json').exists() else {}
   for t in state.get('active',[]):
    if t['engine']=='simai':sp.run(['docker','stop','-t','3','twins-evidence-'+t['label'].replace('_','-').lower()],stdout=sp.DEVNULL,stderr=sp.DEVNULL)
   raise KeyboardInterrupt('signal '+str(signum))
  signal.signal(signal.SIGTERM,terminate)
  sp.run(['systemctl','--user','stop','twins-evidence-after-latency'],check=False)
  policy=caps();c.save(P/'plan.json',dict(utc_ns=time.time_ns(),cpu_reference_cores=CPU_CORES,gpu_p0_core=0,p16_affinity='0-16 exclusive',budget_gib=BUDGET,handoff_after=a.handoff_after,scope='same frozen binaries/inputs/output checks; resource epoch and co-runners explicitly recorded'))
  phase(original_tasks(),policy,a.handoff_after)
  # Old campaign is stopped before the exclusive campaign lock is taken.
  with open(c.OUT/'campaign.lock','w') as campaign_lock:
   fcntl.flock(campaign_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
   reclassify();extra=[]
   for j in c.JOBS:
    for e in ('simai','p0','p16'):
     xs=[read(c.EXPORT/f'attempts/latency-r{r}-{j["case"]}-{e}/result.json') for r in (1,2,3)];vals=[x['wall_ns'] for x in xs if x['accepted']]
     if len(vals)<3 or (max(vals)-min(vals))/__import__('statistics').median(vals)>.10 or any(x.get('interference',{}).get('suspected') for x in xs):
      extra += [task(f'latency-r{r}-{j["case"]}-{e}',j,e) for r in (4,5)]
   phase(extra,policy)
   for r in (1,2,3):
    for name,opts in [('serial',['--serial']),('gwin0',['--gwin=0']),('both',['--serial','--gwin=0'])]:c.attempt(f'ablation-r{r}-{name}',c.JOBS[1],'p0',opts,resource_context=dict(policy='parallel-20261116',co_run_allowed=False))
   c.save(c.EXPORT/'latency-finished.json',dict(utc_ns=time.time_ns(),policy='parallel-20261116'))
   sp.run(['python3','experiments/paper_evidence/supplement_provenance.py'],cwd=c.ROOT,check=True)
   sp.run(['python3','experiments/paper_evidence/cuda_gates.py'],cwd=c.ROOT,check=False)
  sp.run(['systemd-run','--user','--unit=twins-evidence-batches','--property=MemoryMax=80G','--working-directory='+str(c.ROOT),'python3','experiments/paper_evidence/parallel_batches.py'],check=True)
if __name__=='__main__':main()
