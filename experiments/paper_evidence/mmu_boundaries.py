#!/usr/bin/env python3
"""Small reachable-state cases; match only common half-open slot boundaries."""
import hashlib,json,os,re,subprocess,time
from pathlib import Path
SRC=Path(__file__).resolve().parents[2];ROOT=Path('/home/cassius/Work/SpecDES/twiNS/out/paper-evidence-20261116');OUT=Path(os.environ.get('TWINS_MMU_REPRO_OUT',str(ROOT/'local/mmu/boundaries_v3')));OUT.mkdir(parents=True,exist_ok=True)
ORACLE=ROOT/'oracle/portable-source/build/scratch/ns3.36.1-simai-ref';GPU=SRC/'build-mmu-audit/twins-rdma'
config=(ROOT/'local/mmu/mixed.cfg').read_text();config=re.sub(r'^KMIN_MAP.*$', 'KMIN_MAP 3 1000000000 100 25000000000 100 100000000000 400',config,flags=re.M);config=re.sub(r'^KMAX_MAP.*$', 'KMAX_MAP 3 1000000000 400 25000000000 400 100000000000 1600',config,flags=re.M);config=re.sub(r'^PMAX_MAP.*$', 'PMAX_MAP 3 1000000000 0.2 25000000000 0.2 100000000000 0.2',config,flags=re.M)
(OUT/'config.cfg').write_text(config)
normal='5 1 0 1 4 H100\n4\n0 4 100Gbps 1000ns 0\n1 4 100Gbps 1000ns 0\n2 4 100Gbps 1000ns 0\n3 4 25Gbps 1000ns 0\n'
cases=[dict(name='safe-small',topo=normal,flows='src,dst,flow_size,start_ns\n0,3,1000,0\n',buffer=16,step=1000),dict(name='pause-resume',topo=normal,flows='src,dst,flow_size,start_ns\n0,3,262144,0\n1,3,262144,0\n2,3,262144,0\n',buffer=1,step=1000),dict(name='unsigned-wrap',topo=normal,flows='src,dst,flow_size,start_ns\n0,3,20000,0\n1,3,20000,0\n2,3,20000,0\n',buffer=0,step=1000),dict(name='low-headroom',topo=(ROOT/'local/mmu/low_headroom.topo').read_text(),flows=(ROOT/'local/mmu/one.csv').read_text(),buffer=1,step=680)]
cases.append(dict(name='multislot',topo='6 1 0 2 5 H100\n4 5\n0 4 100Gbps 1000ns 0\n1 4 100Gbps 1000ns 0\n4 5 100Gbps 1000ns 0\n2 5 100Gbps 100ns 0\n3 5 25Gbps 1000ns 0\n',flows='src,dst,flow_size,start_ns\n0,3,20000,0\n1,3,20000,0\n',buffer=1,step=100))
cases.append(dict(name='nvl-idle-global-window',topo='7 2 2 1 8 H100\n4 5\n6\n0 4 2880Gbps 25ns 0\n1 4 2880Gbps 25ns 0\n2 5 2880Gbps 25ns 0\n3 5 2880Gbps 25ns 0\n0 6 100Gbps 1000ns 0\n1 6 100Gbps 1000ns 0\n2 6 100Gbps 1000ns 0\n3 6 100Gbps 1000ns 0\n',flows='src,dst,flow_size,start_ns\n0,2,20000,0\n1,3,20000,0\n',buffer=1,step=25))
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def run(d,bin,args,env):
 d.mkdir(parents=True,exist_ok=True);cmd=[str(bin),str(OUT/'config.cfg'),*args,'--out='+str(d/'fct'),'--txtrace='+str(d/'tx')]
 with (d/'process.log').open('w') as f:
  start=time.perf_counter_ns()
  try:rc=subprocess.run(cmd,stdout=f,stderr=subprocess.STDOUT,cwd=d,env={**os.environ,**env},timeout=120).returncode
  except subprocess.TimeoutExpired:rc=124
  wall=time.perf_counter_ns()-start
 v=dict(command=cmd,rc=rc,wall_ns=wall,binary_sha256=sha(bin),fct_sha256=sha(d/'fct') if (d/'fct').exists() else None,tx_sha256=sha(d/'tx') if (d/'tx').exists() else None)
 (d/'run.json').write_text(json.dumps(v,indent=2));return v

def states(path):
 states={};duplicates=0;modes={};lines=path.read_text(errors='replace').splitlines()
 for l in lines:
  if not l.startswith('mmu_boundary '):continue
  v=list(map(int,l.split()[1:]));key=tuple(v[:3])
  if key in states:duplicates+=1
  states[key]=v[3:9];modes[key]=v[9:11]
 return states,duplicates,modes

def fct(p):return sorted(tuple(map(int,l.split())) for l in p.read_text().splitlines()) if p.exists() else None
allresults=[]
for c in cases:
 base=OUT/c['name'];base.mkdir(exist_ok=True);(base/'topo').write_text(c['topo']);(base/'flows.csv').write_text(c['flows']);args=['--topo='+str(base/'topo'),'--flows='+str(base/'flows.csv'),'BUFFER_SIZE='+str(c['buffer']),'CC_MODE=3','SIMULATOR_STOP_TIME=0.005']
 if c['name']=='pause-resume':args+=['HAS_WIN=0','CC_MODE=1','ENABLE_QCN=0']
 ref=base/'reference';r=run(ref,ORACLE,args,dict(TWINS_MMU_SNAPSHOT=str(ref/'boundaries'),TWINS_MMU_STEP=str(c['step'])))
 # Observer control establishes that adding initial boundary events preserves flow outputs.
 plain=base/'reference-no-observer';plainrun=run(plain,ORACLE,args,{})
 rs,rd,_=states(ref/'boundaries');refrows=fct(ref/'fct');observer_ok=refrows==fct(plain/'fct')
 for label,opts in [('safe',[]),('serial',['--serial']),('window',['--gwin=4']),('window-serial',['--gwin=4','--serial'])]:
  d=base/label;v=run(d,GPU,args+opts,{});gs,gd,modes=states(d/'process.log');common=sorted(gs.keys() & rs.keys());mismatch=[dict(key=k,gpu=gs[k],reference=rs[k]) for k in common if gs[k]!=rs[k]];missing_all=sorted(gs.keys()-rs.keys());last_ref=max(k[0] for k in rs) if rs else -1;missing=[k for k in missing_all if k[0]<=last_ref];after_stop=[k for k in missing_all if k[0]>last_ref];fctequal=fct(d/'fct')==refrows
  log=(d/'process.log').read_text(errors='replace');v.update(case=c['name'],variant=label,observer_control_equal=observer_ok,reference_rows=len(refrows) if refrows is not None else None,actual_rows=len(fct(d/'fct') or []),fct_equal=fctequal,common_boundary_ports=len(common),boundary_mismatches=len(mismatch),examples=mismatch[:20],missing_reference_keys=missing[:20],uncompared_after_reference_stop=after_stop[:20],duplicate_gpu_keys=gd,duplicate_reference_keys=rd,safe_boundaries=sum(x[0] for x in modes.values()),observed_boundaries=len(modes),multislot_boundaries=sum(x[1]>1 for x in modes.values()),final_flags=bool(re.search(r'warning:.*(?:not exact|ties the chains)|error:',log)),diagnostics=[x for x in log.splitlines() if x.startswith(('prof: switch ties','pv: switch windows','drops '))])
  v['reference_agreement']=bool(observer_ok and fctequal and common and not mismatch and not missing and not gd and not rd and not v['final_flags'] and v['rc'] in (0,2))
  (d/'result.json').write_text(json.dumps(v,indent=2)+'\n');allresults.append(v);print(json.dumps({k:v[k] for k in ('case','variant','rc','fct_equal','common_boundary_ports','boundary_mismatches','reference_agreement')}),flush=True)
(OUT/'results.json').write_text(json.dumps(allresults,indent=2)+'\n')
