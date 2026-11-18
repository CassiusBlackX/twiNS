#!/usr/bin/env python3
"""Freeze the two distinct TCP scans, eight CUBIC cases and original AI jobs.
No simulator execution. Original worlds and historical outputs remain unchanged.
"""
import argparse,hashlib,json,re,shutil
from pathlib import Path
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def main():
 a=argparse.ArgumentParser();a.add_argument('--repository',type=Path,required=True);a.add_argument('--evidence',type=Path,required=True);x=a.parse_args();R=x.repository;E=x.evidence;I=E/'inputs';I.mkdir(parents=True,exist_ok=True);files={}
 def retain(p):
  rel=str(p.relative_to(R));dest=I/rel;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,dest);files[rel]=dict(sha256=sha(p),bytes=p.stat().st_size);return rel
 cubic=[]
 for k in (8,16):
  for w in ('paper','a2a30','incast16','a2a50s'):
   n=f'k{k}_{w}';cubic.append(dict(case=n,k=k,tcp='cubic',flows=retain(R/f'data/flows/{n}.csv'),reference=retain(R/f'data/ns3/ref/{n}_TcpCubic.csv'),queue=200,ecnK=30,stop_s=.3,hostq='red',flow_ecmp=True))
 scan138=[]
 for w in ('k8_a2a30','k8_a2a50w'):
  flow=retain(R/f'data/flows/{w}.csv')
  for tcp,typ in [('dctcp','TcpDctcp'),('cubic','TcpCubic'),('newreno','TcpNewReno')]:
   for q in (100,200,400):
    for K in (5,10,20,30,40,60,80,120):
     if K>=q:continue
     name=f'{w}_{typ}_q{q}_K{K}';scan138.append(dict(case=name,k=8,flows=flow,tcp=tcp,queue=q,ecnK=K,reference=retain(R/f'data/ns3/sweep_k8/{name}.csv'),stop_s=.3))
 seed=[];source_worlds=[]
 for p in sorted((R/'out/ksweep_ms').glob('worlds_*.txt'),key=lambda p:int(re.search(r'worlds_(\d+)',p.name)[1])):
  source_worlds.append(dict(path=retain(p),sha256=sha(p)))
  for line in p.read_text().splitlines():
   if not line.strip():continue
   d=dict(v.split('=',1) for v in line.split(','));name=Path(d['out']).stem
   seed.append(dict(case=name,k=8,flows=retain(R/d['flows']),tcp=d['tcp'],queue=int(d['queue']),ecnK=int(d['ecnK']),reference=retain(R/d['out']),stop_s=.3,original_world_line=line))
 for rows,n in ((scan138,138),(seed,1260)):
  assert len(rows)==n and len({v['case'] for v in rows})==n
  keys=[json.dumps({k:v[k] for k in ('k','flows','tcp','queue','ecnK','stop_s')},sort_keys=True) for v in rows];assert len(set(keys))==n
  for row,key in zip(rows,keys):row['semantic_key_sha256']=hashlib.sha256(key.encode()).hexdigest()
 assert len({Path(v['flows']).stem for v in seed})==20
 m=dict(campaign='isolated-20261118',source_core='6bc5a7cbdafc5fed232474ea85d74c81b00aa126',cubic=cubic,scan138=scan138,scan1260=seed,source_worlds=source_worlds,files=files,policy=dict(rounds=3,cubic_cpu_threads=[0,1,2,4,8,16,32],scan_cpu_concurrency=[16,32],scan138_gpu_batch=[1,138],scan1260_gpu_batch_cap=32,tcp_fallback='64B then explicit128B for undecided worlds; all passes charged; remaining flags are reported, not accepted',ai_batch_budget_gib=70,ai_admission_order='same descending original old_simai_s as 20261116',timing='AI process wall and batch makespan; TCP CUBIC min-of-3 event loop, scan full wall through outputs; validation outside timers',isolation='one measured setting at a time across all five groups, retain external-activity samples; no cross-setting pipeline overlap'))
 (E/'manifest.json').write_text(json.dumps(m,indent=2)+'\n');print(json.dumps(dict(cubic=len(cubic),scan138=len(scan138),scan1260=len(seed),input_files=len(files),bytes=sum(x['bytes'] for x in files.values()))))
if __name__=='__main__':main()
