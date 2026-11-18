"""Paths and exclusive-setting measurement helpers for the 2026-11-18 campaign."""
import contextlib,fcntl,hashlib,json,os,re,resource,shutil,signal,subprocess as sp,sys,threading,time
from pathlib import Path
HOME=Path.home();ROOT=Path(__file__).resolve().parent.parent
E=ROOT/'out';EXPORT=E/'export';RAW=E/'raw';INPUT=E/'inputs'
BASE=Path(os.environ.get('TWINS_EVIDENCE_BASE',str(HOME/'Videos/twins-paper-evidence-20261116')))
for p in (EXPORT,RAW,E/'receipts'):p.mkdir(parents=True,exist_ok=True)
sys.path.insert(0,str(Path(__file__).with_name('reuse')))
import campaign as c
c.ROOT=BASE;c.OUT=E;c.EXPORT=EXPORT;c.RAW=RAW;c.CACHE=E/'validation-cache';c.CACHE.mkdir(exist_ok=True);c.BIN=BASE/'candidate/build-default'
import batches as b
b.ROOT=BASE;b.OUT=E;b.EXP=EXPORT;b.RAW=RAW;b.BROOT=EXPORT/'ai-batches';b.BROOT.mkdir(exist_ok=True)
MANIFEST=json.loads((E/'manifest.json').read_text())
resource.setrlimit(resource.RLIMIT_CORE,(0,0))
save=c.save;sha=c.sha;read=lambda p:json.loads(Path(p).read_text())
_snapshot=c.snapshot
def cgroup_usage(pid=None):
 try:
  path=Path(f'/proc/{pid or os.getpid()}/cgroup').read_text().split('0::',1)[1].splitlines()[0];vals=dict(l.split() for l in (Path('/sys/fs/cgroup')/path.lstrip('/')/'cpu.stat').read_text().splitlines());return int(vals['usage_usec'])
 except (OSError,ValueError,KeyError,IndexError):return None
c.cgroup_usage=cgroup_usage
def account_batch_cpu(sample,tasks):
 total=sample.get('controller_cpu_usage_usec');complete=total is not None
 for task in tasks:
  if task['v']['engine']!='simai':continue
  value=cgroup_usage(task['hostpid']) if task.get('hostpid',0)>0 else None
  wall=task['raw']/'internal.wall'
  if wall.exists():
   match=re.search(r'cpu_usec=(\d+)',wall.read_text())
   if match:value=max(value or 0,int(match[1]))
  if value is not None:task['last_cpu_usec']=max(task.get('last_cpu_usec',0),value)
  elif task['p'].poll() is None:complete=False
  elif not wall.exists() or 'cpu_usec=' not in wall.read_text():complete=False
  if total is not None:total+=task.get('last_cpu_usec',0)
 sample['owned_cpu_usage_usec']=total if complete else None;sample['cpu_accounting_complete']=complete
c.account_batch_cpu=account_batch_cpu
def snapshot():
 v=_snapshot();v['gpu_pmon']=c.cmdout(['nvidia-smi','pmon','-c','1','-s','u']);v['host_cpu_ticks']=cpu_ticks();v['controller_cpu_usage_usec']=cgroup_usage();v['cpu_clock_ticks_per_second']=os.sysconf('SC_CLK_TCK');return v
c.snapshot=snapshot
def foreign_gpu(sample):
 own={p['pid'] for p in c.proc_tree(os.getpid())}|set(sample.get('known_owned_pids',[]))|{sample.get('pid')}|{p['pid'] for p in sample.get('own_processes',[])}|{p['pid'] for ps in sample.get('own_processes_by_job',{}).values() for p in ps};bad=[]
 for line in sample.get('gpu_pmon','').splitlines():
  f=line.split()
  if len(f)>4 and f[0].isdigit() and f[1].isdigit() and f[3].isdigit() and int(f[1]) not in own and int(f[3])>=20:bad.append(dict(pid=int(f[1]),sm_percent=int(f[3])))
 return bad
def guarded_monitor(stop,path,pid,role,container,cpu):
 """Two sampled external GPU bursts abort only the exact owned Popen/container.
 No foreign PID is ever signalled. Partial timing is retained and excluded.
 """
 streak=0
 with path.open('w',buffering=1) as f:
  while not stop.is_set():
   x=c.snapshot();x.update(pid=pid,bound_cpu=cpu);hostpid=pid
   if role=='simai':
    with contextlib.suppress(Exception):hostpid=int(c.cmdout(['docker','inspect',container,'--format','{{.State.Pid}}']))
   x['own_processes']=c.proc_tree(hostpid);x['cpu_stat']=Path('/proc/stat').read_text().split('intr ')[0]
   controller=x.get('controller_cpu_usage_usec');container_usage=(cgroup_usage(hostpid) if hostpid!=pid else None) if role=='simai' else 0
   x['owned_cpu_usage_usec']=controller+container_usage if controller is not None and container_usage is not None else None
   with contextlib.suppress(OSError):x['process_status']=Path(f'/proc/{pid}/status').read_text()
   bad=foreign_gpu(x);streak=streak+1 if bad else 0
   if streak>=2:
    x.update(interrupted_for_external_gpu=True,external_gpu=bad);f.write(json.dumps(x)+'\n')
    if role=='simai' and container and container.startswith(('twins-evidence-iso18-','iso18-mtp-')):sp.run(['docker','stop','-t','2',container],stdout=sp.DEVNULL,stderr=sp.DEVNULL)
    with contextlib.suppress(ProcessLookupError):os.killpg(pid,signal.SIGTERM)
    return
   f.write(json.dumps(x)+'\n');stop.wait(5)
c.monitor=guarded_monitor;c.foreign_gpu=foreign_gpu
_audit=c.audit_interference
def audit(samples,engine,cpu=0):
 samples=[dict(s,own_processes=s.get('own_processes',[])+[p for ps in s.get('own_processes_by_job',{}).values() for p in ps]) for s in samples]
 v=_audit(samples,engine,cpu);foreign=[]
 legacy=dict(v);cpu_windows=[]
 for a,b_ in zip(samples,samples[1:]):
  if any(s.get('owned_cpu_usage_usec') is None or not s.get('host_cpu_ticks') for s in (a,b_)):continue
  dt=(b_['utc_ns']-a['utc_ns'])/1e9
  if dt<=0:continue
  ticks=[y-x for x,y in zip(a['host_cpu_ticks'],b_['host_cpu_ticks'])];busy=(sum(ticks)-ticks[3]-ticks[4])/a['cpu_clock_ticks_per_second'];own=(b_['owned_cpu_usage_usec']-a['owned_cpu_usage_usec'])/1e6
  if own<0:continue
  cpu_windows.append(max(0,busy-own)/dt)
 if cpu_windows:
  streak=longest=0
  for value in cpu_windows:streak=streak+1 if value>1.0 else 0;longest=max(longest,streak)
  v.update(suspected=longest>=3,legacy_pid_audit=legacy,cpu_audit='persistent cgroup cumulative CPU across process exits, whole-host residual; >1 background core for three windows',foreign_cpu_core_samples=cpu_windows,valid_cgroup_windows=len(cpu_windows),unknown_cgroup_windows=max(0,len(samples)-1-len(cpu_windows)),longest_cpu_streak=longest)
 for s in samples:
  own={p['pid'] for p in s.get('own_processes',[])}|{s.get('pid')}|set(s.get('known_owned_pids',[]))
  for line in s.get('gpu_pmon','').splitlines():
   fields=line.split()
   if len(fields)>4 and fields[0].isdigit() and fields[1].isdigit() and fields[3].isdigit() and int(fields[1]) not in own and int(fields[3])>=20:foreign.append(dict(utc_ns=s.get('utc_ns'),pid=int(fields[1]),sm_percent=int(fields[3])))
 v.update(foreign_gpu_pmon_samples=foreign,interrupted_for_external_gpu=any(s.get('interrupted_for_external_gpu') for s in samples),suspected=v['suspected'] or bool(foreign),policy='Any sampled foreign GPU work >=20% invalidates timing; two consecutive samples abort only owned processes; output correctness is independent.');return v
c.audit_interference=audit
def next_label(folder,base,summary=False):
 """Retain contaminated attempts; a later pass gets a fresh directory/summary label."""
 candidates=[folder/(base+'.json')]+sorted(folder.glob(base+'-timingretry*.json')) if summary else [folder/base/'result.json']+sorted(folder.glob(base+'-timingretry*/result.json'))
 existing=[p for p in candidates if p.exists()]
 if any(read(p).get('timing_eligible',False) and (folder.name!='tcp-scans' or read(p).get('monitor_schema',1)>=2) for p in existing):return None
 return base if not existing else base+'-timingretry'+str(len(existing)+1)
def state(**kw):save(EXPORT/'state.json',dict(utc_ns=time.time_ns(),**kw))
def cpu_ticks():return list(map(int,Path('/proc/stat').read_text().splitlines()[0].split()[1:9]))
def available_gib():return int(re.search(r'^MemAvailable:\s+(\d+)',Path('/proc/meminfo').read_text(),re.M)[1])/2**20
def idle_slot(label,required_gib=1):
 """No cross-setting overlap. A short idle window is observed; background desktop is retained.
 External tasks are never killed or suspended. Recheck before every measured setting.
 """
 if shutil.disk_usage(E).free<14*2**30:
  removed=[]
  for p in c.CACHE.iterdir():
   if p.is_file() and re.fullmatch(r'[0-9a-f]{64}\.(uid|dag|unsorted)',p.name):removed.append(dict(file=p.name,bytes=p.stat().st_size));p.unlink()
  if removed:save(E/'receipts/cache-gc'/(str(time.time_ns())+'.json'),dict(removed=removed,scope='regenerable normalization cache only, between settings; all original references and completed results retained'))
 good=0;previous=cpu_ticks();last=time.monotonic()
 while good<3:
  time.sleep(2);cur=cpu_ticks();at=time.monotonic();d=[y-x for x,y in zip(previous,cur)];busy=(sum(d)-d[3]-d[4])/os.sysconf('SC_CLK_TCK')/(at-last);previous=cur;last=at
  g=c.cmdout(['nvidia-smi','--query-gpu=utilization.gpu,memory.used','--format=csv,noheader,nounits']);util=float(g.split(',')[0]) if ',' in g else 100
  mem=available_gib();free=shutil.disk_usage(E).free;ok=util<8 and busy<1.0 and mem>=required_gib and free>=10*2**30
  sample=dict(utc_ns=time.time_ns(),label=label,observed_background_cpu_cores=busy,gpu=g,available_gib=mem,required_available_gib=required_gib,free_disk_bytes=free,eligible=ok)
  c.append(EXPORT/'idle-admission.jsonl',sample);good=good+1 if ok else 0
  if not ok:state(stage='waiting_for_idle',next_setting=label,admission=sample);time.sleep(20)
 state(stage='running',setting=label,preflight=sample)
 return sample
def rows(p,n=6):
 import csv
 with Path(p).open() as f:
  r=csv.reader(f);header=next(r);return [tuple(int(a) for a in x[:n]) for x in r if x]
def compare_tcp(a,ref):
 try:
  x=rows(a);y=rows(ref);full=rows(a,9);hist=rows(ref,9)
  canonical=lambda rs:hashlib.sha256(('\n'.join(','.join(map(str,r)) for r in rs)+'\n').encode()).hexdigest()
  return dict(equal=x==y,rows=len(x),reference_rows=len(y),actual_sha256=sha(a),reference_sha256=sha(ref),canonical_first6_sha256=canonical(x),reference_first6_sha256=canonical(y),completed=sum(r[5]>=0 for r in x),reference_completed=sum(r[5]>=0 for r in y),retx_sum=sum(r[8] for r in full if len(r)>8),route_hashes_equal=all(len(v)>7 and len(w)>7 and v[6:8]==w[6:8] for v,w in zip(full,hist)) if all(len(w)>7 for w in hist) else None,comparison='row order and first six integer CSV fields; hashes/retransmissions reported separately',first_differences=[dict(row=i,actual=u,reference=v) for i,(u,v) in enumerate(zip(x,y)) if u!=v][:5])
 except Exception as e:return dict(equal=False,error=repr(e))
def process(label,cmd,d,timeout=7200,cwd=None,role='cpu',cpu=0):
 """Real process wall through exit. Timed interval excludes hashing and validation."""
 d.mkdir(parents=True,exist_ok=True);save(d/'started.json',dict(utc_ns=time.time_ns(),command=cmd,pre=c.snapshot(),timeout_s=timeout))
 env={**os.environ,'AS_SEND_LAT':'3','OMP_NUM_THREADS':'1'}
 for k in ('TWINS_STOP_SYNCS','TWINS_TRACE','TWINS_TXTRACE','TWINS_AI_PROGRESS'):env.pop(k,None)
 with (d/'process.log').open('w') as f:
  t0=time.perf_counter_ns();utc=time.time_ns();p=sp.Popen(cmd,cwd=cwd or ROOT,env=env,stdout=f,stderr=sp.STDOUT,start_new_session=True)
  stop=threading.Event();t=threading.Thread(target=c.monitor,args=(stop,d/'resources.jsonl',p.pid,role,label if role=='simai' else None,cpu),daemon=True);t.start();timedout=False
  try:rc=p.wait(timeout=timeout)
  except sp.TimeoutExpired:
   timedout=True
   with contextlib.suppress(ProcessLookupError):os.killpg(p.pid,signal.SIGTERM)
   try:p.wait(5)
   except sp.TimeoutExpired:
    with contextlib.suppress(ProcessLookupError):os.killpg(p.pid,signal.SIGKILL)
    p.wait()
   rc=124
  wall=time.perf_counter_ns()-t0;end=time.time_ns();stop.set();t.join(20)
 samples=[json.loads(l) for l in (d/'resources.jsonl').read_text().splitlines()]
 return dict(label=label,command=cmd,start_utc_ns=utc,end_utc_ns=end,wall_ns=wall,rc=rc,timeout=timedout,resources=str((d/'resources.jsonl').relative_to(E)),interference=c.audit_interference(samples,'p0' if role=='gpu' else 'simai',cpu),post=c.snapshot())
def terminal_flags(log):
 last=re.split(r'^.*running again with[^\n]*\n',log,flags=re.M)[-1]
 return dict(flagged=bool(re.search(r'were undecided|warning:.*(?:not exact|ties the chains|capacity|overflow)|error:',last)),undecided=int((re.findall(r'undecided-ties\s+(\d+)',last) or ['0'])[-1]),overflow=bool(re.search(r'capacity overflow|(?:tx-cap|active-ovf|ooo-ovf|due-ovf) [1-9]',last)))
def components(log):
 out={}
 for key,pat in [('closed_gpu_s',r'GPU ([0-9.]+) s'),('system_layer_s',r'system layer ([0-9.]+) s'),('closed_sync_s',r'sync ([0-9.]+) s'),('island_busy_sum_s',r'islands busy ([0-9.]+) s'),('island_wait_s',r'twins-ai waited ([0-9.]+) s'),('island_move_s',r'for them, ([0-9.]+) s moving'),('stub_and_setup_s',r"system layer's flows:.*?\(([0-9.]+) s")]:
  vs=re.findall(pat,log)
  if vs:out[key]=float(vs[-1])
 out['scope']='existing host wall counters at 1ms print precision; GPU includes launch/copy/synchronize, worker busy is sum across workers; overlaps are not additive'
 return out
