"""Hold other scenario controls fixed while toggling SACK, timestamps, and driver FMA."""
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from adapt_unison import build

VIDEOS=Path.home()/'Videos'
ISO=VIDEOS/'twins-isolated-20261118/out'
DATA=VIDEOS/'twins-paper-followthrough-20261122/project/data-input'

def execute(root,binary,args,settings):
    root.mkdir(parents=True)
    dest=root/'output.csv'
    cmd=['taskset','-c','3',str(binary),'--threads=0',*args,f'--out={dest}']
    start=time.monotonic_ns()
    with (root/'run.log').open('wb') as log:
        rc=subprocess.run(cmd,cwd=root,env={**os.environ,**settings},stdout=log,stderr=subprocess.STDOUT,
                          timeout=900).returncode
        elapsed=time.monotonic_ns()-start
    rows=[tuple(map(int,x[:6])) for x in list(csv.reader(dest.open()))[1:]] if dest.exists() else []
    sort=sorted(x[5]-x[3] for x in rows if x[5]>=0)
    text=(root/'run.log').read_text(errors='replace')
    result=dict(command=cmd,settings=settings,rc=rc,wall_ns=elapsed,rows=len(rows),completed=len(sort),
                p99_ns=sort[int(.99*(len(sort)-1)+.5)] if sort else None,
                sack_blocks=sum(x.startswith('SACK block ') for x in text.splitlines()),
                summary=re.findall(r'^flows .*$',text,re.M),
                binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest())
    (root/'receipt.json').write_text(json.dumps(result,indent=2)+'\n')
    return result,rows

def main():
    out=Path(sys.argv[1]).resolve()
    out.mkdir(parents=True,exist_ok=False)
    reference=build(out/'gcc-no-contract',contract='off',inspect_tcp_options=True)
    fast=build(out/'gcc-fast-contract',contract='fast',inspect_tcp_options=True)
    manifest=json.loads((ISO/'manifest.json').read_text())
    jobs={job['case']:job for job in manifest['cubic']}
    data=[]
    for label,name,queue,ecmp,trace in [
        ('no-loss-control','k8_incast16',200,1,0),
        ('small-queue-burst','k8_a2a30',8,1,0),
        ('packet-ecmp','k8_a2a30',200,0,0),
        ('high-load','k8_a2a50s',100,1,0)]:
        j=jobs[name]
        args=[f'--k={j["k"]}',f'--flows={ISO}/inputs/{j["flows"]}',
              '--tcp=ns3::TcpNewReno','--stop=0.3','--hostq=red',f'--flowEcmp={ecmp}',f'--queue={queue}']
        measurements={}
        for feature,env in [('default',{}),('sack',{'TWINS_REF_SACK':'1'}),
                            ('timestamps',{'TWINS_REF_TIMESTAMPS':'1'})]:
            row,rows=execute(out/'transport'/label/feature,reference,args,{**env,'TRACE_FLOW':str(trace)})
            measurements[feature]=(row,rows)
        base=measurements['default'][1]
        data.append(dict(case=label,arms={k:dict(result=r,six_fields_equal=base==rows,
                         changed_rows=sum(a!=b for a,b in zip(base,rows)))
                         for k,(r,rows) in measurements.items()}))
        print('transport',label,{k:(r['completed'],r['sack_blocks']) for k,(r,_) in measurements.items()},flush=True)
    numeric=[]
    for protocol in ('TcpDctcp','TcpCubic'):
        inputs=DATA/'tcp/128'
        args=['--topo=file',f'--topofile={inputs}/heterogeneous.graph',f'--flows={inputs}/tcp/flows.csv',
              f'--tcp=ns3::{protocol}','--stop=0.1','--hostq=red','--flowEcmp=1','--queue=200']
        a,x=execute(out/'numerical'/protocol/'off',reference,args,{})
        b,y=execute(out/'numerical'/protocol/'fast',fast,args,{})
        numeric.append(dict(tcp=protocol,scope='Only the scenario driver translation unit; original runtime libraries are identical',
                            arms=[a,b],same=x==y,first=next(((i,p,q) for i,(p,q) in enumerate(zip(x,y)) if p!=q),None)))
    (out/'summary.json').write_text(json.dumps(dict(transport=data,scenario_driver_fma=numeric),indent=2)+'\n')

if __name__=='__main__':
    main()
