"""Use the frozen ns-3 scheduler and packet observers to replay the four fixed ECN witnesses."""
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

VIDEOS = Path.home() / 'Videos'
REF = VIDEOS/'netsim/unison-evlog/build/scratch/ns3.46.1-twins-ref-optimized'
DATA = VIDEOS/'twins-paper-scale-20261119/inputs/ecn'
TARGETS = {14:(79,95,54020,1667485),16:(0,47,51100,1671536),
           19:(60,49,29200,1423857),20:(119,64,5840,191218)}
EXPECTED = {14:(322600,1290726),16:(79962,1285209),19:(40363,10036102),20:(38585,10035149)}

def sha(path):
    with path.open('rb') as f:
        return hashlib.file_digest(f,'sha256').hexdigest()

def replay(work, seed, threshold, order, witness=None):
    work.mkdir(parents=True)
    source = DATA/f'{seed}.csv'
    output = work/'flows.csv'
    command = ['taskset','-c','3',str(REF),'--k=8',f'--flows={source}','--hostq=red',
               '--flowEcmp=1','--tcp=ns3::TcpDctcp','--queue=100',f'--ecnK={threshold}',
               '--stop=0.1',f'--out={output}']
    env = {'NS3_TIE_SEED':str(order)}
    if witness:
        env.update(TRACE_QUEUE=f'{witness[0][0]},{witness[0][1]}',TRACE_ALL='1',
                   TRACE_FLOW=str(witness[1]))
    start = time.monotonic_ns()
    with (work/'process.log').open('wb') as log:
        completed = subprocess.run(command,cwd=work,env={**os.environ,**env},
                                   stdout=log,stderr=subprocess.STDOUT,timeout=180)
    elapsed = time.monotonic_ns()-start
    rows = [tuple(map(int,row)) for row in list(csv.reader(output.open()))[1:]]
    if completed.returncode or not rows or any(row[5]<0 for row in rows):
        raise ValueError(f'incomplete ns-3 reference {seed}/{threshold}/{order}')
    p99 = sorted((row[5]-row[3],idx,row) for idx,row in enumerate(rows) if row[2]<100000)
    value, index, tail = p99[int(.99*(len(p99)-1)+.5)]
    target_index,target = next((i,row) for i,row in enumerate(rows) if row[:4] == TARGETS[seed])
    handshake = []
    packets = 0
    if witness:
        for line in (work/'process.log').open():
            if line.startswith('enq '):
                packets += 1
                flags = int(re.search(r'flags (\d+)',line)[1])
                if flags & 2:
                    handshake.append(line.strip())
    result=dict(seed=seed,threshold=threshold,tie_order=order,command=command,env=env,
                traced_identity=witness[0] if witness else None,wall_ns=elapsed,
                source_sha256=sha(source),binary_sha256=sha(REF),output_sha256=sha(output),
                target_identity=target[:4],target_fct_ns=target[5]-target[3],
                target_established_ns=target[4],target_completed_ns=target[5],target_retx=target[8],
                p99_ns=value,p99_identity=tail[:4],trace_count=packets,handshake=handshake)
    (work/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    return result,(tail[:4],index),(target[:4],target_index)

def main():
    output=Path(sys.argv[1]).resolve()
    output.mkdir(parents=True,exist_ok=False)
    data=[]
    for seed in TARGETS:
        for order in (0,1):
            for k in (30,40):
                folder=output/f'a{seed}-o{order}-k{k}'
                plain,tail,target=replay(folder/'timed',seed,k,order)
                # Order zero follows the GPU-selected witness across both thresholds.
                # The other ordering records its own p99 and the original witness.
                witness=target if not order else tail
                observed,_,_=replay(folder/'observed',seed,k,order,witness)
                if plain['output_sha256']!=observed['output_sha256']:
                    raise ValueError('packet observation changed reference results')
                plain['matches_frozen_witness']=plain['target_fct_ns']==EXPECTED[seed][k==40] if order==0 else None
                plain['observer']=observed
                data.append(plain)
                print(seed,order,k,plain['target_fct_ns'],plain['p99_ns'],flush=True)
    (output/'summary.json').write_text(json.dumps(data,indent=2)+'\n')

if __name__=='__main__':
    main()
