#!/usr/bin/env python3
"""Durable, resumable single-host evidence campaign; raw traces never enter export/.
Timing ends before validation. Admission is by reserved cap, not observed RSS.
"""
import argparse, concurrent.futures, contextlib, csv, fcntl, hashlib, itertools, json, os, re, shutil, signal, statistics, subprocess as sp, threading, time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'out/paper-evidence-20261116'
M=json.loads((Path(__file__).with_name('matrix.json')).read_text())
JOBS=M['jobs']; BY={x['case']:x for x in JOBS}
BIN=ROOT/'build-evidence'
AIROOT=Path.home()/'Videos/netsim/aitime'
SD=Path.home()/'Videos/netsim/simai-t64'
SIMBIN='/simai/astra-sim-alibabacloud/extern/network_backend/ns3-interface/simulation/build/scratch/ns3.36.1-AstraSimNetwork-optimized'
EXPORT=OUT/'export'; RAW=OUT/'remote-raw'; CACHE=OUT/'validation-cache'
for p in (EXPORT,RAW,CACHE): p.mkdir(parents=True,exist_ok=True)
def sha(p,kind='sha256'):
 h=hashlib.new(kind)
 with open(p,'rb') as f:
  for b in iter(lambda:f.read(8<<20),b''): h.update(b)
 return h.hexdigest()
def save(p,d):
 p.parent.mkdir(parents=True,exist_ok=True); q=p.with_suffix(p.suffix+'.tmp'); q.write_text(json.dumps(d,indent=2)+'\n'); q.replace(p)
def append(p,d):
 with open(p,'a') as f: f.write(json.dumps(d)+'\n'); f.flush(); os.fsync(f.fileno())
def cmdout(c):
 try: return sp.check_output(c,stderr=sp.STDOUT,text=True,timeout=15).strip()
 except Exception as e: return str(e)
def snapshot():
 return dict(utc_ns=time.time_ns(),load=os.getloadavg(),meminfo=Path('/proc/meminfo').read_text(),gpu=cmdout(['nvidia-smi','--query-gpu=utilization.gpu,memory.used,power.draw,clocks.sm,temperature.gpu','--format=csv,noheader,nounits']),gpu_processes=cmdout(['nvidia-smi','--query-compute-apps=pid,process_name,used_memory','--format=csv,noheader,nounits']))
def norm(path,kind):
 """External sort limits memory; exact UID columns and documented DAG normalization."""
 key=sha(path)+'.'+kind; dest=CACHE/key
 if dest.exists(): return dest
 tmp=dest.with_suffix('.unsorted')
 with open(path) as f,open(tmp,'w') as g:
  for line in f:
   if kind=='dag':
    if line.startswith('#'): continue
    line=re.sub(r'^(on [ft][0-9]* m).*',r'\1',line.rstrip())
   else:
    v=line.split()
    if len(v)!=3: raise ValueError(f'{path}: expected UID/start/finish, got {v[:5]}')
    line=' '.join(str(int(x)) for x in v)
   g.write(line.rstrip()+'\n')
 with dest.open('w') as f: sp.run(['sort','-T',str(CACHE),'-S','256M',str(tmp)],stdout=f,check=True,env={**os.environ,'LC_ALL':'C'})
 tmp.unlink(); return dest

def compare(actual,reference,kind):
 a=norm(actual,kind); b=norm(reference,kind)
 na=nb=duplicates=0; last=None; examples=[]
 with a.open() as f,b.open() as g:
  for x,y in itertools.zip_longest(f,g):
   if x is not None:
    na+=1
    if kind=='uid':
     uid=x.split()[0]
     if uid==last: duplicates+=1
     last=uid
   if y is not None: nb+=1
   if x!=y and len(examples)<20: examples.append([x,y])
 h1=sha(a); h2=sha(b)
 result=dict(actual_rows=na,reference_rows=nb,actual_sha256=h1,reference_sha256=h2,duplicate_ids=duplicates,equal=(h1==h2 and na==nb and duplicates==0),examples=examples)
 # New-run normalized outputs are reproducible temporaries, not paper evidence.
 if a!=b: a.unlink()
 return result

def flags(log):
 parts=re.split(r'^.*running again with[^\n]*\n',log,flags=re.M)
 warnings=[x for x in log.splitlines() if re.search('warning:|error:|undecided ties|capacity overflow|fingerprints:|lost tails|restarting|again with',x)]
 return dict(warnings=warnings,present=bool(re.search(r'warning:.*(?:not exact|ties the chains|overflow)|error:',parts[-1])),passes=[dict(index=i+1,diagnostics=[x for x in t.splitlines() if re.search('warning:|error:|undecided ties|overflows:|fingerprints:',x)]) for i,t in enumerate(parts)],internal_retries=len(parts)-1)

def proc_tree(pid):
 ids=[pid]; out=[]
 for q in ids:
  try:
   text=Path(f'/proc/{q}/stat').read_text();v=text[text.rfind(')')+2:].split()
   out.append(dict(pid=q,uticks=int(v[11]),sticks=int(v[12]),processor=int(v[36]),threads=int(v[17])))
   ids.extend(int(x) for x in Path(f'/proc/{q}/task/{q}/children').read_text().split())
  except (OSError,ValueError,IndexError): pass
 return out

def monitor(stop,path,pid,role,container,cpu):
 with path.open('w',buffering=1) as f:
  while not stop.is_set():
   x=snapshot(); x['pid']=pid; x['bound_cpu']=cpu
   hostpid=pid
   if role=='simai':
    with contextlib.suppress(Exception):hostpid=int(cmdout(['docker','inspect',container,'--format','{{.State.Pid}}']))
   x['own_processes']=proc_tree(hostpid)
   # procfs deltas allow later CPU contention audit without exposing other users' command lines.
   x['cpu_stat']=Path('/proc/stat').read_text().split('intr ')[0]
   try: x['process_status']=Path(f'/proc/{pid}/status').read_text()
   except OSError: pass
   f.write(json.dumps(x)+'\n'); stop.wait(5)

def audit_interference(samples,engine,cpu=0):
 """Retrospective indicators, not proof of exclusive access; three consecutive samples."""
 def streak(xs):
  longest=run=0
  for x in xs:run=run+1 if x else 0;longest=max(longest,run)
  return longest
 gpu=[];cpu_busy=[];fractions=[]
 for x in samples:
  own={p['pid'] for p in x.get('own_processes',[])}|{x.get('pid')}
  try:
   foreign=[int(l.split(',')[0]) for l in x['gpu_processes'].splitlines() if l.split(',')[0].strip().isdigit() and int(l.split(',')[0]) not in own]
   gpu.append(engine!='simai' and bool(foreign) and float(x['gpu'].split(',')[0])>20)
  except (KeyError,ValueError,IndexError):gpu.append(False)
 cpus=set(range(17)) if engine=='p16' else {int(cpu)}
 for a,b in zip(samples,samples[1:]):
  def stats(x):return {int(l.split()[0][3:]):list(map(int,l.split()[1:9])) for l in x.get('cpu_stat','').splitlines() if l.split() and l.split()[0][3:].isdigit() and l.startswith('cpu')}
  sa,sb=stats(a),stats(b);total=busy=0
  for core in cpus&sa.keys()&sb.keys():
   d=[y-x for x,y in zip(sa[core],sb[core])];total+=sum(d);busy+=sum(d)-d[3]-d[4]
  own0=sum(p['uticks']+p['sticks'] for p in a.get('own_processes',[]));own1=sum(p['uticks']+p['sticks'] for p in b.get('own_processes',[]))
  fraction=max(0,busy-max(0,own1-own0))/total if total>0 else None
  fractions.append(fraction);cpu_busy.append(fraction is not None and fraction>.20)
 return dict(foreign_gpu_samples_above20=sum(gpu),bound_cpu_samples_above20=sum(cpu_busy),longest_gpu_streak=streak(gpu),longest_cpu_streak=streak(cpu_busy),suspected=streak(gpu)>=3 or streak(cpu_busy)>=3,sampling_period_s=5,bound_cpus=sorted(cpus),cpu_foreign_busy_fractions=fractions,scope='observed foreign GPU presence plus utilization; bound logical-CPU busy minus own process-tree ticks; no SMT isolation guarantee',classifier='consecutive-v2')

def attempt(label,job,engine,options=(),cpu=0,cap=None,repeat=False,resource_context=None):
 d=EXPORT/'attempts'/label
 if (d/'result.json').exists() and not repeat: return json.loads((d/'result.json').read_text())
 if d.exists() and repeat:
  i=1
  while (EXPORT/'attempts'/f'{label}-retry{i}').exists(): i+=1
  d=EXPORT/'attempts'/f'{label}-retry{i}'
 d.mkdir(parents=True,exist_ok=True); raw=RAW/d.name;raw.mkdir(parents=True,exist_ok=True)
 if shutil.disk_usage(OUT).free < 10<<30: raise RuntimeError('less than 10 GiB free; no new run admitted')
 n=job['case'];cap=cap or job['memory_cap_gib'];g=job['gpus']; container='twins-evidence-'+d.name.replace('_','-').lower()
 if engine=='simai':
  etc=raw/'etc';(etc/'simulation').mkdir(parents=True,exist_ok=True);(etc/'results').mkdir(exist_ok=True)
  # Include Docker startup in wall. Internal nanosecond timing and cgroup peak are also retained.
  shell='export AS_SEND_LAT=3; unset TWINS_TRACE TWINS_TXTRACE; s=$(date +%s%N); "$1" -t 1 -w "$2" -n "$3" -c /simai/astra-sim-alibabacloud/inputs/config/SimAI.conf > /run/out.txt 2>/run/err.txt; rc=$?; ns=$(( $(date +%s%N)-s )); printf "rc=%s ns=%s peak=%s cpu_usec=%s\\n" "$rc" "$ns" "$(cat /sys/fs/cgroup/memory.peak 2>/dev/null)" "$(sed -n \'s/^usage_usec //p\' /sys/fs/cgroup/cpu.stat)" >/run/internal.wall; exit "$rc"'
  cmd=['docker','run','--rm','--name',container,'--label','twins.evidence.campaign=20261118','--label',f'twins.evidence.owner={os.getuid()}','--label','twins.evidence.role=isolated-latency','--user',f'{os.getuid()}:{os.getgid()}','--cpuset-cpus',str(cpu),'--memory',f'{cap}g','--memory-swap',f'{cap}g','-v',f'{SD}:/simai:ro','-v',f'{etc}:/etc/astra-sim','-v',f'{AIROOT}:/work:ro','-v',f'{raw}:/run','-w','/run','localhost/simai-build','bash','-c',shell,'_',SIMBIN,f'/work/wl/paper/{n.replace("_hpn","_")}.txt',f'/work/topo/AlibabaHPN_{g}g_8gps_DualToR_SinglePlane_200Gbps_H100']
 else:
  affinity='0-16' if engine=='p16' else str(cpu)
  cmd=['taskset','-c',affinity,str(BIN/'twins-ai'),str(ROOT/'validation/simai/SimAI.conf'),f'--topo={ROOT}/data/simai/hpn{g}.topo',f'--astra={ROOT}/out/simai/wl/paper/{n.replace("_hpn","_")}.txt',f'--out={raw}/messages',f'--dag-out={raw}/actions.dag','--fp-bases=1000003,1000033']
  if engine=='p16': cmd+=['--nvl-islands=16']
  cmd+=list(options)
 actual_inputs=([SD/'astra-sim-alibabacloud/inputs/config/SimAI.conf',AIROOT/f'wl/paper/{n.replace("_hpn","_")}.txt',AIROOT/f'topo/AlibabaHPN_{g}g_8gps_DualToR_SinglePlane_200Gbps_H100'] if engine=='simai' else [ROOT/'validation/simai/SimAI.conf',ROOT/f'data/simai/hpn{g}.topo',ROOT/f'out/simai/wl/paper/{n.replace("_hpn","_")}.txt'])
 before=snapshot(); start_utc=time.time_ns(); start=time.perf_counter_ns(); timedout=False
 env={**os.environ,'AS_SEND_LAT':'3'};env.pop('TWINS_TRACE',None);env.pop('TWINS_TXTRACE',None)
 save(d/'started.json',dict(case=n,engine=engine,command=cmd,start_utc_ns=start_utc,pre=before,timeout_s=M['timeout_s'],resource_context=resource_context,input_sha256={str(x):sha(x) for x in actual_inputs},reference_uid_sha256=sha(ROOT/f'out/simai/paper/{n}.ref'),reference_dag_sha256=sha(ROOT/f'out/simai/paper/{n}.dag')))
 start_utc=time.time_ns();start=time.perf_counter_ns()
 with (d/'process.log').open('w') as f:
  p=sp.Popen(cmd,cwd=raw,stdout=f,stderr=sp.STDOUT,env=env,start_new_session=True)
  stop=threading.Event();t=threading.Thread(target=monitor,args=(stop,d/'resources.jsonl',p.pid,engine,container,cpu),daemon=True);t.start()
  try: rc=p.wait(timeout=M['timeout_s'])
  except sp.TimeoutExpired:
   timedout=True
   if engine=='simai': sp.run(['docker','stop','-t','5',container],stdout=sp.DEVNULL,stderr=sp.DEVNULL)
   with contextlib.suppress(ProcessLookupError): os.killpg(p.pid,signal.SIGTERM)
   try:p.wait(10)
   except sp.TimeoutExpired:
    with contextlib.suppress(ProcessLookupError): os.killpg(p.pid,signal.SIGKILL)
    p.wait()
   rc=124
  elapsed=time.perf_counter_ns()-start; end_utc=time.time_ns();stop.set();t.join(20)
 # Validation is outside timed interval, and outside the next run's interval in latency mode.
 result=dict(label=d.name,case=n,engine=engine,options=list(options),command=cmd,start_utc_ns=start_utc,end_utc_ns=end_utc,wall_ns=elapsed,rc=rc,timeout=timedout,memory_cap_gib=cap if engine=='simai' else None,affinity=str(cpu) if engine=='simai' else affinity,requested_island_workers=16 if engine=='p16' else 0,post=snapshot(),binary_sha256=sha(SD/SIMBIN.removeprefix('/simai/')) if engine=='simai' else sha(BIN/'twins-ai'))
 result['resource_context']=resource_context
 try:
  if engine=='simai':
   fct=raw/'etc/simulation/llama_hpn7_fct.txt'
   result['validation']=dict(expected_md5=job['expected_md5'],actual_md5=sha(fct,'md5'),sha256=sha(fct),rows=sum(1 for _ in fct.open()),fields='entire reference FCT file, byte for byte MD5 from validated p64 campaign')
   result['validation']['equal']=result['validation']['actual_md5']==job['expected_md5']
   for name in ('out.txt','err.txt','internal.wall'):
    if (raw/name).exists(): shutil.copy2(raw/name,d/name)
  else:
   result['validation']=compare(raw/'messages',ROOT/f'out/simai/paper/{n}.ref','uid')
   result['dag_validation']=compare(raw/'actions.dag',ROOT/f'out/simai/paper/{n}.dag','dag')
   result['dag_normalization']='drop comments; on [ft][0-9]* m... -> on [ft][0-9]* m; LC_ALL=C sort'
 except Exception as e: result['validation']=dict(equal=False,error=str(e))
 log=(d/'process.log').read_text(errors='replace');result['flags']=flags(log)
 if engine=='simai' and (d/'err.txt').exists(): result['flags']=flags((d/'err.txt').read_text(errors='replace'))
 # A foreign compute PID is evidence of potential competition, not a reason to wait forever.
 samples=[json.loads(x) for x in (d/'resources.jsonl').read_text().splitlines()]
 result['resource_samples']=len(samples)
 result['interference']=audit_interference(samples,engine,cpu)

 result['accepted']=bool(rc==0 and result['validation'].get('equal') and result.get('dag_validation',{'equal':True})['equal'] and not result['flags']['present'])
 result['status']='timeout' if timedout else ('accepted' if result['accepted'] else 'failed_or_flagged')
 # Compact system-layer iteration summary is required by the new MTP comparison.
 for summary in raw.glob('*EndToEnd*.csv'):
  if summary.stat().st_size<10<<20:shutil.copy2(summary,d/summary.name)
 save(d/'result.json',result);append(EXPORT/'attempts.jsonl',result)
 # Retain hashes and failure examples. Successful bulk traces and generated SimAI text are discarded.
 if result['accepted']:
  # New isolated campaign: keep one sequential FCT only as the temporary MTP comparison cache.
  # This happens after timing/validation, is not a trace, and never changes a measurement.
  keep=os.environ.get('TWINS_KEEP_REFERENCE_FCT')
  if engine=='simai' and keep and n in ('c_allgather_8m_hpn128','gpt3_13b_full_hpn128','llama_65b_full_hpn128','gpt3_175b_full_hpn128'):
   target=Path(keep)/(n+'.fct');target.parent.mkdir(parents=True,exist_ok=True)
   if not target.exists():shutil.copy2(raw/'etc/simulation/llama_hpn7_fct.txt',target)
  for file in [raw/'messages',raw/'actions.dag',raw/'etc/simulation/llama_hpn7_fct.txt']:
   with contextlib.suppress(FileNotFoundError):file.unlink()
 print(json.dumps({k:result[k] for k in ('label','case','engine','wall_ns','status')}),flush=True)
 return result

def calibrate():
 j=JOBS[0]
 for e in ('simai','p0','p16'): attempt('calibration-'+e,j,e)
 # Fixed bases: output/flags are the reproducibility contract, not atomic counters or interleaving.
 attempt('fixed-bases-p0-repeat',j,'p0')
 results=[json.loads(p.read_text()) for p in (EXPORT/'attempts').glob('calibration-*/result.json')]
 if len(results)!=3 or not all(x['accepted'] for x in results): raise RuntimeError('calibration failed; formal campaign not started')

def latency():
 for r in range(3):
  # Rotate both case order and engine order; deterministic predeclared schedule.
  for ji in range(8):
   j=JOBS[(ji+r)%8]; engines=['simai','p0','p16']; engines=engines[(r+ji)%3:]+engines[:(r+ji)%3]
   for e in engines: attempt(f'latency-r{r+1}-{j["case"]}-{e}',j,e)
 # Supplement unstable triples with two further attempts, keeping original observations.
 for j in JOBS:
  for e in ('simai','p0','p16'):
   xs=[json.loads((EXPORT/f'attempts/latency-r{r}-{j["case"]}-{e}/result.json').read_text()) for r in (1,2,3)]
   vals=[x['wall_ns'] for x in xs if x['accepted']]
   if len(vals)<3 or (max(vals)-min(vals))/statistics.median(vals)>M['variation_threshold'] or any(x.get('interference',{}).get('suspected') for x in xs):
    for r in (4,5):attempt(f'latency-r{r}-{j["case"]}-{e}',j,e)
 for r in range(1,4):
  for e,opts in [('serial',['--serial']),('gwin0',['--gwin=0']),('both',['--serial','--gwin=0'])]:
   attempt(f'ablation-r{r}-{e}',JOBS[1],'p0',opts)

def batch_cpu(roundno,concurrency):
 label=f'batch-cpu-r{roundno}-c{concurrency}'; receipt=EXPORT/'batches'/f'{label}.json'
 if receipt.exists():return
 queue=sorted(JOBS,key=lambda j:-j['old_simai_s']);running={};done=[];reserved=0;peak=0;start=time.perf_counter_ns();utc=time.time_ns();events=[]
 with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
  while queue or running:
   for j in list(queue):
    if len(running)>=concurrency:break
    if reserved+j['memory_cap_gib']>M['batch_budget_gib']:continue
    used={info[1] for info in running.values()};cpu=next(x for x in range(16) if x not in used)
    queue.remove(j);reserved+=j['memory_cap_gib'];f=pool.submit(attempt,label+'-'+j['case'],j,'simai',cpu=cpu)
    running[f]=(j,cpu);peak=max(peak,len(running));events.append(dict(ns=time.perf_counter_ns()-start,action='admit',case=j['case'],cpu=cpu,reserved_gib=reserved,active=len(running)))
   if not running:raise RuntimeError('no job fits memory budget')
   finished,_=concurrent.futures.wait(running,return_when=concurrent.futures.FIRST_COMPLETED)
   for f in finished:
    j,cpu=running.pop(f);reserved-=j['memory_cap_gib'];v=f.result();done.append(v);events.append(dict(ns=time.perf_counter_ns()-start,action='finish',case=j['case'],reserved_gib=reserved,active=len(running)))
 save(receipt,dict(label=label,start_utc_ns=utc,makespan_ns=time.perf_counter_ns()-start,requested_concurrency=concurrency,peak_actual_concurrency=peak,budget_gib=70,events=events,attempts=[x['label'] for x in done],all_accepted=all(x['accepted'] for x in done),timing_scope='startup+execution+validation+admission; validation is charged to batch'))

def throughput():
 for r in range(1,4):
  for c in M['batch_concurrency']:batch_cpu(r,c)
  for e in ('p0','p16'):
   label=f'batch-{e}-r{r}';p=EXPORT/'batches'/f'{label}.json'
   if p.exists():continue
   start=time.perf_counter_ns();utc=time.time_ns();xs=[]
   for j in sorted(JOBS,key=lambda x:-x['old_simai_s']):xs.append(attempt(label+'-'+j['case'],j,e))
   save(p,dict(label=label,start_utc_ns=utc,makespan_ns=time.perf_counter_ns()-start,attempts=[x['label'] for x in xs],all_accepted=all(x['accepted'] for x in xs),timing_scope='startup+execution+validation+admission; validation charged equally to CPU/GPU'))

def main():
 p=argparse.ArgumentParser();p.add_argument('stage',choices=['calibrate','latency','throughput','all']);a=p.parse_args()
 with open(OUT/'campaign.lock','w') as lock:
  fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  save(EXPORT/'campaign.json',dict(matrix=M,source_sha256={str(x.relative_to(ROOT)):sha(x) for x in sorted((ROOT/'src').rglob('*')) if x.is_file()},host=cmdout(['uname','-a']),cpu=cmdout(['lscpu']),docker_image=cmdout(['docker','image','inspect','localhost/simai-build','--format','{{.Id}}']),started_utc_ns=time.time_ns()))
  if a.stage in ('calibrate','all'):calibrate()
  if a.stage in ('latency','all'):latency()
  if a.stage in ('throughput','all'):throughput()
  save(EXPORT/f'{a.stage}-finished.json',dict(utc_ns=time.time_ns(),stage=a.stage))
if __name__=='__main__':main()
