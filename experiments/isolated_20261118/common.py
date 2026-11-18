"""Paths and exclusive-setting measurement helpers for the 2026-11-18 campaign."""
import contextlib,fcntl,hashlib,json,os,re,resource,signal,subprocess as sp,sys,threading,time
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
def snapshot():
 v=_snapshot();v['gpu_pmon']=c.cmdout(['nvidia-smi','pmon','-c','1','-s','u']);return v
c.snapshot=snapshot
_audit=c.audit_interference
def audit(samples,engine,cpu=0):
 samples=[dict(s,own_processes=s.get('own_processes',[])+[p for ps in s.get('own_processes_by_job',{}).values() for p in ps]) for s in samples]
 v=_audit(samples,engine,cpu);foreign=[]
 for s in samples:
  own={p['pid'] for p in s.get('own_processes',[])}|{s.get('pid')}
  for line in s.get('gpu_pmon','').splitlines():
   fields=line.split()
   if len(fields)>4 and fields[0].isdigit() and fields[1].isdigit() and fields[3].isdigit() and int(fields[1]) not in own and int(fields[3])>=20:foreign.append(dict(utc_ns=s.get('utc_ns'),pid=int(fields[1]),sm_percent=int(fields[3])))
 v.update(foreign_gpu_pmon_samples=foreign,suspected=v['suspected'] or bool(foreign),policy='Any sampled foreign GPU work >=20% invalidates timing; output correctness is independent.');return v
c.audit_interference=audit
def next_label(folder,base,summary=False):
 """Retain contaminated attempts; a later pass gets a fresh directory/summary label."""
 candidates=[folder/(base+'.json')]+sorted(folder.glob(base+'-timingretry*.json')) if summary else [folder/base/'result.json']+sorted(folder.glob(base+'-timingretry*/result.json'))
 existing=[p for p in candidates if p.exists()]
 if any(read(p).get('timing_eligible',False) for p in existing):return None
 return base if not existing else base+'-timingretry'+str(len(existing)+1)
def state(**kw):save(EXPORT/'state.json',dict(utc_ns=time.time_ns(),**kw))
def cpu_ticks():return list(map(int,Path('/proc/stat').read_text().splitlines()[0].split()[1:9]))
def available_gib():return int(re.search(r'^MemAvailable:\s+(\d+)',Path('/proc/meminfo').read_text(),re.M)[1])/2**20
def idle_slot(label,required_gib=1):
 """No cross-setting overlap. A short idle window is observed; background desktop is retained.
 External tasks are never killed or suspended. Recheck before every measured setting.
 """
 good=0;previous=cpu_ticks();last=time.monotonic()
 while good<3:
  time.sleep(2);cur=cpu_ticks();at=time.monotonic();d=[y-x for x,y in zip(previous,cur)];busy=(sum(d)-d[3]-d[4])/os.sysconf('SC_CLK_TCK')/(at-last);previous=cur;last=at
  g=c.cmdout(['nvidia-smi','--query-gpu=utilization.gpu,memory.used','--format=csv,noheader,nounits']);util=float(g.split(',')[0]) if ',' in g else 100
  mem=available_gib();ok=util<8 and busy<1.0 and mem>=required_gib
  sample=dict(utc_ns=time.time_ns(),label=label,observed_background_cpu_cores=busy,gpu=g,available_gib=mem,required_available_gib=required_gib,eligible=ok)
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
