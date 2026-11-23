"""Compare the existing Unison sequential queue prefix with the original oracle."""
from collections import defaultdict
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

VIDEOS=Path.home()/'Videos'
PRIOR=VIDEOS/'twins-paper-strict-20261123/unison/20261122T204949Z/results/measurements/preflight/101-128'
DATA=VIDEOS/'twins-paper-followthrough-20261122/project/data-input/tcp/128'
ORACLE=VIDEOS/'twins-extensions-20261119/tcp-oracle/reference'

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def events(path):
    by_port=defaultdict(list)
    malformed=0
    for line in path.open():
        if line.startswith('Q '):
            try:
                ts,node,dev,src,dst,kind,seq,qlen,*rest=line.strip().split()[1:]
                row=[int(ts),int(src),int(dst),kind,int(seq),int(qlen),*rest]
                if kind not in ('D','A','S','SA') or not node.isdecimal() or not dev.isdecimal() or rest:
                    raise ValueError
                by_port[node,dev].append(row)
            except ValueError:
                malformed+=1
    return by_port,malformed

def compare(a,b):
    differences=[]
    for key in sorted(a.keys()|b.keys()):
        x,y=a[key],b[key]
        if x!=y:
            first=next((i for i,(u,v) in enumerate(zip(x,y)) if u!=v),min(len(x),len(y)))
            differences.append(dict(node=int(key[0]),device=int(key[1]),index=first,
                                    adapted=x[first] if first<len(x) else None,
                                    reference=y[first] if first<len(y) else None))
    return dict(equal=not differences,ports=len(a.keys()|b.keys()),
                adapted_records=sum(map(len,a.values())),reference_records=sum(map(len,b.values())),
                first_differences=differences[:10],differing_ports=len(differences))

def main():
    out=Path(sys.argv[1])
    out.mkdir(parents=True,exist_ok=False)
    graph=DATA/'heterogeneous.graph'
    traffic=DATA/'tcp/flows.csv'
    actual=out/'flows.fct'
    command=['taskset','-c','3',str(ORACLE),'--topo=file','--hostq=red','--tcp=ns3::TcpDctcp','--flowEcmp=1',
        '--stop=0.1','--ecnK=30',f'--topofile={graph}',f'--flows={traffic}',f'--out={actual}']
    env={**os.environ,'TRACE_ALLQ':'10000000'}
    log=out/'reference.log'
    with log.open('wb') as stream:
        subprocess.run(command,env=env,stdout=stream,stderr=subprocess.STDOUT,cwd=out,check=True,timeout=180)
    baseline,malformed=events(log)
    if not baseline or malformed:
        raise ValueError('oracle queue prefix is empty or malformed')
    arms={}
    for width in (0,1,2):
        source=PRIOR/f't{width}/trial-0/process.log'
        queues,malformed=events(source)
        arms[str(width)]=dict(log_sha256=digest(source),malformed_records=malformed,**compare(queues,baseline))
        if malformed:
            arms[str(width)]['equal']=False
    result=dict(command=command,oracle_binary_sha256=digest(ORACLE.parent/'bin'),
                queue_prefix_ps=10000000,oracle_log_sha256=digest(log),
                input_sha256={x.name:digest(x) for x in (graph,traffic)},arms=arms)
    (out/'summary.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(arms),flush=True)
    return 0 if arms['0']['equal'] else 1

if __name__=='__main__':
    raise SystemExit(main())
