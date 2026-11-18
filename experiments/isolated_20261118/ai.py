"""New exclusive-setting AI latency, ablation, MTP and throughput measurements."""
import gzip,shutil,statistics
from common import *
POLICY=BASE/'out/paper-evidence-20261116/export/batches-v2/memory-policy.json'
def cleanup_raw(label):
 p=RAW/label
 if p.exists() and p.resolve().parent==RAW.resolve():shutil.rmtree(p)
def latency():
 os.environ['TWINS_KEEP_REFERENCE_FCT']=str(E/'reference-cache')
 for r in (1,2,3):
  for ji in range(8):
   j=c.JOBS[(ji+r-1)%8];engines=['simai','p0','p16'];rot=(r+ji-1)%3;engines=engines[rot:]+engines[:rot]
   for eng in engines:
    formal=f'iso18-latency-r{r}-{j["case"]}-{eng}';label=next_label(EXPORT/'attempts',formal)
    if label is None:continue
    path=EXPORT/'attempts'/label/'result.json'
    idle=idle_slot(label,required_gib=j['memory_cap_gib']+1 if eng=='simai' else 12)
    v=c.attempt(label,j,eng,resource_context=dict(policy='isolated-20261118',co_run_allowed=False,preflight=idle,single_measured_setting=True,source_core=MANIFEST['source_core'],background_scope='idle admission and samples; desktop/foreign processes are not stopped'))
    v['formal_id']=formal;v['components']=components((path.parent/'process.log').read_text());v['timing_eligible']=not v['interference']['suspected'];save(path,v)
    if v['accepted']:cleanup_raw(label)
 for r in (1,2,3):
  for setting,opts in [('serial',['--serial']),('gwin0',['--gwin=0']),('both',['--serial','--gwin=0'])]:
   formal=f'iso18-ablation-r{r}-{setting}';label=next_label(EXPORT/'attempts',formal)
   if label is None:continue
   path=EXPORT/'attempts'/label/'result.json';idle=idle_slot(label,required_gib=12);v=c.attempt(label,c.JOBS[1],'p0',opts,resource_context=dict(policy='isolated-20261118',co_run_allowed=False,preflight=idle));v['formal_id']=formal;v['components']=components((path.parent/'process.log').read_text());v['timing_eligible']=not v['interference']['suspected'];save(path,v)
   if v['accepted']:cleanup_raw(label)
 os.environ.pop('TWINS_KEEP_REFERENCE_FCT',None)
 save(EXPORT/'ai-latency-finished.json',dict(utc_ns=time.time_ns(),planned_latency=72,planned_ablation=9))
def throughput():
 policy=read(POLICY);save(b.BROOT/'memory-policy.json',dict(**policy,scope='exact same frozen per-case caps and descending old_simai_s admission order as the previous campaign; each batch now runs alone'))
 for r in (1,2,3):
  settings=[('simai',1),('simai',2),('simai',4),('simai',8),('p0',1),('p16',1)];rot=(r-1)%6
  for eng,con in settings[rot:]+settings[:rot]:
   formal=f'iso18-cpu-r{r}-c{con}' if eng=='simai' else f'iso18-{eng}-r{r}';label=next_label(b.BROOT,formal,True)
   if label is None:continue
   idle=idle_slot(label,required_gib=1+min(70,sum(sorted((x['cap_gib'] for x in policy['jobs'].values()),reverse=True)[:con])) if eng=='simai' else 12)
   b.batch(label,eng,con,policy,cpu_pool=list(range(16)),resource_context=dict(policy='isolated-20261118',co_running_batches_allowed=False,preflight=idle,single_measured_setting=True,source_core=MANIFEST['source_core']))
   v=read(b.BROOT/(label+'.json'));p=b.BROOT/v['trial'];interference=audit([json.loads(l) for l in (p/'resources.jsonl').read_text().splitlines()],eng);v.update(formal_id=formal,timing_eligible=not interference['suspected'],interference=interference)
   save(b.BROOT/(label+'.json'),v)
   for q in p.glob('*/result.json'):
    x=read(q);x['components']=components((q.parent/'process.log').read_text());save(q,x)
 save(EXPORT/'ai-throughput-finished.json',dict(utc_ns=time.time_ns(),planned_batches=18,planned_jobs=144))
def fct_difference(actual,ref):
 # Preserve exact byte agreement and line multiplicity; ordering-independent comparison
 # complements the raw hash when MTP uses a different output order.
 from collections import Counter
 a=actual.read_text().splitlines();b=ref.read_text().splitlines();x,y=Counter(a),Counter(b)
 def keyed(lines):
  groups={}
  for line in lines:
   fields=line.split()
   if len(fields)==8:groups.setdefault(tuple(fields[:5]),[]).append(tuple(map(int,fields[5:])))
  return groups
 ga,gb=keyed(a),keyed(b);common=ga.keys()&gb.keys();unique=[k for k in common if len(ga[k])==len(gb[k])==1]
 paired=dict(common_flow_keys=len(common),unique_common_flow_keys=len(unique),identical_start_ns=sum(ga[k][0][0]==gb[k][0][0] for k in unique),identical_fct_ns=sum(ga[k][0][1]==gb[k][0][1] for k in unique),identical_completion_ns=sum(sum(ga[k][0][:2])==sum(gb[k][0][:2]) for k in unique),scope='key=source,destination,sourceport,destinationport,size; exact timestamp comparison only for unique common keys; not a claimed system UID mapping')
 return dict(byte_equal=sha(actual)==sha(ref),row_multiset_equal=x==y,actual_rows=len(a),reference_rows=len(b),matching_rows=sum((x&y).values()),extra_rows=sum((x-y).values()),missing_rows=sum((y-x).values()),actual_sha256=sha(actual),reference_sha256=sha(ref),timestamp_comparison=paired,examples_extra=list((x-y).elements())[:10],examples_missing=list((y-x).elements())[:10],scope='complete SimAI FCT rows; simulation iteration summaries separately retained; differing row identities are not assumed interchangeable')
def mtp_attempt(j,threads,roundno,phase='sweep'):
 formal=f'iso18-mtp-{phase}-{j["case"]}-t{threads}-r{roundno}';label=next_label(EXPORT/'mtp',formal)
 if label is None:
  ps=[EXPORT/'mtp'/formal/'result.json']+list((EXPORT/'mtp').glob(formal+'-timingretry*/result.json'));return next(read(p) for p in ps if p.exists() and read(p).get('timing_eligible'))
 d=EXPORT/'mtp'/label
 idle=idle_slot(label,required_gib=71);d.mkdir(parents=True,exist_ok=True);raw=RAW/label;raw.mkdir(exist_ok=True);etc=raw/'etc';(etc/'simulation').mkdir(parents=True,exist_ok=True);(etc/'results').mkdir(exist_ok=True)
 mtp=ROOT/'simai-mtp';sim='astra-sim-alibabacloud/extern/network_backend/ns3-interface/simulation';bin=mtp/sim/'build-mtp/scratch/ns3.36.1-AstraSimNetwork-optimized';mask='0' if threads==1 else '0-'+str(threads-1)
 cmd=['docker','run','--rm','--name',label,'--label','twins.evidence.campaign=20261118','--user',f'{os.getuid()}:{os.getgid()}','--cpuset-cpus',mask,'--memory=70g','--memory-swap=70g','--ulimit','core=0','-v',str(mtp)+':/simai:ro','-v',str(c.AIROOT)+':/work:ro','-v',str(etc)+':/etc/astra-sim','-v',str(raw)+':/run','-w','/run','localhost/simai-build','bash','-c','export AS_SEND_LAT=3; exec timeout 7200 "$@" > /run/out.txt 2>/run/err.txt','_','/simai/'+sim+'/build-mtp/scratch/ns3.36.1-AstraSimNetwork-optimized','-t',str(threads),'-w',f'/work/wl/paper/{j["case"].replace("_hpn","_")}.txt','-n',f'/work/topo/AlibabaHPN_{j["gpus"]}g_8gps_DualToR_SinglePlane_200Gbps_H100','-c','/simai/astra-sim-alibabacloud/inputs/config/SimAI.conf']
 # Keep full stdout/stderr, never a core dump. Timeout belongs to the container too.
 v=process(label,cmd,d,timeout=7260,role='simai',cpu=0);fct=etc/'simulation/llama_hpn7_fct.txt';ref=E/'reference-cache'/(j['case']+'.fct')
 try:diff=fct_difference(fct,ref)
 except Exception as e:diff=dict(error=repr(e),actual_rows=sum(1 for _ in fct.open()) if fct.exists() else 0,reference_rows=sum(1 for _ in ref.open()) if ref.exists() else None,byte_equal=False)
 for file in ('out.txt','err.txt'):
  if (raw/file).exists():shutil.copy2(raw/file,d/file)
 for summary in raw.glob('*EndToEnd*.csv'):
  if summary.stat().st_size<10<<20:shutil.copy2(summary,d/summary.name)
 v.update(formal_id=formal,case=j['case'],threads=threads,round=roundno,phase=phase,affinity=mask,binary_sha256=sha(bin),patch_manifest_sha256=sha(E/'receipts/mtp-source.json'),validation=diff,complete=v['rc']==0 and diff.get('actual_rows',0)>0 and diff.get('actual_rows')==diff.get('reference_rows'),exclusive_preflight=idle,timing_eligible=not v['interference']['suspected'],comparison_scope='completion and matching reported independently; MTP differing outputs remain eligible only as a separate scheduling semantics speed baseline')
 v['stdout_iteration_lines']=[line for line in (d/'out.txt').read_text(errors='replace').splitlines() if re.search('iter|finish|complete|elapsed|EndToEnd',line,re.I)][-80:] if (d/'out.txt').exists() else []
 save(d/'result.json',v)
 # A limited difference summary and hashes are the evidence; the large CPU row files are regenerable.
 cleanup_raw(label);print(json.dumps(dict(label=label,rc=v['rc'],complete=v['complete'],seconds=v['wall_ns']/1e9)),flush=True);return v
def mtp():
 jobs=[j for j in c.JOBS if j['case'] in ('gpt3_13b_full_hpn128','llama_65b_full_hpn128','gpt3_175b_full_hpn128')]
 for j in jobs:
  sweep=[mtp_attempt(j,t,1) for t in (1,2,4,8,16,32)];complete=[v for v in sweep if v['complete'] and v['timing_eligible']]
  if not complete:
   save(EXPORT/'mtp'/('summary-'+j['case']+'.json'),dict(case=j['case'],fastest_completed_threads=None,status='no completed timing-eligible configuration; all failures/censoring retained'));continue
  best=min(complete,key=lambda x:x['wall_ns']);extras=[mtp_attempt(j,best['threads'],r,'repeat-best') for r in (2,3)]
  save(EXPORT/'mtp'/('summary-'+j['case']+'.json'),dict(case=j['case'],fastest_completed_threads=best['threads'],selection='minimum completed eligible single sweep observation, then two additional repeats; original chosen point retained',attempts=[best['label']]+[v['label'] for v in extras],all_three_complete=all(v['complete'] for v in [best]+extras),all_three_timing_eligible=all(v['timing_eligible'] for v in [best]+extras),wall_ns=[v['wall_ns'] for v in [best]+extras]))
 save(EXPORT/'mtp-finished.json',dict(utc_ns=time.time_ns(),planned_sweep=18,repeat_policy='two further repeats of fastest completed thread setting per workload, if any'))
