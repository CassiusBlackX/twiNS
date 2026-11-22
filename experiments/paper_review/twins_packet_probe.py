"""Count stock TwiNS queue observations outside the formal timing trials."""
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

def main():
    output=Path(sys.argv[1]).resolve()
    output.mkdir(parents=True,exist_ok=False)
    baseline=Path.home()/'Videos/twins-paper-pair-20261122/project'
    binary=Path.home()/'Videos/twins-paper-followthrough-20261122/project/build/twins'
    reports={}
    for k in (8,16):
        hosts=k**3//4
        case=output/f'k{k}'
        case.mkdir()
        trace=case/'enqueue.csv'
        command=[str(binary),f'--k={k}','--tcp=newreno','--queue=200','--ecnK=200',
                 '--max_slots=10000',f'--flows={baseline}/common-inputs/k{k}/twins.csv',
                 '--out=fct.csv',f'--qtrace={trace}','--qtrace_until=10000000000']
        with (case/'run.log').open('wb') as log:
            rc=subprocess.run(command,cwd=case,stdout=log,stderr=subprocess.STDOUT,timeout=180).returncode
        counts=Counter()
        segments=Counter()
        lines=0
        for line in trace.open():
            ns,node,dev,src,dst,kind,seq,qlen=line.split()
            counts[('source' if int(node)<hosts else 'switch')+'/'+kind]+=1
            if int(node)<hosts and kind=='D':
                segments[(src,dst,seq)]+=1
            lines+=1
        log=(case/'run.log').read_text()
        exported=int(re.search(r'qtrace: (\d+) records',log)[1])
        reference=baseline/'runs/20261122T094810Z/measurements'/f'k{k}/trial-0/twins/fct.csv'
        matched=(case/'fct.csv').read_bytes()==reference.read_bytes()
        report=dict(rc=rc,counts=dict(counts),trace_rows=lines,exported_rows=exported,
                    trace_sha256=hashlib.sha256(trace.read_bytes()).hexdigest(),
                    repeated_data_segments=sum(n-1 for n in segments.values()),
                    unchanged_fct=matched,command=command,
                    observation_scope='Accepted queue enqueues. Source-owned queues count generated packets; switch queues count each forwarded hop.')
        reports[f'k{k}']=report
        if rc or not matched or lines!=exported or exported>=20_000_000 or len(segments)!=32*hosts:
            raise RuntimeError('stock observer must fully reproduce the frozen run')
        trace.unlink()
    (output/'summary.json').write_text(json.dumps(reports,indent=2)+'\n')

if __name__=='__main__':
    main()
