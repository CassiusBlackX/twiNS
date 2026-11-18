"""CUBIC event-loop comparisons and two distinct full-wall TCP scan experiments."""
import concurrent.futures as cf,gzip,shutil
from common import *
CPU=ROOT/'tcp-reference/ns3-unison-ref';GPU=c.BIN
def cpu_cmd(j,out,threads=0,mask='0'):
 return ['taskset','-c',mask,str(CPU),'--k='+str(j['k']),'--flows='+str(INPUT/j['flows']),'--tcp=ns3::'+{'cubic':'TcpCubic','dctcp':'TcpDctcp','newreno':'TcpNewReno'}[j['tcp']],'--stop='+str(j['stop_s']),'--hostq=red','--flowEcmp=1','--queue='+str(j['queue']),'--ecnK='+str(j['ecnK']),'--threads='+str(threads),'--out='+str(out)]
def gpu_cmd(j,out,binary='twins'):
 return ['taskset','-c','0',str(GPU/binary),'--k='+str(j['k']),'--flows='+str(INPUT/j['flows']),'--tcp='+j['tcp'],'--queue='+str(j['queue']),'--ecnK='+str(j['ecnK']),'--out='+str(out)]
def loop_ns(log):
 v=re.findall(r'event_loop_ns (\d+)',log)
 if v:return int(v[-1])
 v=re.findall(r'Simulation Duration: ([0-9.]+)s',log)
 return round(float(v[-1])*1e9) if v else None
def sim_clock(log,gpu=False):
 p=re.findall(r'evidence final_simulated_ps (\d+)',log)
 if p:return dict(value_ps=int(p[-1]),source='Simulator::Now immediately after Run')
 p=re.findall(r'Executed Slots: (\d+)',log)
 return dict(value_ps=int(p[-1])*1000000,source='reported executed slots times production SLOT_PS=1000000; boundary horizon') if p else None
def preserve_failure(path,dest):
 if path.exists():
  with path.open('rb') as f,gzip.open(dest,'wb') as g:shutil.copyfileobj(f,g)
def cubic():
 jobs=sorted(MANIFEST['cubic'],key=lambda j:('incast' not in j['case'],j['k'],j['case']))
 for r in (1,2,3):
  for j in jobs:
   settings=['seq','u1','u2','u4','u8','u16','u32','twins'];shift=(r-1)%8;settings=settings[shift:]+settings[:shift]
   for setting in settings:
    formal=f'cubic-r{r}-{j["case"]}-{setting}';label=next_label(EXPORT/'cubic',formal)
    if label is None:continue
    d=EXPORT/'cubic'/label
    pre=idle_slot(label);d.mkdir(parents=True,exist_ok=True);out=RAW/(label+'.csv');attempts=[]
    if setting=='twins':
     for binary,frame in [('twins',64),('twins128',128)]:
      v=process(label+'-'+str(frame),gpu_cmd(j,out,binary),d/str(frame),role='gpu');log=(d/str(frame)/'process.log').read_text();v.update(event_loop_ns=loop_ns(log),flags=terminal_flags(log),frame_bytes=frame,binary_sha256=sha(GPU/binary),final_simulated=sim_clock(log,True));attempts.append(v)
      if v['rc'] or not (v['flags']['flagged'] or v['flags']['undecided']):break
     v=attempts[-1];event_ns=sum(x['event_loop_ns'] or 0 for x in attempts);flagged=v['flags']['flagged'] or v['flags']['undecided']>0
    else:
     threads=0 if setting=='seq' else int(setting[1:]);mask='0' if threads<=1 else '0-'+str(threads-1)
     v=process(label,cpu_cmd(j,out,threads,mask),d/'run',cpu=0);log=(d/'run/process.log').read_text();v.update(event_loop_ns=loop_ns(log),flags=dict(flagged=False),threads=threads,affinity=mask,binary_sha256=sha(CPU),final_simulated=sim_clock(log));attempts=[v];event_ns=v['event_loop_ns'];flagged=False
    valid=compare_tcp(out,INPUT/j['reference']);result=dict(label=label,formal_id=formal,case=j['case'],round=r,setting=setting,event_loop_ns=event_ns,wall_ns=sum(x['wall_ns'] for x in attempts),attempts=attempts,validation=valid,final_flagged=flagged,accepted=v['rc']==0 and valid['equal'] and not flagged,timing_eligible=not any(x['interference']['suspected'] for x in attempts),input_sha256=sha(INPUT/j['flows']),reference_sha256=sha(INPUT/j['reference']),exclusive_preflight=pre,timing_scope='min-of-three event loops for table; per-sample TwiNS includes 64B plus required128B loops; full process wall separate')
    if not result['accepted']:preserve_failure(out,d/'mismatching-output.csv.gz')
    save(d/'result.json',result)
    if out.exists():out.unlink()
    print(json.dumps(dict(label=label,accepted=result['accepted'],seconds=(event_ns or 0)/1e9)),flush=True)
 save(EXPORT/'cubic-finished.json',dict(utc_ns=time.time_ns(),executed=192))

def _monitor(stop,d,active,cancel,owned):
 streak=0
 with (d/'resources.jsonl').open('w',buffering=1) as f:
  while not stop.is_set():
   tasks=list(active);sample=dict(**c.snapshot(),active=[x['case'] for x in tasks],own_processes_by_job={x['case']:c.proc_tree(x['p'].pid) for x in tasks},cpu_stat=Path('/proc/stat').read_text().split('intr ')[0],known_owned_pids=list(owned));sample['owned_cpu_usage_usec']=sample.get('controller_cpu_usage_usec');bad=foreign_gpu(sample);streak=streak+1 if bad else 0
   if streak>=2:
    sample.update(interrupted_for_external_gpu=True,external_gpu=bad);cancel.set()
    for task in tasks:
     with contextlib.suppress(ProcessLookupError):os.killpg(task['p'].pid,signal.SIGTERM)
   f.write(json.dumps(sample)+'\n');stop.wait(5)
def scan_one(group,engine,setting,r):
 jobs=MANIFEST[group];formal=f'{group}-r{r}-{engine}{setting}';label=next_label(EXPORT/'tcp-scans',formal)
 if label is None:return
 d=EXPORT/'tcp-scans'/label
 d.mkdir(parents=True,exist_ok=True);pre=idle_slot(label);raw=RAW/label;raw.mkdir(exist_ok=True);commands=[];allpasses=[];active=[]
 if engine=='cpu':
  queue=[]
  for j in jobs:
   out=raw/(j['case']+'.csv');log=d/(j['case']+'.log');queue.append(dict(case=j['case'],job=j,out=out,log=log))
 else:
  queue=[]
  for i in range(0,len(jobs),setting):
   chunk=jobs[i:i+setting];worlds=raw/f'worlds-{i//setting}.txt';lines=[f"flows={INPUT/j['flows']},tcp={j['tcp']},queue={j['queue']},ecnK={j['ecnK']},out={raw/(j['case']+'.csv')}" for j in chunk];worlds.write_text('\n'.join(lines)+'\n');queue.append(dict(jobs=chunk,worlds=worlds,lines=lines))
 save(d/'prepared.json',dict(label=label,manifest_sha256=sha(E/'manifest.json'),engine=engine,setting=setting,job_order=[j['case'] for j in jobs],binary_sha256=sha(CPU) if engine=='cpu' else {x:sha(GPU/x) for x in ('twins','twins128')},exclusive_preflight=pre))
 stop=threading.Event();cancel=threading.Event();owned=set();thread=threading.Thread(target=_monitor,args=(stop,d,active,cancel,owned),daemon=True);thread.start();t0=time.perf_counter_ns();utc=time.time_ns();peak=0;events=[]
 try:
  if engine=='cpu':
   done=[]
   while queue or active:
    if cancel.is_set():raise InterruptedError('foreign GPU activity interrupted this scan')
    while queue and len(active)<setting:
     t=queue.pop(0);used={x['cpu'] for x in active};cpu=next(i for i in range(setting) if i not in used);cmd=cpu_cmd(t['job'],t['out'],0,str(cpu));f=t['log'].open('w');p=sp.Popen(cmd,stdout=f,stderr=sp.STDOUT,start_new_session=True);owned.add(p.pid);t.update(cpu=cpu,p=p,f=f,cmd=cmd,start=time.perf_counter_ns());active.append(t);peak=max(peak,len(active));events.append(dict(action='start',case=t['case'],cpu=cpu,ns=time.perf_counter_ns()-t0,active=len(active)))
    for t in list(active):
     if t['p'].poll() is None:
      if time.perf_counter_ns()-t['start']>7200e9:os.killpg(t['p'].pid,signal.SIGTERM)
      else:continue
     end=time.perf_counter_ns();t['f'].close();active.remove(t);t.update(rc=t['p'].returncode,wall_ns=end-t['start']);done.append(t);events.append(dict(action='exit',case=t['case'],ns=end-t0,active=len(active)))
    if active:time.sleep(.01)
  else:
   peak=1;seq=0
   def run_group(js,lines,worlds,binary,depth=0):
    nonlocal seq
    seq+=1;name=f'pass-{seq:04d}-{binary}';log=d/(name+'.log');cmd=['taskset','-c','0',str(GPU/binary),'--k=8','--worlds='+str(worlds)];s=time.perf_counter_ns()
    with log.open('w') as f:
     p=sp.Popen(cmd,stdout=f,stderr=sp.STDOUT,start_new_session=True);owned.add(p.pid);active.append(dict(case=name,p=p))
     try:rc=p.wait(timeout=7200)
     except sp.TimeoutExpired:
      os.killpg(p.pid,signal.SIGTERM);p.wait();rc=124
     active.clear()
    text=log.read_text();v=dict(label=name,command=cmd,cases=[j['case'] for j in js],frame_bytes=64 if binary=='twins' else 128,rc=rc,wall_ns=time.perf_counter_ns()-s,event_loop_ns=loop_ns(text),flags=terminal_flags(text));allpasses.append(v)
    # Extra128B worlds may exceed VRAM; splitting that retry is fully charged.
    if rc and binary=='twins128' and len(js)>1 and depth<9:
     half=len(js)//2
     for k,(part,ls) in enumerate(((js[:half],lines[:half]),(js[half:],lines[half:]))):
      wf=worlds.with_name(worlds.name+f'.split{k}');wf.write_text('\n'.join(ls)+'\n');run_group(part,ls,wf,binary,depth+1)
     return
    m=re.search(r'undecided-ties by world:([ 0-9]*)',text);indices={int(x) for x in m[1].split()} if m else set(range(len(js))) if v['flags']['flagged'] else set()
    for idx,j in enumerate(js):
     statuses.setdefault(j['case'],[]).append(dict(pass_label=name,frame_bytes=v['frame_bytes'],rc=rc,undecided=idx in indices,overflow=v['flags']['overflow']))
    if binary=='twins' and rc==0 and indices:
     ids=sorted(indices);redo=[js[i] for i in ids];ls=[lines[i] for i in ids];wf=worlds.with_name(worlds.name+'.long');wf.write_text('\n'.join(ls)+'\n');run_group(redo,ls,wf,'twins128')
   statuses={}
   for t in queue:
    if cancel.is_set():raise InterruptedError('foreign GPU activity interrupted this scan')
    run_group(t['jobs'],t['lines'],t['worlds'],'twins')
  elapsed=time.perf_counter_ns()-t0;end_utc=time.time_ns()
 except InterruptedError:
  save(d/'interrupted.json',dict(utc_ns=time.time_ns(),elapsed_ns=time.perf_counter_ns()-t0,reason='foreign GPU competition; entire scan will be repeated',partial_events=events))
  raise
 finally:
  stop.set();thread.join(20)
  for t in active:
   with contextlib.suppress(ProcessLookupError):os.killpg(t['p'].pid,signal.SIGTERM)
   if 'f' in t:t['f'].close()
 validation_start=time.perf_counter_ns();checks=[]
 for j in jobs:
  output=raw/(j['case']+'.csv');v=compare_tcp(output,INPUT/j['reference']);v['case']=j['case']
  if engine=='cpu':
   t=next(x for x in done if x['case']==j['case']);v.update(rc=t['rc'],wall_ns=t['wall_ns'],command=t['cmd'],final_simulated=sim_clock(t['log'].read_text()),flagged=False)
  else:
   passes=statuses.get(j['case'],[]);last=passes[-1] if passes else {};v.update(rc=last.get('rc',1),passes=passes,flagged=last.get('undecided',True) or last.get('overflow',True))
  v['accepted']=v['rc']==0 and v['equal'] and not v['flagged'];checks.append(v)
  if not v['accepted']:preserve_failure(output,d/(j['case']+'.mismatch.csv.gz'))
 samples=[dict(json.loads(l),known_owned_pids=list(owned)) for l in (d/'resources.jsonl').read_text().splitlines()];interference=audit(samples,'p0' if engine=='gpu' else 'simai')
 save(d/'result.json',dict(label=label,formal_id=formal,monitor_schema=2,owned_model_pids=sorted(owned),group=group,engine=engine,setting=setting,round=r,start_utc_ns=utc,end_utc_ns=end_utc,makespan_ns=elapsed,validation_ns=time.perf_counter_ns()-validation_start,peak_actual_concurrency=peak,gpu_peak_worlds_in_one_launch=max((len(x['cases']) for x in allpasses),default=0),events=events,gpu_passes=allpasses,checks=checks,all_accepted=all(x['accepted'] for x in checks),timing_eligible=not interference['suspected'],interference=interference,scope='full batch wall includes routing, initialization, output writes and all retries; validation outside timer; CPU logical masks0..15 or0..31; GPU fixed cap is an upper limit'))
 for p in raw.iterdir():
  if p.is_file():p.unlink()
 raw.rmdir();print(json.dumps(dict(label=label,makespan_s=elapsed/1e9,accepted=sum(x['accepted'] for x in checks),total=len(checks))),flush=True)
def scans():
 for group in ('scan138','scan1260'):
  settings=[('cpu',16),('cpu',32)]+([('gpu',1),('gpu',138)] if group=='scan138' else [('gpu',32)])
  while True:
   for r in (1,2,3):
    shift=(r-1)%len(settings)
    for eng,setting in settings[shift:]+settings[:shift]:
     while True:
      try:scan_one(group,eng,setting,r);break
      except InterruptedError:
       formal=f'{group}-r{r}-{eng}{setting}'
       for d in (EXPORT/'tcp-scans').glob(formal+'*'):
        if (d/'interrupted.json').exists() and not (d/'result.json').exists():
         dest=EXPORT/'interrupted-scans'/(d.name+'-'+str(time.time_ns()));dest.parent.mkdir(exist_ok=True);d.rename(dest)
         raw=RAW/d.name
         if raw.is_dir() and raw.resolve().parent==RAW.resolve():shutil.rmtree(raw)
       state(stage='waiting_after_external_interruption',setting=formal);time.sleep(10)
   eligible={x['formal_id'] for p in (EXPORT/'tcp-scans').glob(group+'-*/result.json') if (x:=read(p)).get('timing_eligible') and x.get('monitor_schema',1)>=2}
   if len(eligible)==len(settings)*3:break
   state(stage='repeating_scan_timings',group=group,eligible=len(eligible),expected=len(settings)*3)
 save(EXPORT/'tcp-scans-finished.json',dict(utc_ns=time.time_ns(),batches=21))
