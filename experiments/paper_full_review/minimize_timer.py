"""Reduce the frozen A2A input while preserving a native CNP/timer same-time event."""
import argparse
from collections import defaultdict
import json
from pathlib import Path
import subprocess

p=argparse.ArgumentParser(description=__doc__)
for name in ("binary","inputs","output"):
    p.add_argument(name,type=lambda value:Path(value).resolve())
a=p.parse_args()
a.output.mkdir(parents=True,exist_ok=False)
header,*rows=(a.inputs/"k8_a2a30.csv").read_text().splitlines()
attempts=[]
def check(test):
    i=len(attempts)
    offered=a.output/f"{i:02d}.csv"
    offered.write_text("\n".join([header,*test])+"\n")
    with (a.output/f"{i:02d}.log").open("wb") as log:
        rc=subprocess.run([a.binary,a.inputs/"hpcc.cfg",f"--topo={a.inputs/'fattree8.txt'}",f"--flows={offered}",
                           "--delay=10000ns",f"--out={a.output/f'{i:02d}.fct'}","CC_MODE=1"],
                          stdout=log,stderr=subprocess.STDOUT,timeout=60).returncode
    groups=defaultdict(list)
    with (a.output/f"{i:02d}.log").open() as stream:
        for line in stream:
            if line.startswith("DCQCN "):
                _,tick,src,dst,port,tag,*data=line.split()
                groups[(tick,src,dst,port)].append(tag)
    collision=[(k,v) for k,v in groups.items() if len(v)>1 and "cnp" in v and any(t!="cnp" for t in v)]
    row=dict(index=i,flows=len(test),rc=rc,collisions=collision if rc==0 else [])
    attempts.append(row)
    (a.output/"attempts.json").write_text(json.dumps(attempts,indent=2)+"\n")
    return bool(row["collisions"])

assert check(rows)
best=0
for granularity in (2,4,8,16):
    chunk=max(1,(len(rows)+granularity-1)//granularity)
    start=0
    while start<len(rows) and len(attempts)<40:
        reduced=rows[:start]+rows[start+chunk:]
        if reduced and check(reduced):
            rows=reduced
            best=len(attempts)-1
        else:
            start+=chunk
(a.output/"minimal.csv").write_text("\n".join([header,*rows])+"\n")
(a.output/"result.json").write_text(json.dumps(attempts[best],indent=2)+"\n")
