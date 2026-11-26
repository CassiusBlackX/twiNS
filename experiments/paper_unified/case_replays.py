"""Recompute the archived MoE ranking, parent violations, and four native ECN witnesses."""
import argparse
from collections import Counter
import csv
import hashlib
import json
from pathlib import Path
p=argparse.ArgumentParser(description=__doc__)
p.add_argument('workspace',type=Path);p.add_argument('out',type=Path);a=p.parse_args()
root=a.workspace.resolve();out=a.out.resolve();out.mkdir(exist_ok=True)
w=root/'twiNS/out/simai/whatif'
sources={}
def read(path):
 sources[str(path)]=hashlib.sha256(path.read_bytes()).hexdigest();return path.read_text()
def messages(path):
 return {int(i):(int(t),int(f)) for i,t,f in (r.split() for r in read(path).splitlines())}
def dag(path):
 flows={};keys={};count=Counter();on={}
 for line in read(path).splitlines():
  v=line.split()
  if not v or v[0].startswith('#'):continue
  if v[0]=='flow':
   i=int(v[1]);key=tuple(map(int,v[2:5]));nth=count[key];count[key]+=1;keys[i]=key+(nth,);flows[i]=key
  elif v[0]=='on':on[v[1]]=v[2]
 return flows,keys,on
def makespan(m):return max(t+f for t,f in m.values())
results={'moe':{},'bandwidth_control':{},'ecn':{}}
_,base_keys,_=dag(w/'moe_sx.dag');base_ids={key:i for i,key in base_keys.items()}
for kind in ['hpn','g200']:
 reference=messages(w/f'moe_{kind}.ref');live=messages(w/f'moe.{kind}.closed');recorded=messages(w/f'moe.{kind}.cross')
 assert reference==live and len(reference)==6496
 flows,keys,causes=dag(w/f'moe_{kind}.dag');bad=[]
 for i in sorted(flows):
  parent=causes[f'f{i}'];visited=set()
  while parent.startswith('t'):
   assert parent not in visited;visited.add(parent);parent=causes[parent]
  if not parent.startswith('r'):continue
  j=int(parent[1:]);src_id=base_ids[keys[i]];par_id=base_ids[keys[j]]
  start=recorded[src_id][0];finish=sum(recorded[par_id])
  if start<finish:bad.append(dict(child=flows[i][:2],parent=flows[j][:2],start=start,parent_completed=finish))
 bad.sort(key=lambda r:r['start'])
 results['moe'][kind]=dict(messages=len(reference),live_matches=True,live_makespan_ns=makespan(live),recorded_makespan_ns=makespan(recorded),premature=len(bad),first=bad[:1])
assert results['moe']['hpn']['premature']==565
assert results['moe']['g200']['premature']==0
assert results['moe']['hpn']['live_makespan_ns']>results['moe']['g200']['live_makespan_ns']
assert results['moe']['hpn']['recorded_makespan_ns']<results['moe']['g200']['recorded_makespan_ns']
for kind in ['sx','g200']:
 ref=messages(w/f'a2a_{kind}.ref');live=messages(w/f'a2a.{kind}.closed');cross=messages(w/f'a2a.{kind}.self' if kind=='sx' else w/f'a2a.{kind}.cross')
 assert ref==live
 results['bandwidth_control'][kind]=dict(live_makespan_ns=makespan(live),recorded_makespan_ns=makespan(cross),messages=len(ref))
assert (results['bandwidth_control']['sx']['live_makespan_ns']<results['bandwidth_control']['g200']['live_makespan_ns']) == (results['bandwidth_control']['sx']['recorded_makespan_ns']<results['bandwidth_control']['g200']['recorded_makespan_ns'])
pairs=root/'materials/paper_data/ecn_tail_pairs.csv'
for record in csv.DictReader(read(pairs).splitlines()):
 seed=int(record['arrival_seed'])
 if seed not in [14,16,19,20]:continue
 identity=tuple(int(record[k]) for k in ['src','dst','bytes','start_ns'])
 arms={}
 for k in [30,40]:
  d=root/f'twins-paper-review/out/strict-20261123/ecn_tail/20261122T205212Z/a{seed}-o0-k{k}/observed'
  found=[r for r in csv.DictReader(read(d/'flows.csv').splitlines()) if tuple(int(r[x]) for x in ['src','dst','flow_size','start_ns'])==identity]
  assert len(found)==1;row=found[0];fct=int(row['completed_ns'])-int(row['start_ns']);connect=int(row['established_ns'])-int(row['start_ns'])
  assert fct==int(record[f'K{k}_fct_ns']) and connect==int(record[f'K{k}_connect_ns'])
  log=read(d/'process.log').splitlines()
  evidence=[r for r in log if r.startswith(('cwnd ','state ','drop ','tx '))]
  (out/f'ecn-{seed}-k{k}-events.txt').write_text('\n'.join(evidence)+'\n')
  arms[k]=dict(fct_ns=fct,connect_ns=connect,retx=int(row['retx']),event_rows=len(evidence))
 results['ecn'][seed]=dict(identity=identity,arms=arms)
results['sha256']=sources
(out/'summary.json').write_text(json.dumps(results,indent=2)+'\n')
print(json.dumps({k:v for k,v in results.items() if k!='sha256'},indent=2))
