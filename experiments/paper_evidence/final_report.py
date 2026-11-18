#!/usr/bin/env python3
"""Build final paper tables/plots from retained receipts; never execute models.

Usage: MPLCONFIGDIR=/tmp/twins-mpl python final_report.py --evidence PATH
Requires matplotlib only for PNG/SVG plots. All seconds derive from integer ns.
"""
import argparse,collections,csv,hashlib,json,statistics
from pathlib import Path
from receipt_status import terminal_profile

def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()
def read(p): return json.loads(p.read_text())
def stats(values):
 return dict(n=len(values),median_s=statistics.median(values),min_s=min(values),max_s=max(values),range_over_median=(max(values)-min(values))/statistics.median(values))
def write_csv(p,rows):
 with p.open('w',newline='') as f:
  w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
def main():
 ap=argparse.ArgumentParser();ap.add_argument('--evidence',type=Path,default=Path('/home/cassius/Work/SpecDES/twiNS/out/paper-evidence-20261116'));a=ap.parse_args();E=a.evidence
 O=E/'reports/final';O.mkdir(parents=True,exist_ok=True)
 jobs=read(Path(__file__).with_name('matrix.json'))['jobs'];names=[j['case'] for j in jobs];inputs={};samples=[];groups=collections.defaultdict(list)
 def record(p): inputs[str(p.relative_to(E))]=sha(p)
 for p in sorted((E/'s50/export/attempts').glob('*/result.json')):
  v=read(p)
  if not v['label'].startswith(('latency-','ablation-')):continue
  assert v['accepted'] and v['rc']==0 and v['validation']['equal'] and not v['flags']['present']
  assert v.get('dag_validation',{'equal':True})['equal']
  record(p);ctx=v.get('resource_context',{});epoch=ctx.get('policy','original-serial')
  r=dict(label=v['label'],case=v['case'],engine=v['engine'],wall_ns=v['wall_ns'],seconds=v['wall_ns']/1e9,affinity=v['affinity'],resource_epoch=epoch,co_run_allowed=ctx.get('co_run_allowed',False),observed_interference=v.get('interference',{}).get('suspected',False),binary_sha256=v['binary_sha256'],receipt=str(p.relative_to(E)))
  samples.append(r)
  if v['label'].startswith('latency-'): groups[(v['case'],v['engine'])].append(r)
 assert len([r for r in samples if r['label'].startswith('latency-')])==78
 latency=[]
 for name in names:
  for eng in ('simai','p0','p16'):
   rs=groups[name,eng];assert len(rs)>=3
   latency.append(dict(case=name,engine=eng,**stats([r['seconds'] for r in rs]),resource_epochs=dict(collections.Counter(r['resource_epoch'] for r in rs)),by_epoch={ep:stats([r['seconds'] for r in rs if r['resource_epoch']==ep]) for ep in sorted({r['resource_epoch'] for r in rs})},receipts=[r['receipt'] for r in rs]))
 batches=[];job_checks=[]
 for p in sorted((E/'s50/export/batches-v2').glob('*.json')):
  v=read(p)
  if 'makespan_ns' not in v:continue
  record(p);assert v['all_accepted'] and len(v['jobs'])==8 and set(v['jobs'])==set(names)
  key='cpu-c'+str(v['target_concurrency']) if v['engine']=='simai' else v['engine']
  batches.append(dict(setting=key,label=v['label'],trial=v['trial'],makespan_ns=v['makespan_ns'],seconds=v['makespan_ns']/1e9,target_concurrency=v['target_concurrency'],peak_actual_concurrency=v['peak_actual_concurrency'],budget_gib=v['budget_gib'],receipt=str(p.relative_to(E))))
  ps=list((p.parent/v['trial']).glob('*/result.json'));assert len(ps)==8
  cases=[]
  for q in ps:
   x=read(q);record(q);cases.append(x['case']);good=x['accepted'] and x['rc']==0 and x['validation']['equal'] and not x['flags']['present'] and x.get('dag_validation',{'equal':True})['equal'];assert good,str(q)
   job_checks.append(dict(case=x['case'],engine=x['engine'],batch=v['label'],receipt=str(q.relative_to(E)),accepted=good))
  assert set(cases)==set(names)
 assert len(batches)==18 and len(job_checks)==144
 throughput=[]
 for setting in ('cpu-c1','cpu-c2','cpu-c4','cpu-c8','p0','p16'):
  rs=[r for r in batches if r['setting']==setting];assert len(rs)==3
  throughput.append(dict(setting=setting,**stats([r['seconds'] for r in rs]),actual_peaks=sorted({r['peak_actual_concurrency'] for r in rs}),receipts=[r['receipt'] for r in rs]))
 best=min((r for r in throughput if r['setting'].startswith('cpu')),key=lambda r:r['median_s'])
 for r in throughput:r['speedup_vs_best_observed_cpu']=best['median_s']/r['median_s'];r['jobs_per_hour']=8*3600/r['median_s']
 ablations=[]
 for setting in ('serial','gwin0','both'):
  rs=[r for r in samples if r['label'].startswith('ablation-') and r['label'].endswith('-'+setting)];assert len(rs)==3
  ablations.append(dict(setting=setting,**stats([r['seconds'] for r in rs]),receipts=[r['receipt'] for r in rs]))
 metadata=dict(latency_count=78,ablation_count=9,batch_count=18,batch_job_count=144,host='s50 Ryzen 9 9950X + one RTX 5090',budget_gib=70,best_observed_cpu_setting=best['setting'],timing_scope='process wall including startup and AI stub; batch makespan includes admission and excludes subsequent validation/provenance',source_policy='A3/throughput use original frozen A1 source, not A2 48e0a3a or later 6bc5a7c candidate',resource_policy='original serial and parallel resource epochs are retained; SimAI/P0 may co-run on disjoint physical cores, P16 has no campaign CPU co-runners; shared bandwidth/foreign work not fully isolated',uncertainty='observed min/median/max only, no confidence interval or isolated-performance inference',cpu_scaling='target c8 only reaches four simultaneous jobs under 70GiB; do not claim eight-core scaling',ablation_scope='ablation runs are serial-resource observations; original P0 baseline has two original-serial points and one parallel point; baseline contrast is descriptive, not an isolated causal estimate')
 strict=[];growth=[];micro=[]
 for p in sorted((E/'local/profile-final').glob('*strict/result.json')):
  x=terminal_profile(read(p),p);assert x['accepted'];record(p);record(p.parent/'process.log')
  strict.append(dict(case=x['case'],seconds=x['wall_ns']/1e9,internal_retries=x.get('internal_retries',0),accepted=x['accepted'],receipt=str(p.relative_to(E)),classification='terminal flags; wall includes every retry'))
 assert len(strict)==4
 for p in sorted((E/'local/growth').glob('*/result.json')):
  x=read(p);assert x['rc']==0 and x['equal'] and not x['flagged'];record(p)
  growth.append(dict(scale=x['scale'],variant=x['variant'],seconds=x['wall_ns']/1e9,receipt=str(p.relative_to(E))))
 assert len(growth)==12
 for p in sorted((E/'local/corpus').glob('*/micro-isolated-result.json')):
  x=read(p);assert x['accepted'];record(p);phases=collections.defaultdict(list)
  for r in x['records']:
   if 'phase' in r:assert r['mismatches']==0;phases[r['phase']].append(r['gpu_ms'])
  for phase,values in phases.items():
   assert len(values)==3
   micro.append(dict(case=p.parent.name,phase=phase,n=3,median_ms=statistics.median(values),min_ms=min(values),max_ms=max(values),receipt=str(p.relative_to(E))))
 out=dict(metadata=metadata,latency=latency,throughput=throughput,ablations=ablations,samples=samples,batches=batches,underlying_job_checks=job_checks,strict=strict,growth=growth,micro=micro)
 (O/'paper-data.json').write_text(json.dumps(out,indent=2)+'\n');write_csv(O/'latency-samples.csv',samples);write_csv(O/'throughput-samples.csv',batches)
 write_csv(O/'latency-summary.csv',[{k:v for k,v in r.items() if k not in ('resource_epochs','by_epoch','receipts')} for r in latency]);write_csv(O/'throughput-summary.csv',[{k:v for k,v in r.items() if k not in ('actual_peaks','receipts')} for r in throughput])
 lines=['# Final measured tables, 2026-11-18','','All seconds derive from integer nanoseconds. Intervals are observed min–max, not confidence intervals. Mixed resource epochs remain visible in `paper-data.json` and `latency-samples.csv`.','','## A3 latency','','| Case | SimAI median [min,max], n | P0 median [min,max], n | P16 median [min,max], n | SimAI/P0 | SimAI/P16 |','|---|---:|---:|---:|---:|---:|']
 lookup={(r['case'],r['engine']):r for r in latency}
 def cell(r):return f"{r['median_s']:.6f} [{r['min_s']:.6f}, {r['max_s']:.6f}], {r['n']}"
 for name in names:
  rs=[lookup[name,e] for e in ('simai','p0','p16')];lines.append('| '+name+' | '+' | '.join(cell(r) for r in rs)+f" | {rs[0]['median_s']/rs[1]['median_s']:.3f}× | {rs[0]['median_s']/rs[2]['median_s']:.3f}× |")
 lines+=['','## Same job-set batch makespan','','| Setting | Median [min,max] seconds, n | Actual peak jobs | Jobs/hour | Best CPU / setting |','|---|---:|---:|---:|---:|']
 for r in throughput: lines.append(f"| {r['setting']} | {cell(r)} | {r['actual_peaks']} | {r['jobs_per_hour']:.3f} | {r['speedup_vs_best_observed_cpu']:.3f}× |")
 lines+=['',f"Best observed CPU setting is {best['setting']}. Target c8 reaches only four simultaneous jobs. The small c4/c8 timing difference does not establish eight-core scaling.",'','## Safe/window ablations: 512MiB allgather, 1024 GPUs','','| Setting | Median [min,max] seconds, n |','|---|---:|']
 for r in ablations:lines.append(f"| {r['setting']} | {cell(r)} |")
 lines+=['','Default P0 baseline: '+cell(lookup['c_allgather_512m_hpn1024','p0'])+'. Baseline has mixed resource epochs; ablations have no own CPU co-runners. Do not interpret the contrast as a fully isolated causal estimate.','','## Interpretation constraints','',*['- '+metadata[k] for k in ('timing_scope','source_policy','resource_policy','uncertainty','cpu_scaling','ablation_scope')]]
 lines+=['','## A2 direct traversal (one attempt per case, frozen 48e0a3a)','','| Case | Whole-process seconds | Internal retries | Final accepted |','|---|---:|---:|---:|']
 for r in strict:lines.append(f"| {r['case']} | {r['seconds']:.9f} | {r['internal_retries']} | {r['accepted']} |")
 lines+=['','TWINS_NO_FP retains FP storage and updates. Retry-inclusive differences are not pure FP instruction cost. Clean/profile medians and storage ledger remain in ../a2-paper-tables.md.','','## A2 growth (flow-size multiplier only; each point once)','','| Multiplier | Clean seconds | Profile seconds | Strict seconds |','|---|---:|---:|---:|']
 for scale in (1,2,4,8):lines.append('| '+str(scale)+' | '+' | '.join(f"{next(r['seconds'] for r in growth if r['scale']==scale and r['variant']==v):.9f}" for v in ('clean','profile','strict'))+' |')
 lines+=['','## Corrected isolated microbenchmarks','','GPU milliseconds for the fixed recorded corpus and benchmark repeat loop, not per-comparison cost or end-to-end wall. Reconstructed contiguous layout; untimed warmup before each timed round. Do not sum stages as wall. Old micro-v1 and sampling-hook FP timings are excluded.','','| Case | Phase | Median [min,max] GPU ms, n=3 |','|---|---|---:|']
 for r in micro:lines.append(f"| {r['case']} | {r['phase']} | {r['median_ms']:.9f} [{r['min_ms']:.9f}, {r['max_ms']:.9f}] |")
 (O/'paper-tables.md').write_text('\n'.join(lines)+'\n')
 import matplotlib;matplotlib.use('Agg')
 import matplotlib.pyplot as plt
 import numpy as np
 plt.rcParams.update({'font.size':10,'svg.fonttype':'none'})
 short=['Allgather 8MiB /128','Allgather 512MiB /1024','GPT-3 13B full /128','LLaMA 65B full /128','GPT-3 175B full /128','GPT-3 13B mid /512','LLaMA 65B mid /1024','GPT-3 175B probe /1024']
 fig,ax=plt.subplots(figsize=(11.2,5.6));colors=['#555555','#0072b2','#d55e00']
 for i,eng in enumerate(('simai','p0','p16')):
  rs=[lookup[n,eng] for n in names];y=np.arange(8)+(i-1)*.22;med=np.array([r['median_s'] for r in rs]);err=np.array([[r['median_s']-r['min_s'] for r in rs],[r['max_s']-r['median_s'] for r in rs]])
  ax.errorbar(med,y,xerr=err,fmt='o',markersize=4,capsize=3,color=colors[i],label={'simai':'Sequential SimAI','p0':'TwiNS P=0','p16':'TwiNS P=16'}[eng])
 ax.set(yticks=np.arange(8),yticklabels=short,xscale='log',xlabel='Process wall time (seconds, log scale)',title='AI latency: median and observed range (n=3 or 5)');ax.invert_yaxis();ax.grid(axis='x',alpha=.2);ax.legend(loc='upper right');fig.tight_layout()
 for ext in ('png','svg'):fig.savefig(O/('latency.'+ext),dpi=180)
 plt.close(fig)
 fig,ax=plt.subplots(figsize=(9,4.5));labels=['CPU target1','CPU target2','CPU target4','CPU target8\n(actual peak4)','TwiNS P=0','TwiNS P=16'];med=np.array([r['median_s']/3600 for r in throughput]);err=np.array([[(r['median_s']-r['min_s'])/3600 for r in throughput],[(r['max_s']-r['median_s'])/3600 for r in throughput]])
 ax.bar(np.arange(6),med,color=['#777777']*4+colors[1:],width=.65);ax.errorbar(np.arange(6),med,yerr=err,fmt='none',ecolor='black',capsize=4)
 for i,r in enumerate(throughput):ax.text(i,med[i]+.14,f"{r['median_s']/60:.2f} min",ha='center',fontsize=9)
 ax.set(xticks=np.arange(6),xticklabels=labels,ylabel='Eight-job batch makespan (hours)',ylim=(0,7.1),title='Same eight jobs, 70GiB reservation budget: median and range, n=3');ax.grid(axis='y',alpha=.2);ax.set_axisbelow(True);fig.tight_layout()
 for ext in ('png','svg'):fig.savefig(O/('throughput.'+ext),dpi=180)
 plt.close(fig)
 provenance=dict(generator=str(Path(__file__).resolve()),generator_sha256=sha(Path(__file__)),matrix_sha256=sha(Path(__file__).with_name('matrix.json')),input_receipts=inputs,artifacts={p.name:sha(p) for p in O.iterdir() if p.is_file() and p.name!='provenance.json'},scope='all final tables/plots are derived locally from archived receipts, without running models; historical/raw receipts remain unchanged')
 (O/'provenance.json').write_text(json.dumps(provenance,indent=2)+'\n')
 print(json.dumps(dict(metadata=metadata,throughput=throughput)))
if __name__=='__main__':main()
