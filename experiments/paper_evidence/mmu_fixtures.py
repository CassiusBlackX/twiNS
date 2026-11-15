#!/usr/bin/env python3
"""Compile the arithmetic verbatim from rstep; independently enumerate boundary expectations."""
from pathlib import Path
import hashlib,itertools,json,subprocess
ROOT=Path(__file__).resolve().parents[2]; OUT=Path('/home/cassius/Work/SpecDES/twiNS/out/paper-evidence-20261116/local/mmu/fixed-fixtures');OUT.mkdir(parents=True,exist_ok=True)
s=(ROOT/'src/rdma/rstep.cuh').read_text();begin=s.index('        int64_t amax = sw.shared_used');end=s.index('        safe = !__syncthreads_or(bad);',begin)+len('        safe = !__syncthreads_or(bad);');body=s[begin:end]
cpp='''#include <cstdint>
#include <iostream>
constexpr uint32_t RESERVE=4096;
struct SW {uint32_t shared_used,total_hdrm,total_rsrv,max_shift;};
struct PC {uint32_t hdrm_cap;};struct Sim {PC pc[1];int pbase[1];};
struct WP_t {uint32_t buffer_size;};struct Port {uint32_t ing;};struct W {Port pt;};
int __syncthreads_or(int x){return x;}
int main(){uint32_t B,H,R,S,shift,ing,ab,swbad,hcap;while(std::cin>>B>>H>>R>>S>>shift>>ing>>ab>>swbad>>hcap){SW sw{S,H,R,shift};WP_t WP{B};Sim s{{{hcap}},{0}};int x=0;W w[1]={{{ing}}};uint32_t abytes[1]={ab};uint64_t sw_sum=ab;int sw_bad=swbad;int lane=0,lanes=1,deg=1,bad=0;bool safe=true;
'''+body+'''
std::cout<<safe<<" "<<room<<" "<<th_min<<"\\n";}}
'''
(OUT/'predicate-extracted.cc').write_text(cpp);subprocess.run(['c++','-O2','-std=c++17','-fsanitize=undefined',str(OUT/'predicate-extracted.cc'),'-o',str(OUT/'predicate-extracted')],check=True)
fixtures=[]
# B-H-R=-1/0/+1 and Q-Smax=-1/0/+1; exact threshold equality and reserve crossings.
for difference,shift in itertools.product((-1,0,1,7,8,4096,65536),(0,1,3,31)):
 B=65536;H=4096;R=(B-H-difference)&0xffffffff;Q=(B-H-R)&0xffffffff
 for shared in sorted({0,max(0,Q-1),Q,min(0xffffffff,Q+1)}):
  for ing,ab in ((4095,0),(4096,0),(4097,0),(0,1000),(4096,1),(0xffffffff,1)):
   fixtures.append(dict(B=B,H=H,R=R,S=shared,shift=shift,ing=ing,ab=ab,swbad=0,hcap=0xffffffff,domain='synthetic arithmetic, not a reachable network-state assertion'))
for bad in (1,2):fixtures.append(dict(B=1<<20,H=3000,R=4096,S=0,shift=3,ing=0,ab=1000,swbad=bad,hcap=0xffffffff,domain='headroom/pause precondition'))
fixtures.append(dict(B=1048576,H=1029630,R=16384,S=0,shift=3,ing=0,ab=1064,swbad=0,hcap=255,domain='reachable mixed-rate admission regression'))
text=''.join(' '.join(str(x[k]) for k in ('B','H','R','S','shift','ing','ab','swbad','hcap'))+'\n' for x in fixtures)
p=subprocess.run([str(OUT/'predicate-extracted')],input=text,text=True,capture_output=True,check=True);results=[]
for x,line in zip(fixtures,p.stdout.splitlines(),strict=True):
 safe,room,th=map(int,line.split());Q=(x['B']-x['H']-x['R'])&0xffffffff;r=Q-x['S']-x['ab'];t=(r>>x['shift']) if r>0 else -1
 expect=(not x['swbad'] and max(x['ing']+x['ab']-4096,0)<t and (x['ab']<=x['hcap'] or x['ab']+max(x['ing']+x['ab']-4096,0)<=t))
 results.append(dict(input=x,actual_safe=bool(safe),expected_safe=expect,room=room,threshold=th,equal=(bool(safe)==expect and room==r and th==t)))
# Ingress-admission distinction: exact byte state. A supported mixed-rate topology realizes it.
B=1<<20; H=255+3*343125;R=4*4096;size=1064;Q=B-H-R;t0=Q>>3;tmin=(Q-size)>>3
counterexample=dict(buffer_size=B,headroom_by_port=[255,343125,343125,343125],total_reserve=R,Q=Q,packet_size=size,initial_ing=0,initial_shared=0,threshold_before=t0,threshold_lower_bound=tmin,original_predicate_safe=(0<tmin),serial_admission_drop=(size>255 and size>t0),scope='arithmetic counterexample; validate actual topology separately')
v=dict(source_sha256=hashlib.sha256(s.encode()).hexdigest(),extracted_sha256=hashlib.sha256(cpp.encode()).hexdigest(),count=len(results),all_equal=all(x['equal'] for x in results),ubsan_stderr=p.stderr,results=results,admission_counterexample=counterexample)
(OUT/'fixtures.json').write_text(json.dumps(v,indent=2)+'\n');print(json.dumps({k:v[k] for k in ('count','all_equal','admission_counterexample')}))
