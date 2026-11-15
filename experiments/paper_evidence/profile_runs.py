#!/usr/bin/env python3
"""A2 end-to-end and count builds on identical inputs; no no-chain timing ratios."""
import csv,hashlib,json,os,re,subprocess,time,threading
from pathlib import Path
SRC=Path(__file__).resolve().parents[2];DATA=Path('/home/cassius/Work/SpecDES/twiNS');OUT=DATA/'out/paper-evidence-20261116/local/profile';OUT.mkdir(parents=True,exist_ok=True)
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def csvrows(p):
 with open(p) as f:r=csv.reader(f);h=next(r);return h,[tuple(x) for x in r]
def uidrows(p):return sorted(tuple(map(int,x.split())) for x in p.read_text().splitlines())
def normdag(p):return sorted(re.sub(r'^(on [ft][0-9]* m).*',r'\1',l) for l in p.read_text().splitlines() if not l.startswith('#'))
# CPU-memory compilation tasks finish before timing starts; waiting is bounded per build.
for file in ['profile-rle-build.rc','profile57-build.rc','strict-build.rc','mmu-ai-build.rc','mmu-audit-build.rc','corpus-build.rc','oracle-snapshot-build.rc','micro-build.rc','timer-build.rc','mmu-audit-v3-build.rc']:
 p=DATA/'out/paper-evidence-20261116/receipts'/file;deadline=time.monotonic()+3600
 while not p.exists():
  if time.monotonic()>deadline:raise RuntimeError('build wait timeout '+file)
  time.sleep(10)
 if p.read_text().strip()!='0':raise RuntimeError('build failed '+file)
cases=[]
for name,work,tcp,ref in [('tcp-ordinary','k16_a2a30','dctcp','k16_a2a30_TcpDctcp'),('tcp-long','k8_dm30','newreno','k8_dm30_TcpNewReno')]:
 for runs,clean,profile in [(9,'build-evidence/twins','build-profile/twins'),(25,'build-evidence/twins128','build-profile/twins128'),(57,'build-runs57/twins','build-profile57/twins')]:
  if name=='tcp-ordinary' and runs!=9:continue
  cases.append(dict(name=name+f'-runs{runs}',kind='tcp',args=['--k='+str(16 if work.startswith('k16') else 8),'--flows='+str(DATA/f'data/flows/{work}.csv'),'--tcp='+tcp],ref=DATA/f'data/ns3/ref/{ref}.csv',clean=SRC/clean,profile=SRC/profile))
for name,flow,cc in [('rdma-pfc','k8_a2a50s',1),('rdma-lockstep','k8_perm',3)]:
 cases.append(dict(name=name,kind='rdma',args=[str(SRC/'validation/hpcc/hpcc.cfg'),'--topo='+str(DATA/'data/topo/fattree8.txt'),'--flows='+str(next(p for p in (DATA/f'data/rdma/{flow}.csv',DATA/f'data/flows/{flow}.csv') if p.exists())),'CC_MODE='+str(cc),'BUFFER_SIZE=1','--fp-bases=1000003,1000033'],ref=DATA/f'out/rdma/ref/{flow}_b1_cc{cc}.fct',clean=SRC/'build-mmu/twins-rdma',profile=SRC/'build-profile/twins-rdma',strict=SRC/'build-strict/twins-rdma'))
for name,g in [('c_allgather_512m_hpn1024',1024),('gpt3_13b_probe_hpn128',128)]:
 cases.append(dict(name=name,kind='ai',args=[str(SRC/'validation/simai/SimAI.conf'),'--topo='+str(DATA/f'data/simai/hpn{g}.topo'),'--astra='+str(DATA/f'out/simai/wl/paper/{name.replace("_hpn","_")}.txt'),'--fp-bases=1000003,1000033'],ref=DATA/f'out/simai/paper/{name}.ref',clean=SRC/'build-mmu/twins-ai',profile=SRC/'build-profile/twins-ai',strict=SRC/'build-strict/twins-ai'))
# Three interleaved clean/profile rounds; strict has a one-hour censoring limit.
strict_only=os.environ.get('TWINS_PROFILE_STRICT_ONLY')=='1'
for roundno in ([1] if strict_only else range(1,4)):
 for case in cases:
  variants=(["strict"] if 'strict' in case else []) if strict_only else (['clean','profile'] if roundno%2 else ['profile','clean'])
  # Strict comparisons run after all clean/count triples, so censoring does not delay them.
  for variant in variants:
   d=OUT/f'{case["name"]}-r{roundno}-{variant}';d.mkdir(exist_ok=True)
   if (d/'result.json').exists():continue
   bin=case[variant]
   if not bin.exists():raise RuntimeError('missing '+str(bin))
   cmd=[str(bin),*case['args'],'--out='+str(d/'output')]
   if case['kind']=='ai':cmd+=['--dag-out='+str(d/'actions.dag')]
   gpu_paths=list({p.resolve() for p in Path('/sys/class/drm').glob('card[0-9]*/device/mem_info_vram_used')});peaks={str(p):0 for p in gpu_paths};baseline={str(p):int(p.read_text()) for p in gpu_paths};stop=threading.Event();hwm=[0]
   def sample_memory(pid):
    while not stop.is_set():
     for p in gpu_paths:
      try:peaks[str(p)]=max(peaks[str(p)],int(p.read_text()))
      except OSError:pass
     try:
      m=re.search(r'VmHWM:\s+(\d+)',Path(f'/proc/{pid}/status').read_text());hwm[0]=max(hwm[0],int(m[1])*1024 if m else 0)
     except OSError:pass
     stop.wait(.2)
   start=time.perf_counter_ns();utc=time.time_ns()
   with (d/'process.log').open('w') as f:
    proc=subprocess.Popen(cmd,cwd=d,stdout=f,stderr=subprocess.STDOUT,env={**os.environ,'AS_SEND_LAT':'3'},start_new_session=True);thread=threading.Thread(target=sample_memory,args=(proc.pid,));thread.start()
    try:rc=proc.wait(timeout=3600)
    except subprocess.TimeoutExpired:
     import signal
     os.killpg(proc.pid,signal.SIGTERM)
     try:proc.wait(5)
     except subprocess.TimeoutExpired:os.killpg(proc.pid,signal.SIGKILL);proc.wait()
     rc=124
    elapsed=time.perf_counter_ns()-start;stop.set();thread.join()
   log=(d/'process.log').read_text(errors='replace');v=dict(case=case['name'],kind=case['kind'],round=roundno,variant=variant,command=cmd,binary_sha256=sha(bin),reference_sha256=sha(case['ref']),wall_ns=elapsed,start_utc_ns=utc,rc=rc,timeout=(rc==124),flagged=bool(re.search(r'were undecided|warning:.*(?:not exact|ties the chains)|error:',log)),overflow=('capacity overflow' in log),load=os.getloadavg())
   try:
    if case['kind']=='tcp':
     h,a=csvrows(d/'output');rh,b=csvrows(case['ref']);a=[x[:6] for x in a];b=[x[:6] for x in b]
    else:a=uidrows(d/'output');b=uidrows(case['ref'])
    v['validation']=dict(equal=a==b,rows=len(a),reference_rows=len(b),sha256=sha(d/'output'))
    if case['kind']=='ai':v['dag_equal']=normdag(d/'actions.dag')==normdag(DATA/f'out/simai/paper/{case["name"]}.dag')
   except Exception as e:v['validation']=dict(equal=False,error=str(e))
   v['memory_observations']=dict(peak_total_device_bytes=peaks,idle_total_device_bytes=baseline,parent_vm_hwm_bytes=hwm[0],sample_period_s=.2,scope='whole-device totals include desktop; parent HWM excludes short-lived stub children');v['cpu_affinity']=sorted(os.sched_getaffinity(0))
   v['counters']={l.split(':')[0]:l.split(':',1)[1].strip() for l in log.splitlines() if l.startswith(('evidence_','prof:','lead fingerprints:','drops '))}
   v['accepted']=rc==0 and v['validation']['equal'] and v.get('dag_equal',True) and not v['flagged'] and not v['overflow']
   (d/'result.json').write_text(json.dumps(v,indent=2)+'\n')
   with (OUT/'attempts.jsonl').open('a') as f:f.write(json.dumps(v)+'\n')
   print(json.dumps({k:v[k] for k in ('case','variant','round','wall_ns','rc','accepted')}),flush=True)
   # Preserve compact checks and logs; full AI outputs/DAGs can be regenerated.
   if v['validation'].get('equal') and case['kind']=='ai':
    (d/'output').unlink()
    if (d/'actions.dag').exists() and v.get('dag_equal'):(d/'actions.dag').unlink()
(OUT/('strict-finished.json' if strict_only else 'finished.json')).write_text(json.dumps(dict(utc_ns=time.time_ns())))
