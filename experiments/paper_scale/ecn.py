"""Run the frozen arrival/order factorial and compare original-order FCT records."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import csv
import hashlib
from itertools import product
import json
import os
from pathlib import Path
import subprocess
import time

def digest(p):
    with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()

def fields(p):
    with p.open() as f:return [tuple(map(int,x[:6])) for x in list(csv.reader(f))[1:]]

def main():
    p=argparse.ArgumentParser()
    for key in ('inputs','out','reference','gpu_outputs'):p.add_argument(key,type=Path)
    p.add_argument('--workers',type=int,default=1);a=p.parse_args()
    a.out.mkdir(parents=True,exist_ok=True)
    binary=digest(a.reference)
    def job(key):
        arrival,order,k=key
        flow=a.inputs/'ecn'/f'{arrival}.csv';d=a.out/f'a{arrival}-o{order}-k{k}';d.mkdir(exist_ok=True)
        ref=a.gpu_outputs/f'ws30_s{arrival}_TcpDctcp_q100_K{k}.csv'
        cmd=[str(a.reference.resolve()),'--k=8',f'--flows={flow.resolve()}','--hostq=red','--flowEcmp=1','--tcp=ns3::TcpDctcp','--queue=100',f'--ecnK={k}','--stop=0.1',f'--out={d.resolve()}/flows.csv']
        stamp=dict(ref_sha=binary,input_sha=digest(flow),arrival=arrival,order=order,k=k,command=cmd)
        rec=d/'receipt.json'
        if rec.exists():
            prev=json.loads(rec.read_text())
            if all(prev[x]==v for x,v in stamp.items()) and prev['output_sha']==digest(d/'flows.csv') and (order or prev['gpu_sha']==digest(ref)):return prev
            raise RuntimeError(f'changed frozen case {d}')
        start=time.monotonic_ns()
        with (d/'process.log').open('w') as log:
            subprocess.run(cmd,env={**os.environ,'NS3_TIE_SEED':str(order)},stdout=log,stderr=subprocess.STDOUT,timeout=1800,check=True)
        wall=time.monotonic_ns()-start
        rows=fields(d/'flows.csv')
        with flow.open() as f:count=sum(1 for _ in csv.reader(f))-1
        if len(rows)!=count or any(x[5]<0 for x in rows):raise ValueError(f'incomplete {d}')
        if order==0 and rows!=fields(ref):raise ValueError(f'original order differs {d}')
        short=sorted(x[5]-x[3] for x in rows if x[2]<100000)
        v=dict(**stamp,wall_ns=wall,flows=count,output_sha=digest(d/'flows.csv'),gpu_sha=None if order else digest(ref),p99_ns=short[int(.99*(len(short)-1)+.5)])
        rec.write_text(json.dumps(v,indent=2));return v
    with ThreadPoolExecutor(a.workers) as pool:
        rows=list(pool.map(job,product(range(1,21),range(6),(10,20,30,40))))
    vals={(x['arrival'],x['order'],x['k']):x['p99_ns'] for x in rows}
    out=[]
    for order in range(6):
        paired=[]
        for i in range(1,21):
            def rank(o):
                scores=[vals[i,o,k] for k in (10,20,30)]
                return tuple(tuple(k for k in (10,20,30) if vals[i,o,k]==s) for s in sorted(set(scores)))
            p30,p40=vals[i,order,30],vals[i,order,40]
            paired.append(dict(arrival=i,delta_ns=p40-p30,crossed=p30<1000000<=p40,rank=rank(order),rank_status='tied' if len(rank(order))<3 else 'preserved' if rank(order)==rank(0) else 'changed'))
        out.append(dict(order=order,crossings=sum(x['crossed'] for x in paired),pairs=paired))
    (a.out/'result.json').write_text(json.dumps(out,indent=2))
if __name__=='__main__':main()
