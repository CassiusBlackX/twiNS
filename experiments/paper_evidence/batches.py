#!/usr/bin/env python3
"""Actual batch execution: provenance before timer, validation after makespan.
Interrupted batches are retained and entirely repeated; no sum of selected job times.
"""
import concurrent.futures,contextlib,hashlib,json,math,os,signal,subprocess as sp,threading,time
from pathlib import Path
import campaign as c
ROOT=c.ROOT;OUT=c.OUT;EXP=c.EXPORT;RAW=c.RAW;M=c.M
BROOT=EXP/'batches-v2';BROOT.mkdir(exist_ok=True)
def saved(p):return json.loads(p.read_text())
def cap_manifest():
 p=BROOT/'memory-policy.json'
 if p.exists():return saved(p)
 rows={}
 for j in c.JOBS:
  peaks=[]
  for d in (EXP/'attempts').glob('latency-r*-'+j['case']+'-simai'):
   f=d/'internal.wall'
   if f.exists() and (d/'result.json').exists() and saved(d/'result.json')['accepted']:
    import re
    m=re.search(r'peak=(\d+)',f.read_text())
    if m:peaks.append(int(m[1]))
  cap=max(1,math.ceil((max(peaks)*1.10+(512<<20))/(1<<30))) if peaks else j['memory_cap_gib']
  rows[j['case']]=dict(cap_gib=cap,observed_peaks_bytes=peaks,fallback_historical_cap_gib=j['memory_cap_gib'])
 d=dict(policy='ceil(1.10 * max accepted A3 cgroup peak + 512MiB), minimum 1GiB; same fixed caps in all batches',total_budget_gib=70,jobs=rows)
 c.save(p,d);return d

def prepare(trial,j,engine,cpu,policy):
 name=j['case'];d=trial/name;d.mkdir(exist_ok=True);raw=RAW/(trial.name+'-'+name);raw.mkdir(exist_ok=True);g=j['gpus'];container='twins-batch-'+trial.name.replace('_','-')+'-'+name.replace('_','-');cap=policy['jobs'][name]['cap_gib']
 if engine=='simai':
  etc=raw/'etc';(etc/'simulation').mkdir(parents=True,exist_ok=True);(etc/'results').mkdir(exist_ok=True)
  shell='export AS_SEND_LAT=3; unset TWINS_TRACE TWINS_TXTRACE; s=$(date +%s%N); "$1" -t 1 -w "$2" -n "$3" -c /simai/astra-sim-alibabacloud/inputs/config/SimAI.conf > /run/out.txt 2>/run/err.txt; rc=$?; ns=$(( $(date +%s%N)-s )); printf "rc=%s ns=%s peak=%s\\n" "$rc" "$ns" "$(cat /sys/fs/cgroup/memory.peak 2>/dev/null)" >/run/internal.wall; exit "$rc"'
  cmd=['docker','run','--rm','--name',container,'--label','twins.evidence.campaign=20261116','--label',f'twins.evidence.owner={os.getuid()}','--label','twins.evidence.role=throughput-v2','--user',f'{os.getuid()}:{os.getgid()}','--cpuset-cpus',str(cpu),'--memory',f'{cap}g','--memory-swap',f'{cap}g','-v',f'{c.SD}:/simai:ro','-v',f'{etc}:/etc/astra-sim','-v',f'{c.AIROOT}:/work:ro','-v',f'{raw}:/run','-w','/run','localhost/simai-build','bash','-c',shell,'_',c.SIMBIN,f'/work/wl/paper/{name.replace("_hpn","_")}.txt',f'/work/topo/AlibabaHPN_{g}g_8gps_DualToR_SinglePlane_200Gbps_H100']
  bin=c.SD/c.SIMBIN.removeprefix('/simai/');affinity=str(cpu)
 else:
  bin=c.BIN/'twins-ai';affinity='0-16' if engine=='p16' else '0'
  cmd=['taskset','-c',affinity,str(bin),str(ROOT/'validation/simai/SimAI.conf'),f'--topo={ROOT}/data/simai/hpn{g}.topo',f'--astra={ROOT}/out/simai/wl/paper/{name.replace("_hpn","_")}.txt',f'--out={raw}/messages',f'--dag-out={raw}/actions.dag','--fp-bases=1000003,1000033']
  if engine=='p16':cmd+=['--nvl-islands=16']
 inputs=([c.SD/'astra-sim-alibabacloud/inputs/config/SimAI.conf',c.AIROOT/f'wl/paper/{name.replace("_hpn","_")}.txt',c.AIROOT/f'topo/AlibabaHPN_{g}g_8gps_DualToR_SinglePlane_200Gbps_H100'] if engine=='simai' else [ROOT/'validation/simai/SimAI.conf',ROOT/f'data/simai/hpn{g}.topo',ROOT/f'out/simai/wl/paper/{name.replace("_hpn","_")}.txt'])
 v=dict(case=name,engine=engine,command=cmd,binary_sha256=c.sha(bin),affinity=affinity,cap_gib=cap if engine=='simai' else None,reference_uid_sha256=c.sha(ROOT/f'out/simai/paper/{name}.ref'),reference_dag_sha256=c.sha(ROOT/f'out/simai/paper/{name}.dag'),input_sha256={str(p):c.sha(p) for p in inputs})
 c.save(d/'prepared.json',v);return dict(j=j,d=d,raw=raw,container=container,v=v)

def launch(task):
 start=time.perf_counter_ns();utc=time.time_ns();log=(task['d']/'process.log').open('w');p=sp.Popen(task['v']['command'],stdout=log,stderr=sp.STDOUT,cwd=task['raw'],start_new_session=True,env={**os.environ,'AS_SEND_LAT':'3'})
 task.update(p=p,log=log,start=start,start_utc_ns=utc);return task

def stop(task):
 if task['v']['engine']=='simai':sp.run(['docker','stop','-t','5',task['container']],stdout=sp.DEVNULL,stderr=sp.DEVNULL)
 with contextlib.suppress(ProcessLookupError):os.killpg(task['p'].pid,signal.SIGTERM)
 try:task['p'].wait(5)
 except sp.TimeoutExpired:
  with contextlib.suppress(ProcessLookupError):os.killpg(task['p'].pid,signal.SIGKILL)
  task['p'].wait()

def validate(task):
 v=task['v'];raw=task['raw'];d=task['d'];name=v['case'];engine=v['engine']
 try:
  if engine=='simai':
   for file in ('out.txt','err.txt','internal.wall'):
    if (raw/file).exists():sp.run(['cp',str(raw/file),str(d/file)],check=True)
   fct=raw/'etc/simulation/llama_hpn7_fct.txt';actual=c.sha(fct,'md5')
   validation=dict(expected_md5=task['j']['expected_md5'],actual_md5=actual,sha256=c.sha(fct),rows=sum(1 for _ in fct.open()),equal=actual==task['j']['expected_md5'])
   log=(d/'err.txt').read_text(errors='replace') if (d/'err.txt').exists() else ''
  else:
   validation=c.compare(raw/'messages',ROOT/f'out/simai/paper/{name}.ref','uid');v['dag_validation']=c.compare(raw/'actions.dag',ROOT/f'out/simai/paper/{name}.dag','dag');log=(d/'process.log').read_text(errors='replace')
 except Exception as e:validation=dict(equal=False,error=str(e));log=(d/'process.log').read_text(errors='replace')
 v.update(validation=validation,flags=c.flags(log));v['accepted']=v['rc']==0 and validation['equal'] and v.get('dag_validation',{'equal':True})['equal'] and not v['flags']['present']
 c.save(d/'result.json',v)
 # Full matching outputs and stub artifacts are regenerable; raw failures remain for diagnosis.
 if v['accepted']:
  import shutil
  if raw.parent.resolve()!=RAW.resolve():raise RuntimeError('unexpected raw directory')
  shutil.rmtree(raw)
 return v

def batch(label,engine,concurrency,policy,cpu_pool=None,resource_context=None,reservation_update=None,cancel_event=None):
 receipt=BROOT/(label+'.json')
 if receipt.exists():return
 cpu_pool=list(range(16)) if cpu_pool is None else list(cpu_pool)
 if __import__('shutil').disk_usage(OUT).free<10<<30:raise RuntimeError('less than 10GiB free before batch')
 trialno=1
 while (BROOT/f'{label}-trial{trialno}').exists():trialno+=1
 trial=BROOT/f'{label}-trial{trialno}';trial.mkdir();queue=[]
 # All configuration, input and reference hashes are computed before the batch timer.
 jobs=sorted(c.JOBS,key=lambda j:-j['old_simai_s'])
 for j in jobs:queue.append(prepare(trial,j,engine,0,policy))
 c.save(trial/'state.json',dict(status='running',utc_ns=time.time_ns(),engine=engine,target_concurrency=concurrency,policy=policy,timing_scope='Docker/process startup + stub + execution + admission; provenance and validation excluded'))
 active=[];done=[];reserved=0;peak=0;events=[];stopmon=threading.Event()
 def monitor():
  with (trial/'resources.jsonl').open('w',buffering=1) as f:
   while not stopmon.is_set():
    tasks=list(active);processes={}
    for t in tasks:
     pid=t['p'].pid
     if engine=='simai':
      with contextlib.suppress(ValueError):pid=int(c.cmdout(['docker','inspect',t['container'],'--format','{{.State.Pid}}']))
     processes[t['v']['case']]=c.proc_tree(pid)
    f.write(json.dumps(dict(**c.snapshot(),active=[t['v']['case'] for t in tasks],reserved_gib=reserved,own_processes_by_job=processes,cpu_stat=Path('/proc/stat').read_text().split('intr ')[0]))+'\n');stopmon.wait(5)
 thread=threading.Thread(target=monitor);thread.start();start=time.perf_counter_ns();utc=time.time_ns()
 try:
  while queue or active:
   if cancel_event is not None and cancel_event.is_set():raise InterruptedError('parallel controller cancelled the batch')
   for task in list(queue):
    if len(active)>=concurrency:break
    cap=policy['jobs'][task['j']['case']]['cap_gib'] if engine=='simai' else 0
    if engine=='simai' and reserved+cap>70:continue
    cpu=next(i for i in cpu_pool if i not in [t['cpu'] for t in active]);task['cpu']=cpu
    if engine=='simai':
     ix=task['v']['command'].index('--cpuset-cpus');task['v']['command'][ix+1]=str(cpu);task['v']['affinity']=str(cpu)
    task=launch(task);queue.remove(task);active.append(task);reserved+=cap;peak=max(peak,len(active));events.append(dict(ns=time.perf_counter_ns()-start,action='admit',case=task['j']['case'],cpu=cpu,reserved_gib=reserved,active=len(active)))
   if not active:raise RuntimeError('no job fits memory budget')
   now=time.perf_counter_ns()
   for task in list(active):
    timedout=now-task['start']>M['timeout_s']*1000000000
    if task['p'].poll() is None and not timedout:continue
    if timedout:stop(task)
    end=time.perf_counter_ns();task['v'].update(start_utc_ns=task['start_utc_ns'],end_utc_ns=time.time_ns(),wall_ns=end-task['start'],rc=124 if timedout else task['p'].returncode,timeout=timedout)
    task['log'].close();active.remove(task);done.append(task)
    if engine=='simai':reserved-=policy['jobs'][task['j']['case']]['cap_gib']
    events.append(dict(ns=end-start,action='finish',case=task['j']['case'],reserved_gib=reserved,active=len(active)))
   if reservation_update and engine=='simai':
    remaining=queue+active
    # Bound every possible future admission; releasing this quota cannot delay this batch.
    bound=min(70,sum(sorted((policy['jobs'][t['j']['case']]['cap_gib'] for t in remaining),reverse=True)[:concurrency]))
    keep=[t['cpu'] for t in active]
    keep += [i for i in cpu_pool if i not in keep][:max(0,min(concurrency,len(remaining))-len(keep))]
    reservation_update(bound,keep)
   if active:time.sleep(.01)
  elapsed=time.perf_counter_ns()-start
 except BaseException as exc:
  c.save(trial/'state.json',dict(status='interrupted',utc_ns=time.time_ns(),reason=repr(exc),completed=[t['v'] for t in done],pending=[t['v']['case'] for t in queue],active=[t['v']['case'] for t in active],meaning='not a completed batch timing; entire batch will be repeated'))
  raise
 finally:
  stopmon.set();thread.join(20)
  for task in active:stop(task);task['log'].close()
 validation_start=time.perf_counter_ns();results=[validate(t) for t in done];validation_ns=time.perf_counter_ns()-validation_start
 result=dict(label=label,trial=trial.name,engine=engine,start_utc_ns=utc,makespan_ns=elapsed,validation_ns=validation_ns,target_concurrency=concurrency,peak_actual_concurrency=peak,budget_gib=70,events=events,all_accepted=all(v['accepted'] for v in results),jobs=[v['case'] for v in results],timing_scope='startup + stub + execution + admission; validation/provenance outside batch timer')
 result.update(cpu_pool=cpu_pool,resource_context=resource_context)
 c.save(receipt,result);c.save(trial/'state.json',dict(status='completed',result=result));print(json.dumps(result),flush=True)

def main():
 with open(OUT/'campaign.lock','w') as lock:
  import fcntl
  fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  def terminate(signum,frame):raise KeyboardInterrupt('received signal '+str(signum))
  signal.signal(signal.SIGTERM,terminate)
  # A crashed previous driver may leave Docker alive outside its process cgroup.
  # Labels scope cleanup to this user's own throughput campaign after taking its lock.
  old=sp.check_output(['docker','ps','-q','--filter','label=twins.evidence.campaign=20261116','--filter',f'label=twins.evidence.owner={os.getuid()}','--filter','label=twins.evidence.role=throughput-v2'],text=True).split()
  for container in old:sp.run(['docker','stop','-t','5',container],check=True,stdout=sp.DEVNULL)
  for p in BROOT.glob('*-trial*/state.json'):
   v=saved(p)
   if v.get('status')=='running':v.update(status='interrupted',reason='previous driver ended before completed receipt',utc_ns=time.time_ns());c.save(p,v)
  policy=cap_manifest()
  for r in range(1,4):
   for con in (1,2,4,8):batch(f'cpu-r{r}-c{con}','simai',con,policy)
   for e in ('p0','p16'):batch(f'{e}-r{r}',e,1,policy)
  c.save(EXP/'all-finished.json',dict(utc_ns=time.time_ns(),stage='latency+throughput'))
if __name__=='__main__':main()
