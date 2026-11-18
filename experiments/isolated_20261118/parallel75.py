#!/usr/bin/env python3
"""User-authorized shared-host campaign. One GPU, disjoint physical cores, <=75GiB.
CPU work proceeds through foreign GPU activity. GPU observations retain the external
activity exclusion. Shared-memory-bandwidth timing is explicitly not exclusive.
"""
import argparse
from common import *
import ai,tcp,runner
P=EXPORT/'parallel75';P.mkdir(exist_ok=True)
BUDGET=75;OVERHEAD=1;MODEL_BUDGET=BUDGET-OVERHEAD
CORE_ORDER=[8,4,12,2,10,6,14,1,9,3,11,5,13,7] # physical cores; 0 GPU,15 observer
POLICY=read(ai.POLICY)
GPU_POLICY=read(Path(__file__).with_name('gpu_host_policy.json'))
def gpu_memory(case):return GPU_POLICY['jobs'][case]['reserved_gib']
def tasks():
 out=[]
 def add(kind,formal,engine,memory,ncores,phase,**kw):out.append(dict(kind=kind,formal=formal,engine=engine,memory=memory,ncores=ncores,phase=phase,**kw))
 add('probe','iso18-gpu-contention-probe-512m','p0',10,1,-1,case=c.JOBS[1]['case'])
 for r in (1,2,3):
  for j in c.JOBS:
   for eng in ('simai','p0','p16'):
    add('latency',f'iso18-latency-r{r}-{j["case"]}-{eng}',eng,POLICY['jobs'][j['case']]['cap_gib'] if eng=='simai' else gpu_memory(j['case']),1 if eng!='p16' else 16,0 if eng!='p16' else 1,case=j['case'])
  for name,opts in [('serial',['--serial']),('gwin0',['--gwin=0']),('both',['--serial','--gwin=0'])]:add('latency',f'iso18-ablation-r{r}-{name}','p0',gpu_memory(c.JOBS[1]['case']),1,0,case=c.JOBS[1]['case'],options=opts)
  for group in ('scan138','scan1260'):
   for eng,setting in [('cpu',16),('cpu',32)]+([('gpu',1),('gpu',138)] if group=='scan138' else [('gpu',32)]):
    add('scan',f'{group}-r{r}-{eng}{setting}',eng,8 if eng=='cpu' else 4,16 if eng=='cpu' else 1,2 if eng=='cpu' else 0,group=group,setting=setting,round=r)
  for con in (1,2,4,8):
   mem=min(b.BUDGET,sum(sorted((v['cap_gib'] for v in POLICY['jobs'].values()),reverse=True)[:con]));add('batch',f'iso18-cpu-r{r}-c{con}','simai',mem,con,4,con=con)
  for eng in ('p0','p16'):add('batch',f'iso18-{eng}-r{r}',eng,max(gpu_memory(j['case']) for j in c.JOBS),1 if eng=='p0' else 16,1,con=1)
 for j in c.JOBS:
  if j['case'] not in ('gpt3_13b_full_hpn128','llama_65b_full_hpn128','gpt3_175b_full_hpn128'):continue
  # All sweep settings use one fixed cap, not a thread-dependent memory policy.
  for threads in (1,2,4,8,16,32):add('mtp',f'iso18-mtp-sweep-{j["case"]}-t{threads}-r1','mtp',32,min(16,threads),3,case=j['case'],threads=threads,round=1,mtp_phase='sweep')
 return out

def receipt_paths(t):
 f=t['formal'];kind=t['kind']
 if kind=='scan':root=EXPORT/'tcp-scans';return [root/f/'result.json']+list(root.glob(f+'-timingretry*/result.json'))
 if kind=='batch':root=b.BROOT;return [root/(f+'.json')]+list(root.glob(f+'-timingretry*.json'))
 root=EXPORT/('mtp' if kind=='mtp' else 'attempts');return [root/f/'result.json']+list(root.glob(f+'-timingretry*/result.json'))
def result(t):
 for p in receipt_paths(t):
  if not p.exists():continue
  v=read(p)
  if (t['kind']=='probe' or v.get('timing_eligible')) and (t['kind']!='scan' or v.get('monitor_schema',1)>=2):return v
 return None

def worker(t):
 os.sched_setaffinity(0,{15}) # observers/validation, not the measured children
 cores=t['cores'];eng=t['engine'];kind=t['kind'];ctx=dict(policy='parallel75-20261119',co_run_allowed=eng!='p16' and len(cores)<16,total_budget_gib=BUDGET,observer_reservation_gib=OVERHEAD,initial_reservation_gib=t['memory'],physical_cores=cores,dispatch_log='export/parallel75/dispatch.jsonl',scope='disjoint physical cores; memory quotas guaranteed; RAM bandwidth/power remain shared; not an exclusive workstation timing')
 if kind=='scan':tcp.scan_one(t['group'],eng,t['setting'],t['round']);return
 if kind=='mtp':
  j=c.BY[t['case']];ai.mtp_attempt(j,t['threads'],t['round'],t['mtp_phase']);return
 if kind=='batch':
  label=next_label(b.BROOT,t['formal'],True)
  if label is None:return
  def shrink(bound,keep):save(P/'quotas'/(t['formal']+'.json'),dict(memory=bound,cores=keep))
  b.batch(label,eng,t['con'],POLICY,cpu_pool=cores,resource_context=ctx,reservation_update=shrink if eng=='simai' else None,cancel_event=threading.Event())
  p=b.BROOT/(label+'.json');v=read(p);d=b.BROOT/v['trial'];samples=[json.loads(l) for l in (d/'resources.jsonl').read_text().splitlines()];a=audit(samples,eng);v.update(formal_id=t['formal'],timing_eligible=not a['suspected'],interference=a);save(p,v)
  for q in d.glob('*/result.json'):
   v=read(q);v['components']=components((q.parent/'process.log').read_text());save(q,v)
  return
 label=next_label(EXPORT/'attempts',t['formal'])
 if label is None:return
 os.environ['TWINS_KEEP_REFERENCE_FCT']=str(E/'reference-cache')
 j=c.BY[t['case']];pre=idle_slot(label);ctx['preflight']=pre;v=c.attempt(label,j,eng,t.get('options',()),cpu=cores[0],cap=t['memory'],resource_context=ctx)
 p=EXPORT/'attempts'/label/'result.json';v.update(components=components((p.parent/'process.log').read_text()),timing_eligible=not v['interference']['suspected']);
 if kind=='probe':v.update(diagnostic=True,scope='controlled shared-GPU contention diagnostic; never a formal timing sample')
 else:v['formal_id']=t['formal']
 save(p,v)
 if v['accepted'] or v['interference'].get('interrupted_for_external_gpu'):ai.cleanup_raw(label)

def allocate(t,allocations):
 used={i for x in allocations for i in x['cores']};reserved=sum(x['memory'] for x in allocations)
 gpu=t['engine'] in ('gpu','p0','p16')
 if gpu and any(x['gpu'] for x in allocations):return None
 if t['ncores']==16:cores=list(range(16))
 elif gpu:cores=[0]
 else:cores=[i for i in CORE_ORDER if i not in used][:t['ncores']]
 if len(cores)!=t['ncores'] or used&set(cores) or reserved+t['memory']>MODEL_BUDGET:return None
 return dict(cores=cores,memory=t['memory'],gpu=gpu)

def supervise():
 os.sched_setaffinity(0,{15})
 with (E/'exclusive-campaign.lock').open('a') as lock:
  fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);runner.freeze()
  all_tasks=tasks();save(P/'plan.json',dict(created_utc_ns=time.time_ns(),total_budget_gib=BUDGET,observer_gib=OVERHEAD,gpu_host_memory_policy=GPU_POLICY,model_budget_gib=MODEL_BUDGET,batch_internal_budget_gib=b.BUDGET,tasks=all_tasks,authorization='User 2026-11-19: start early, parallelize where accuracy permits, 75GB budget; overrides older exclusive-only admission requirement',core_policy='no own overlap on physical SMT siblings; P16/wide CPU tasks drain all other measured tasks'))
  pending=[t for t in all_tasks if result(t) is None];active={};retry={};last_gpu=0;gpu_quiet=False;cpu_phase=0;mtp_added=False
  def shutdown(signum,frame):
   # Only containers with both our campaign and UID label and our raw mount qualify.
   ids=sp.check_output(['docker','ps','-q','--filter','label=twins.evidence.campaign=20261118','--filter',f'label=twins.evidence.owner={os.getuid()}'],text=True).split()
   for cid in ids:
    info=json.loads(sp.check_output(['docker','inspect',cid],text=True))[0]
    if any(Path(m.get('Source','/')).is_relative_to(RAW) for m in info.get('Mounts',[])):sp.run(['docker','stop','-t','2',cid],stdout=sp.DEVNULL,stderr=sp.DEVNULL)
   for x in active.values():
    with contextlib.suppress(ProcessLookupError):os.killpg(x['p'].pid,signal.SIGTERM)
   raise SystemExit(128+signum)
  signal.signal(signal.SIGTERM,shutdown);signal.signal(signal.SIGINT,shutdown)
  while pending or active or not mtp_added:
   for key,x in list(active.items()):
    if x['p'].poll() is None:
     q=P/'quotas'/(key+'.json')
     if q.exists():
      new=read(q)
      if new['memory']>x['alloc']['memory'] or not set(new['cores'])<=set(x['alloc']['cores']):raise RuntimeError('quota grew')
      x['alloc'].update(new)
     continue
    x['log'].close();active.pop(key);v=result(x['task']);c.append(P/'dispatch.jsonl',dict(utc_ns=time.time_ns(),action='finished',formal=key,worker_rc=x['p'].returncode,timing_eligible=v is not None,accepted=v.get('accepted',v.get('all_accepted',v.get('complete'))) if v else None))
    if v and x['task']['kind']=='mtp' and not v.get('complete') and v.get('rc') in (137,-9) and x['task']['memory']<MODEL_BUDGET:
     old=x['task'];new=dict(old,memory=MODEL_BUDGET,formal=old['formal'].replace('-mtp-sweep-','-mtp-memory-retry-sweep-'),mtp_phase='memory-retry-sweep');all_tasks.append(new);pending.append(new)
    if v is None:
     retry[key]=time.monotonic()+60;pending.append(x['task'])
     if x['p'].returncode not in (0,1):raise RuntimeError('worker failed: '+key)
   # After all sweep receipts, select each model's fastest completed configuration.
   if not mtp_added and not any(t['kind']=='mtp' for t in pending) and not any(x['task']['kind']=='mtp' for x in active.values()):
    for case in sorted({t['case'] for t in all_tasks if t['kind']=='mtp'}):
     xs=[result(t) for t in all_tasks if t['kind']=='mtp' and t['case']==case];complete=[v for v in xs if v and v.get('complete')]
     if not complete:save(EXPORT/'mtp'/('summary-'+case+'.json'),dict(case=case,fastest_completed_threads=None,status='no completed timing-eligible MTP setting'));continue
     best=min(complete,key=lambda v:v['wall_ns'])
     for r in (2,3):
      t=dict(kind='mtp',formal=f'iso18-mtp-repeat-best-{case}-t{best["threads"]}-r{r}',engine='mtp',memory=best.get('memory_cap_gib',32),ncores=min(16,best['threads']),phase=3,case=case,threads=best['threads'],round=r,mtp_phase='repeat-best');all_tasks.append(t)
      if result(t) is None:pending.append(t)
    mtp_added=True
   if time.monotonic()-last_gpu>5:
    # Check only foreign compute PIDs: our own current GPU task does not close admission.
    sample=c.snapshot();own={p['pid'] for p in c.proc_tree(os.getpid())};gpu_quiet=not foreign_gpu(dict(sample,known_owned_pids=list(own)));g=sample.get('gpu','').split(',');gpu_quiet &= float(g[0])<8 if len(g)>1 and not any(x['alloc']['gpu'] for x in active.values()) else True;last_gpu=time.monotonic()
   cpu_pending=[t for t in pending if t['engine'] in ('cpu','simai','mtp')];phase=min((t['phase'] for t in cpu_pending),default=5)
   # Avoid launching the next phase until active CPU tasks of the previous phase drain.
   phase=min([phase]+[x['task']['phase'] for x in active.values() if x['task']['engine'] in ('cpu','simai','mtp')])
   wide_wait=any(t['phase']==phase and t['ncores']==16 for t in cpu_pending)
   candidates=sorted(pending,key=lambda t:(t['phase'],0 if t['engine'] in ('gpu','p0','p16') else 1,-c.BY[t['case']]['old_simai_s'] if t['kind']=='latency' and t['engine']=='simai' else 0,t['formal']))
   for t in candidates:
    if retry.get(t['formal'],0)>time.monotonic():continue
    gpu=t['engine'] in ('gpu','p0','p16')
    if not gpu and t['phase']!=phase:continue
    if gpu and t['kind']!='probe' and (not gpu_quiet or (wide_wait and t['phase']>=phase and not any(x['task']['engine'] in ('cpu','simai','mtp') for x in active.values()))):continue
    if t['kind']=='latency' and t['engine']=='simai':
     peers=[x['task'] for x in active.values() if x['task']['kind']=='latency' and x['task']['engine']=='simai']
     if len(peers)>=4:continue
     other_cases={u['case'] for u in pending if u['kind']=='latency' and u['engine']=='simai'}-{u['case'] for u in peers}
     if any(u['case']==t['case'] for u in peers) and other_cases:continue
    # P16 owns all physical cores, admitted at a drained boundary whenever possible.
    alloc=allocate(t,[x['alloc'] for x in active.values()])
    if alloc is None or available_gib()<t['memory']+2 or shutil.disk_usage(E).free<10*2**30:continue
    task=dict(t,**alloc);mask=','.join(map(str,alloc['cores']))
    if t['engine']=='mtp' and t['threads']==32:mask='0-31'
    elif t['engine']=='cpu' and t['setting']==32:mask='0-31'
    elif t['engine']=='p16':mask='0-16'
    env={**os.environ,'TWINS_RESOURCE_MODE':'parallel75','TWINS_SCHEDULER_ADMITTED':'1','TWINS_TASK_MEMORY_GIB':str(t['memory']),'TWINS_TASK_CPU_MASK':mask}
    if t['kind']=='probe':env['TWINS_GPU_CONTENTION_PROBE']='1'
    spec=P/'tasks'/(t['formal']+'.json');save(spec,task);q=P/'quotas'/(t['formal']+'.json')
    if q.exists():q.unlink()
    log=(P/'tasks'/(t['formal']+'.log')).open('a');p=sp.Popen([sys.executable,str(Path(__file__)),'--worker',str(spec)],env=env,stdout=log,stderr=sp.STDOUT,start_new_session=True)
    pending.remove(t);active[t['formal']]=dict(task=t,alloc=alloc,p=p,log=log);c.append(P/'dispatch.jsonl',dict(utc_ns=time.time_ns(),action='started',formal=t['formal'],**alloc,total_reserved_gib=OVERHEAD+sum(x['alloc']['memory'] for x in active.values()),peers=list(active)))
   book=dict(utc_ns=time.time_ns(),resource_epoch='parallel75-20261119',budget_gib=BUDGET,reserved_gib=OVERHEAD+sum(x['alloc']['memory'] for x in active.values()),active=[dict(formal=k,pid=x['p'].pid,engine=x['task']['engine'],**x['alloc']) for k,x in active.items()],queued=len(pending),cpu_phase=phase,gpu_admission_open=gpu_quiet)
   save(P/'active.json',book);state(stage='parallel75',**book);time.sleep(2)
  for case in sorted({t['case'] for t in all_tasks if t['kind']=='mtp'}):
   xs=[result(t) for t in all_tasks if t['kind']=='mtp' and t['case']==case];xs=[v for v in xs if v]
   repeats=[v for v in xs if v.get('phase')=='repeat-best'];sweep=[v for v in xs if v.get('phase')!='repeat-best' and v.get('complete')]
   if not sweep:continue
   best=min(sweep,key=lambda v:v['wall_ns']);vs=[best]+repeats
   save(EXPORT/'mtp'/('summary-'+case+'.json'),dict(case=case,fastest_completed_threads=best['threads'],attempts=[v['label'] for v in vs],wall_ns=[v['wall_ns'] for v in vs],all_three_complete=len(vs)==3 and all(v['complete'] for v in vs),all_three_timing_eligible=len(vs)==3 and all(v['timing_eligible'] for v in vs),resource_epoch='parallel75-20261119'))
  # Detailed result quality is not replaced by this queue-completion marker.
  save(EXPORT/'all-finished.json',dict(utc_ns=time.time_ns(),policy='parallel75-20261119',status='all predeclared attempts returned timing-eligible receipts; output acceptance retained separately'))
if __name__=='__main__':
 ap=argparse.ArgumentParser();ap.add_argument('--worker');args=ap.parse_args()
 if args.worker:worker(read(args.worker))
 else:supervise()
