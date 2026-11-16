#!/usr/bin/env python3
"""Post-latency candidate validation. It does not replace or relabel frozen A1 timings."""
import csv,hashlib,json,re,subprocess,time,os
from pathlib import Path
import campaign as c
ROOT=c.ROOT;CAND=ROOT/'candidate';OUT=c.EXPORT/'candidate-gates';OUT.mkdir(exist_ok=True)
# The parallel latency parent uses a housekeeping core. This standalone post-latency
# build/gate stage can use all physical cores; model affinity remains explicit below.
os.sched_setaffinity(0,set(range(16)))
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def run(name,cmd,timeout=3600):
 d=OUT/name;d.mkdir(exist_ok=True);start=time.perf_counter_ns()
 with (d/'process.log').open('w') as f:
  try:rc=subprocess.run(cmd,stdout=f,stderr=subprocess.STDOUT,cwd=ROOT,timeout=timeout).returncode
  except subprocess.TimeoutExpired:rc=124
 v=dict(name=name,command=cmd,rc=rc,wall_ns=time.perf_counter_ns()-start);c.save(d/'run.json',v);return v,d
# All three builds use the archived source, distinct from the frozen A1 directory.
base=['cmake','-S',str(CAND),'-DTWINS_GPU_BACKEND=CUDA','-DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc','-DCMAKE_CUDA_ARCHITECTURES=120','-DTWINS_BUILD_EVIDENCE_TOOLS=ON']
results=[]
for label,defs,targets in [('default','',['twins','twins128','twins-rdma','twins-ai','twins-astra-stub','twins-island','twins-lineage-bench']),('timer','TWINS_TCP_TIMER_ORDER',['twins128']),('strict','TWINS_NO_FP',['twins-rdma'])]:
 build=CAND/('build-'+label);v,d=run('configure-'+label,base+['-B',str(build),'-DTWINS_EXTRA_DEFS='+defs]);results.append(v)
 if v['rc']:c.save(OUT/'summary.json',dict(status='configure_failed',steps=results));raise SystemExit(1)
 v,d=run('build-'+label,['cmake','--build',str(build),'--target',*targets,'-j6']);results.append(v)
 if v['rc']:c.save(OUT/'summary.json',dict(status='build_failed',steps=results));raise SystemExit(1)
v,d=run('saturated-count-prefix',[str(CAND/'build-default/twins-lineage-bench'),'--boundary-only']);results.append(v)
# Refuse the known unsupported packet/headroom regions before simulation.
config=ROOT/'validation/hpcc/hpcc.cfg';topo=ROOT/'data/topo/fattree8.txt';flow=ROOT/'data/rdma/k8_perm.csv';binary=CAND/'build-default/twins-rdma'
for label,args in [('mtu-zero',['PACKET_PAYLOAD_SIZE=0']),('mtu-overflow',['PACKET_PAYLOAD_SIZE=65535']),('headroom-overflow',['--delay=1000000000'])]:
 v,d=run(label,[str(binary),str(config),'--topo='+str(topo),'--flows='+str(flow),'--out='+str(OUT/(label+'.fct')),*args]);message='headroom exceeds the supported integer range' if label.startswith('headroom') else 'payload plus headers must fit a nonzero 16-bit wire size';v['expected_refusal']=v['rc']==1 and message in (d/'process.log').read_text(errors='replace');results.append(v)
# Reachable MMU cases: compare outputs, preserve stranded vs complete classification.
for case in sorted((CAND/'gate-inputs').iterdir()):
 if not case.is_dir():continue
 metadata=json.loads((case/'case.json').read_text());reference=case/'reference.fct'
 for variant,opts in [('safe',[]),('serial',['--serial']),('window',['--gwin=4']),('strict',[])]:
  selected=CAND/'build-strict/twins-rdma' if variant=='strict' else binary
  label='mmu-'+case.name+'-'+variant;v,d=run(label,[str(selected),str(CAND/'gate-inputs/config.cfg'),'--topo='+str(case/'topo'),'--flows='+str(case/'flows.csv'),'--out='+str(OUT/(label+'.fct')),*metadata['overrides'],*opts]);actual=OUT/(label+'.fct')
  def rows(p):return sorted(tuple(map(int,l.split())) for l in p.read_text().splitlines()) if p.exists() else None
  a,b=rows(actual),rows(reference);log=(d/'process.log').read_text(errors='replace');v.update(output_equal=a==b and a is not None,rows=len(a or []),reference_rows=len(b or []),final_flags=c.flags(log),status='stranded' if v['rc']==2 else 'complete' if v['rc']==0 else 'failed',binary_sha256=sha(selected),reference_sha256=sha(reference));c.save(d/'result.json',v);results.append(v)
# The three residual timer cases, same 128B frame size, extra exact representation only.
for name,tcp,options,ref in [('fin-newreno','newreno',['--k=8','--flows='+str(ROOT/'data/flows/k8_a2a50s.csv'),'--fin=1'],ROOT/'data/ns3/ref/k8_a2a50s_TcpNewReno_fin.csv'),('fin-cubic','cubic',['--k=8','--flows='+str(ROOT/'data/flows/k8_a2a50s.csv'),'--fin=1'],ROOT/'data/ns3/ref/k8_a2a50s_TcpCubic_fin.csv'),('grid-g075','newreno',['--topo=graph','--topofile='+str(ROOT/'data/grid/topo/hx2_8x4.txt'),'--flows='+str(ROOT/'data/grid/g075.csv'),'--queue=200','--ecnK=40','--spray=1','--fin=1'],ROOT/'data/ns3/grid/g075.csv')]:
 label='timer-'+name;v,d=run(label,[str(CAND/'build-timer/twins128'),*options,'--tcp='+tcp,'--out='+str(OUT/(label+'.csv'))]);actual=OUT/(label+'.csv')
 def rows(p):
  with p.open() as f:r=csv.reader(f);next(r);return [x[:6] for x in r]
 log=(d/'process.log').read_text(errors='replace');v.update(output_equal=rows(actual)==rows(ref),flagged='were undecided' in log,reference_sha256=sha(ref));c.save(d/'result.json',v);results.append(v)
# End-to-end closed loop on two representatives, all output checks, medians/ranges later.
c.BIN=CAND/'build-default';old_timeout=c.M['timeout_s'];c.M['timeout_s']=3600
ai=[]
for roundno in range(1,4):
 for name in ('c_allgather_8m_hpn128','c_allgather_512m_hpn1024','gpt3_13b_probe_hpn128'):
  job=c.BY.get(name) or dict(case=name,gpus='128',memory_cap_gib=12)
  for engine in ('p0','p16'):ai.append(c.attempt(f'candidate-r{roundno}-{name}-{engine}',job,engine))
c.M['timeout_s']=old_timeout
def accepted(v):
 if 'expected_refusal' in v:return v['expected_refusal']
 if 'output_equal' in v:return v['output_equal'] and v['rc'] in (0,2) and not v.get('flagged') and not v.get('final_flags',{}).get('present')
 return v['rc']==0
ok=all(accepted(x) for x in results) and all(x['accepted'] for x in ai)
c.save(OUT/'summary.json',dict(status='passed' if ok else 'failed',all_accepted=ok,candidate_source=json.loads((CAND/'source-manifest.json').read_text()),steps=results,ai_attempts=[x['label'] for x in ai],ai_all_accepted=all(x['accepted'] for x in ai),warning='candidate gates have a different source/binary hash from the original A3 timing group; do not pool versions'))
