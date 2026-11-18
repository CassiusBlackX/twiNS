#!/usr/bin/env python3
"""One exclusive measured setting at a time. Resumes completed receipts, no hidden merge.
Short TCP results precede long AI stages; builds run before all timing.
"""
import argparse,shutil,tarfile
from common import *
import tcp,ai
def freeze():
 d=E/'receipts/frozen';d.mkdir(exist_ok=True);manifest=read(BASE/'candidate/source-manifest.json');assert manifest['commit'].startswith('6bc5a7c')
 # Validate actual candidate source, not just a directory's label.
 mismatch=[]
 for rel,h in manifest['source_sha256'].items():
  p=BASE/'candidate'/rel
  if not p.is_file() or sha(p)!=h:mismatch.append(rel)
 assert not mismatch,mismatch
 inputs={};copies=d/'ai-inputs';copies.mkdir(exist_ok=True)
 for j in c.JOBS:
  n=j['case'];g=j['gpus']
  for p in [c.SD/'astra-sim-alibabacloud/inputs/config/SimAI.conf',c.AIROOT/f'wl/paper/{n.replace("_hpn","_")}.txt',c.AIROOT/f'topo/AlibabaHPN_{g}g_8gps_DualToR_SinglePlane_200Gbps_H100',BASE/'validation/simai/SimAI.conf',BASE/f'data/simai/hpn{g}.topo',BASE/f'out/simai/wl/paper/{n.replace("_hpn","_")}.txt']:
   if str(p) not in inputs:
    h=sha(p);dest=copies/(h+'-'+p.name);shutil.copy2(p,dest);inputs[str(p)]=dict(sha256=h,copy=str(dest.relative_to(E)),bytes=p.stat().st_size)
 binaries={str(p):sha(p) for p in [c.BIN/n for n in ('twins','twins128','twins-ai','twins-astra-stub','twins-island')]+[tcp.CPU,c.SD/c.SIMBIN.removeprefix('/simai/') ]}
 # Include the actual sequential shared libraries and source/config metadata.
 libs=(c.SD/c.SIMBIN.removeprefix('/simai/')).parent.parent/'lib'
 binaries.update({str(p):sha(p) for p in libs.glob('*.so')})
 v=dict(utc_ns=time.time_ns(),source_manifest=manifest,inputs=inputs,binaries=binaries,host=c.cmdout(['uname','-a']),cpu=c.cmdout(['lscpu']),gpu=c.cmdout(['nvidia-smi','--query-gpu=name,driver_version,memory.total','--format=csv']),image=c.cmdout(['docker','image','inspect','localhost/simai-build','--format','{{.Id}}']),cubic_manifest_sha256=sha(E/'manifest.json'),old_cpu_cap_policy=read(ai.POLICY),controller_source_sha256={str(p.relative_to(ROOT)):sha(p) for p in (ROOT/'code').rglob('*.py')})
 old=d/'manifest.json'
 if old.exists():
  before=read(old);assert before['binaries']==v['binaries'],'binary changed since campaign start';assert before['inputs']==v['inputs'],'input changed since campaign start'
 else:save(old,v)
 for n in ('twins-evidence-ref.cc','logging-only.patch'):shutil.copy2(ROOT/'tcp-reference'/n,d/n)
 inv=d/'invocations';inv.mkdir(exist_ok=True);save(inv/(str(v['utc_ns'])+'.json'),v)
 bundle=d/('controller-'+str(v['utc_ns'])+'.tar.gz')
 with tarfile.open(bundle.with_suffix('.tmp'),'w:gz') as tar:
  for p in (ROOT/'code').rglob('*'):
   if p.is_file() and p.suffix in ('.py','.json','.md'):tar.add(p,arcname=str(p.relative_to(ROOT)))
 bundle.with_suffix('.tmp').rename(bundle)
def quality_count(name):
 paths={'cubic':('cubic/*/result.json',192),'scans':('tcp-scans/*/result.json',21),'ai-latency':('attempts/iso18-latency*/result.json',81),'throughput':('ai-batches/*.json',18)}
 if name=='ai-latency':
  ps=list(EXPORT.glob('attempts/iso18-latency*/result.json'))+list(EXPORT.glob('attempts/iso18-ablation*/result.json'));expected=81
 elif name in paths:pat,expected=paths[name];ps=list(EXPORT.glob(pat))
 elif name=='mtp':
  ps=list(EXPORT.glob('mtp/iso18-mtp-sweep*/result.json'));expected=18
  summaries=list(EXPORT.glob('mtp/summary-*.json'));summaries_ok=len(summaries)==3 and all(read(p).get('all_three_timing_eligible',True) for p in summaries)
  done=len({x['formal_id'] for p in ps if (x:=read(p)).get('timing_eligible')});return dict(eligible=done,expected=expected,complete=done==expected and summaries_ok)
 else:return dict(complete=True)
 done=len({x['formal_id'] for p in ps if (x:=read(p)).get('timing_eligible') and 'formal_id' in x});return dict(eligible=done,expected=expected,complete=done==expected)
def pilot():
 p=EXPORT/'pilot-finished.json'
 if p.exists():return
 pre=idle_slot('pilot-cubic-incast-and-ai',required_gib=14);j=next(j for j in MANIFEST['cubic'] if j['case']=='k8_incast16');d=EXPORT/'pilots';d.mkdir(exist_ok=True);out=RAW/'pilot-tcp.csv';results=[]
 for mode in ('seq','u2','twins'):
  cmd=tcp.cpu_cmd(j,out,0 if mode=='seq' else 2,'0' if mode=='seq' else '0-1') if mode!='twins' else tcp.gpu_cmd(j,out)
  v=process('pilot-'+mode,cmd,d/mode,role='gpu' if mode=='twins' else 'cpu');v['validation']=compare_tcp(out,INPUT/j['reference']);v['event_loop_ns']=tcp.loop_ns((d/mode/'process.log').read_text());save(d/mode/'result.json',v);results.append(v)
  assert v['rc']==0 and v['event_loop_ns'] and (v['validation']['equal'] or mode=='u2'),mode
  if out.exists():out.unlink()
 for eng in ('simai','p0','p16'):
  label='iso18-pilot-'+eng;v=c.attempt(label,c.JOBS[0],eng,resource_context=dict(policy='pilot-not-formal-sample',co_run_allowed=False));assert v['accepted'],label
  ai.cleanup_raw(label)
 # MTP is a distinct scheduler, so mismatched times are recorded rather than rejected.
 j=c.JOBS[0];os.environ['TWINS_KEEP_REFERENCE_FCT']=str(E/'reference-cache')
 c.attempt('iso18-pilot-seq-cache',j,'simai',resource_context=dict(policy='pilot-not-formal-sample',co_run_allowed=False));os.environ.pop('TWINS_KEEP_REFERENCE_FCT',None)
 v=ai.mtp_attempt(j,1,1,'pilot');results.append(v)
 save(p,dict(utc_ns=time.time_ns(),tcp_and_ai_output_gates=True,mtp_pilot_complete=v['complete'],mtp_pilot_equal=v['validation'].get('byte_equal'),scope='pilot, never included in three formal observations'))
def pilot_pipeline():
 marker=EXPORT/'pilot-pipeline-finished.json'
 if marker.exists():return
 old_export=tcp.EXPORT;old_jobs=c.JOBS;old_broot=b.BROOT
 try:
  tcp.EXPORT=EXPORT/'pilot-pipeline';tcp.MANIFEST['pilot-scan']=MANIFEST['scan138'][:2]
  for engine,setting in [('cpu',2),('gpu',2)]:tcp.scan_one('pilot-scan',engine,setting,0)
  for p in (tcp.EXPORT/'tcp-scans').glob('*/result.json'):assert read(p)['all_accepted'],str(p)
  c.JOBS=old_jobs[:1];b.BROOT=EXPORT/'pilot-pipeline/ai-batches';b.BROOT.mkdir(parents=True,exist_ok=True);policy=read(ai.POLICY)
  for eng in ('simai','p0','p16'):
   idle_slot('pilot-batch-'+eng,required_gib=14);label='iso18-pilot-batch-'+eng;b.batch(label,eng,1,policy,resource_context=dict(policy='pilot-not-formal',co_running_batches_allowed=False));assert read(b.BROOT/(label+'.json'))['all_accepted']
  save(marker,dict(utc_ns=time.time_ns(),two_world_cpu_and_gpu_checks=True,single_job_cpu_p0_p16_batches=True,scope='actual short sub-process batches and output checks; never formal samples'))
 finally:tcp.EXPORT=old_export;c.JOBS=old_jobs;b.BROOT=old_broot
def main():
 ap=argparse.ArgumentParser();ap.add_argument('--stage',choices=['all','pilot','cubic','scans','ai-latency','mtp','throughput'],default='all');args=ap.parse_args()
 with (E/'exclusive-campaign.lock').open('w') as lock:
  fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  while not (E/'receipts/prepared.json').exists():state(stage='waiting_for_builds');time.sleep(20)
  freeze()
  stages=[('pilot',pilot),('pilot-pipeline',pilot_pipeline),('cubic',tcp.cubic),('scans',tcp.scans),('ai-latency',ai.latency),('mtp',ai.mtp),('throughput',ai.throughput)]
  for name,fn in stages:
   if args.stage not in ('all',name):continue
   while True:
    state(stage=name,status='running');fn();quality=quality_count(name)
    save(EXPORT/(name+'-quality.json'),quality)
    if quality['complete']:state(stage=name,status='finished',quality=quality);break
    state(stage=name,status='repeating_contaminated_timings',quality=quality);time.sleep(20)
  if args.stage=='all':save(EXPORT/'all-finished.json',dict(utc_ns=time.time_ns(),status='all predeclared groups executed; output acceptance remains per receipt'))
if __name__=='__main__':
 try:main()
 except BaseException as e:
  state(stage='controller_stopped',reason=repr(e),pending_work=True);raise
