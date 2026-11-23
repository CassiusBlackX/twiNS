"""Capture marks/drops for the already frozen ECN tail witnesses."""
import csv
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from adapt_unison import build
from ecn_tail import DATA, EXPECTED, TARGETS, VIDEOS, sha

HISTORICAL=VIDEOS/'twins-paper-strict-20261123/ecn_tail/20261122T205212Z/results'

def capture(out,binary,seed,k,order):
    out.mkdir(parents=True)
    source=DATA/f'{seed}.csv'
    prior=HISTORICAL/f'a{seed}-o{order}-k{k}'/'timed/flows.csv'
    dst=out/'flows.csv'
    frozen=[tuple(map(int,row)) for row in list(csv.reader(prior.open()))[1:]]
    wanted=TARGETS[seed]
    index,tail=next((i,f) for i,f in enumerate(frozen) if f[:4]==wanted)
    command=['taskset','-c','3',str(binary),'--k=8',f'--flows={source}','--hostq=red',
             '--flowEcmp=1','--tcp=ns3::TcpDctcp','--queue=100',f'--ecnK={k}',
             '--stop=0.1',f'--out={dst}']
    options={'NS3_TIE_SEED':str(order),'TWINS_TARGET_QUEUE':f'{wanted[0]},{wanted[1]}',
             'TRACE_QUEUE':f'{wanted[0]},{wanted[1]}','TRACE_FLOW':str(index),'TRACE_ALL':'1'}
    start=time.monotonic_ns()
    log=out/'process.log'
    with log.open('wb') as stream:
        process=subprocess.run(command,cwd=out,env={**os.environ,**options},
                               stdout=stream,stderr=subprocess.STDOUT,timeout=240)
    runtime=time.monotonic_ns()-start
    if process.returncode:
        raise RuntimeError(f'ns-3 terminated with {process.returncode}')
    equal=sha(dst)==sha(prior)
    tx=[]
    marks=[]
    for line in log.open():
        if line.startswith('target_socket_tx '):
            tx.append(tuple(map(int,line.split()[1:])))
        elif line.startswith('target_queue '):
            cols=line.strip().split(maxsplit=13)
            marks.append([cols[1],*list(map(int,cols[2:13])),cols[13]])
    if not tx:
        raise RuntimeError('missing connection observer')
    # Initial SYN on the traced socket supplies its TCP port pair. Binding by
    # ports disambiguates other workload flows between the same two hosts.
    first_syn=next(x for x in tx if x[3]&2)
    a,b=first_syn[1:3]
    snapshots=[]
    for event in marks:
        src,dst,sp,dp=event[4:8]
        if (src,dst,sp,dp) in ((wanted[0],wanted[1],a,b),(wanted[1],wanted[0],b,a)):
            snapshots.append(event)
    result=dict(flow=wanted,input_sha256=sha(source),binary_sha256=sha(binary),seed=seed,threshold=k,
                tie_order=order,first_syn=first_syn,packet_events=snapshots,command=command,seconds=runtime/1e9,
                fct_equal=equal,completion_ns=tail[5])
    (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n')
    return result

def main():
    out=Path(sys.argv[1]).resolve()
    out.mkdir()
    binary=build(out/'build',tree=VIDEOS/'netsim/unison-evlog',trace_queue_reasons=True)
    result=[]
    for seed in TARGETS:
        for order in (0,1):
            for k in (30,40):
                row=capture(out/f'a{seed}-o{order}-k{k}',binary,seed,k,order)
                result.append(row)
                print(seed,order,k,row['fct_equal'],len(row['packet_events']),flush=True)
                (out/'summary.json').write_text(json.dumps(result,indent=2)+'\n')
    return 0 if all(x['fct_equal'] for x in result) else 1

if __name__=='__main__':
    raise SystemExit(main())
