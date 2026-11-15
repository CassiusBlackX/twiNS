#!/usr/bin/env python3
"""Canonical configurations + explicit suite membership. Never infer missing old pass costs."""
import hashlib,itertools,json,re,shlex
from pathlib import Path
ROOT=Path('/home/cassius/Work/SpecDES/twiNS');OUT=ROOT/'out/paper-evidence-20261116/manifests';OUT.mkdir(parents=True,exist_ok=True)
rows=[]
hash_cache={}
def sha(p):
 if not p or not p.is_file():return None
 k=str(p)
 if k not in hash_cache:
  h=hashlib.sha256()
  with p.open('rb') as f:
   while b:=f.read(8<<20):h.update(b)
  hash_cache[k]=h.hexdigest()
 return hash_cache[k]
def add(suite,name,kind,args,ref):
 files={}
 for a in args:
  if '=' in a:
   p=ROOT/a.split('=',1)[1]
   if p.is_file():files[a.split('=',1)[0]]=sha(p)
 canon={a.split('=',1)[0]:(a.split('=',1)[1] if '=' in a else '1') for a in args if a!='--general'}
 if kind=='tcp':
  for k,v in {'--ecnK':'30','--queue':'200','--fin':'0','--spray':'0'}.items():canon.setdefault(k,v)
 core={'kind':kind,'args':dict(sorted(canon.items())),'input_sha256':files,'reference_sha256':sha(ref)}
 canonical=hashlib.sha256(json.dumps(core,sort_keys=True).encode()).hexdigest()
 rows.append(dict(suite=suite,case=name,kind=kind,args=args,reference=str(ref) if ref else None,reference_sha256=sha(ref),input_sha256=files,semantic_config_sha256=canonical,execution_config_sha256=hashlib.sha256(json.dumps([core,args]).encode()).hexdigest(),prior_attempt_cost_status='not reconstructed: do not infer from final-pass or grouped logs'))
def tcp(suite,w,t,variant='base',extra=()):
 T={'dctcp':'TcpDctcp','newreno':'TcpNewReno','cubic':'TcpCubic'}[t]
 work=w if variant in ('base','fin','spray','sprayfin') else w+'_'+variant
 p=ROOT/f'data/flows/{w}.csv.topo'
 args=shlex.split(next(x.removeprefix('# twins ') for x in p.read_text().splitlines() if x.startswith('# twins '))) if p.exists() else ['--k='+re.match('k([0-9]+)',w)[1]]
 args+=['--flows=data/flows/'+work+'.csv','--tcp='+t,*extra]
 if variant in ('fin','sprayfin'):args+=['--fin=1']
 if variant in ('spray','sprayfin'):args+=['--spray=1']
 n=w+'_'+T+('' if variant=='base' else '_'+variant)
 add(suite,n,'tcp',args,ROOT/f'data/ns3/ref/{n}.csv')
K8='k8_paper k8_fullsync k8_fullexp k8_a2a30 k8_incast16 k8_a2a50s'.split();R='k8_r30 k8_r50 k8_r40p1 k8_rpc ls_r30 k16_r30'.split();T=['dctcp','newreno','cubic']
def expand(ws,vs=['base'],extra=()):
 for w,t,v in itertools.product(ws,T,vs):tcp('tcp-main',w,t,v,extra)
expand(K8+'ls_paper ls_a2a30 ls_incast16 ls_a2a50s k16_paper k16_a2a30 k16_incast16 k16_a2a50s'.split())
expand(K8,['jit','jit1','jit2','jit3','jit4'])
expand('k8_paper k8_a2a30 k8_a2a50s ls_a2a30 k16_a2a30'.split(),extra=['--general'])
expand('k8_a2a30b k8_a2a50b k8_incastb ls_a2a30b k16_a2a30b'.split());expand(R);expand(R+'k8_a2a30 k8_incast16 k8_a2a50s k8_a2a30b'.split(),['fin'])
expand('k8_paper k8_a2a30 k8_incast16 k8_a2a50s k8_a2a30b ls_a2a30 k16_a2a30'.split(),['spray']);expand(['k8_r30'],['sprayfin'])
expand('jf32_a2a30 jf32_a2a50 jf32_incast torus6_a2a30 torus6_a2a50 df4x9_a2a30 df4x9_a2a50 jf256_a2a30 ft8f_a2a30 ft8f_a2a50s jf32f_a2a50 torus6f_a2a30'.split(),['base','spray'])
assert len(rows)==306
for l in (ROOT/'validation/grid/jobs_twins.txt').read_text().splitlines():
 v=shlex.split(l)
 if v and not v[0].startswith('#'):add('tcp-grid',v[0],'tcp',v[1:],ROOT/f'data/ns3/grid/{v[0]}.csv')
for group in ('topo','cdf'):
 for l in (ROOT/f'validation/ns3/jobs_{group}.txt').read_text().splitlines():
  if not l.strip() or l.startswith('#'):continue
  n=l.split()[0];work,tcpn=n.rsplit('_Tcp',1)
  if not (ROOT/f'data/ns3/ref/{n}.csv').exists():continue
  tcp('tcp-'+group,work,tcpn.lower())
for k in (8,16):
 for p in sorted((ROOT/f'data/ns3/sweep_k{k}').glob('*.csv')):
  m=re.fullmatch(r'(.+)_Tcp(Dctcp|NewReno|Cubic)_q([0-9]+)_K([0-9]+)',p.stem)
  if not m:raise ValueError(str(p))
  w,t,q,K=m.groups()
  add(f'tcp-sweep-k{k}',p.stem,'tcp',['--k='+str(k),'--flows=data/flows/'+w+'.csv','--tcp='+t.lower(),'--queue='+q,'--ecnK='+K],p)
def findfile(n):
 for d in ('data/rdma','data/topo','data/flows'):
  p=ROOT/d/n
  if p.exists():return p
 return ROOT/n
for file in sorted(list((ROOT/'validation/hpcc').glob('jobs*.txt'))+[ROOT/'validation/hpcc/sweep_k8.txt']):
 for l in file.read_text().splitlines():
  if not l.strip() or l.lstrip().startswith('#'):continue
  v=shlex.split(l)
  if not v or v[0].startswith('#'):continue
  n,t,f,*rest=v;rf=ROOT/f'out/rdma/ref/{n}.fct'
  if not rf.exists():rf=ROOT/f'out/reconcile_20261116/historical_audit/hpcc_oracle/{n}.fct'
  add('rdma-'+file.stem,n,'rdma',['--topo='+str(findfile(t).relative_to(ROOT)),'--flows='+str(findfile(f).relative_to(ROOT)),*rest],rf)
for file in sorted((ROOT/'validation/simai').glob('jobs*.txt')):
 for l in file.read_text().splitlines():
  if not l.strip() or l.lstrip().startswith('#'):continue
  v=shlex.split(l)
  if not v or v[0].startswith('#') or len(v)<2:continue
  n,t,*rest=v;rf=ROOT/f'out/simai/paper/{n}.ref'
  if not rf.exists():
   choices=[q for folder in ('e2e','ref') for q in (ROOT/f'out/simai/{folder}').glob(n+'.ref')]
   if len(choices)==1:rf=choices[0]
  add('ai-'+file.stem,n,'ai',['--topo=data/simai/'+t,*rest],rf)
# The separately defined closed-loop network/buffer what-if matrix (not 47 new paper workloads).
wl={'mar':(128,'microAR_tp128'),'moe':(16,'moe_16'),'ar64':(128,'paper/c_allreduce_64m_128'),'llp':(128,'paper/llama_65b_probe_128'),'a2a':(128,'paper/c_alltoall_64m_128')}
for ref in sorted((ROOT/'out/simai/whatif').glob('*.ref')):
 load,config=ref.stem.rsplit('_',1)
 if load not in wl:continue
 g,w=wl[load];topo=(f'{config}{g}.topo' if config in ('hpn','dcn') else f'dcn{g}.topo' if config.startswith('dcnb') else f'sx{g}_200.topo' if config=='g200' else f'sx{g}.topo')
 args=['--topo=data/simai/'+topo,'--astra=out/simai/wl/'+w+'.txt']
 if config=='cc3':args+=['CC_MODE=3']
 elif config.startswith('dcnb'):args+=['BUFFER_SIZE='+config[4:]]
 elif re.fullmatch(r'b[0-9]+',config):args+=['BUFFER_SIZE='+config[1:]]
 add('ai-whatif-closed',ref.stem,'ai-whatif',args,ref)
 rows[-1]['reference_dag_sha256']=sha(ref.with_suffix('.dag'))
(OUT/'scenario-inventory.json').write_text(json.dumps(rows,indent=2)+'\n')
suites={x['suite']:sum(y['suite']==x['suite'] for y in rows) for x in rows};groups={}
for x in rows:groups.setdefault(x['semantic_config_sha256'],[]).append([x['suite'],x['case']])
summary=dict(suite_entries=suites,total_entries=len(rows),semantic_keys=len(groups),keys_with_multiple_memberships=sum(len(v)>1 for v in groups.values()),missing_reference_entries=sum(x['reference_sha256'] is None for x in rows),overlap_groups=[v for v in groups.values() if len(v)>1],limitation='input inventory, not a statement of acceptance or historical per-pass cost')
(OUT/'inventory-summary.json').write_text(json.dumps(summary,indent=2)+'\n');print({k:v for k,v in summary.items() if k!='overlap_groups'})
