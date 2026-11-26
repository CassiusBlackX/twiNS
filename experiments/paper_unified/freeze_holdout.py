"""Freeze 12 packet inputs and two closed-loop controls before looking at results."""
import hashlib
import json
from pathlib import Path
import random
import sys

dest, fixtures = (Path(v).resolve() for v in sys.argv[1:])
dest.mkdir()
topology = (fixtures/"128.graph").read_text()
cases = []
for i, (cc, sack, ts, queue, spray) in enumerate((
    ("dctcp",0,0,200,0), ("dctcp",1,1,80,0), ("cubic",0,1,200,0), ("cubic",1,1,24,0),
    ("newreno",1,0,40,1), ("newreno",1,1,12,1), ("dctcp",1,1,16,1), ("cubic",1,1,33,0),
)):
    seed = 271+i
    rng = random.Random(seed)
    case = dest / f"tcp-{i:02d}"
    case.mkdir()
    changed = []
    for line in topology.splitlines():
        x=line.split()
        if len(x)==5 and x[0]=="link":
            x[4]=str(int(x[4]) + (137 + i*31 if len(changed)%3 == 0 else 0))
            line=" ".join(x)
        changed.append(line)
    (case/"topology").write_text("\n".join(changed)+"\n")
    rows=[]
    for j in range(12):
        src=3*j + rng.randint(0,2)
        dst=108+j%3 if i in (3,5,6) else 64 + rng.randrange(47)
        packet_sizes=(1389,1460,2961,27411,81307,133440)
        rows.append((src,dst,rng.choice(packet_sizes),rng.randrange(0,48)*53))
    (case/"flows.csv").write_text("src,dst,flow_size,start_ns\n"+"".join(",".join(map(str,r))+"\n" for r in rows))
    cases.append(dict(case=case.name,engine="tcp",protocol=cc,sack=sack,timestamps=ts,queue=queue,
                      spray=spray,seed=seed,cutoff_ns=100_000_000,reserved=(i in (6,7))))
for i,(rto,first,second,offset,cutoff) in enumerate((
    (817,1971,4501,41,2000100000),(905,2701,4580,19,2000100000),
    (100000,3801,5090,37,2000100000),(833,981,4159,42,2000004180),
)):
    case=dest/f"irn-{i:02d}"
    case.mkdir()
    (case/"flows.csv").write_text(f"src,dst,flow_size,start_ns\n0,2,{first},2000000000\n1,2,{second},{2000000000+offset}\n")
    (case/"ref.flows").write_text(f"2\n0 2 3 {first} 2.0\n1 2 3 {second} 2.{offset:09d}\n")
    cases.append(dict(case=case.name,engine="irn",rto_ns=rto,seed=1,cutoff_ns=cutoff,reserved=False))
cases.extend([dict(case="ai-repeat",engine="closed",fixture="boundary-review/simai-native/control",
                   note="frozen repetition control from the independent two-collective replay"),
              dict(case="ai-stall",engine="closed",fixture="boundary-review/hip-closed/reference-stall",
                   note="frozen independent SimAI stall control")])
(dest/"scenarios.json").write_text(json.dumps(cases,indent=2)+"\n")
hashes={str(f.relative_to(dest)):hashlib.sha256(f.read_bytes()).hexdigest()
        for f in sorted(dest.rglob("*")) if f.is_file()}
(dest/"sealed.sha256").write_text("".join(f"{h}  {n}\n" for n,h in hashes.items()))
