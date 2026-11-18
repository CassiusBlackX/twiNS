"""Fixed trajectory 1, K=30, insertion order versus existing perturbation 1."""
from pathlib import Path
import gzip,hashlib,json,os,subprocess,sys
from collections import Counter
r=Path(sys.argv[1]).resolve()
for seed in (0,1):
    d=r/'trace'/str(seed);d.mkdir(parents=True,exist_ok=True)
    if (d/'receipt.json').exists():continue
    cmd=[str(r/'src/build/scratch/ns3.46.1-twins-ref-optimized'),'--k=8',f'--flows={r}/inputs/ecn/1.csv','--hostq=red','--flowEcmp=1','--queue=100','--ecnK=30','--tcp=ns3::TcpDctcp','--stop=0.1','--out=flows.csv']
    with subprocess.Popen(cmd,cwd=d,env={**os.environ,'TRACE_ALLQ':'100000000000','TRACE_ECN':'1','NS3_TIE_SEED':str(seed)},stdout=subprocess.PIPE,stderr=subprocess.STDOUT) as p,gzip.open(d/'events.gz','wb',compresslevel=1) as f:
        for line in p.stdout:f.write(line)
        assert p.wait()==0
    (d/'receipt.json').write_text(json.dumps(dict(order=seed,k=30,trajectory=1,command=cmd)))
