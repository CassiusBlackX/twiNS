"""Use the standard TCP entry point with two frozen imported policies."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import re
import subprocess

p=argparse.ArgumentParser(description=__doc__)
for name in ("binary","inputs","out"):
    p.add_argument(name,type=lambda s:Path(s).resolve())
a=p.parse_args()
a.out.mkdir()
passes = {}

def fct(path):
    with path.open() as stream:
        r=csv.reader(stream)
        next(r)
        return [row[:6] for row in r]

def launch(args,d):
    with (d/"run.log").open("wb") as f:
        rc=subprocess.run(args,cwd=d,stdout=f,stderr=subprocess.STDOUT,timeout=100).returncode
    return rc,(d/"run.log").read_text(errors="replace")

for cc in ("scalable","highspeed"):
    for case,flow,queue in (("tail","tail.csv",4),("incast","k8_incast16.csv",200)):
        label=f"{cc}-{case}"
        d=a.out/label
        d.mkdir()
        gold=json.loads((a.inputs/f"{label}.json").read_text())
        # Frozen policy runner used the same backend-independent qtrace formatting.
        args=[str(a.binary),"--k=8",f"--tcp={cc}",f"--flows={a.inputs/flow}",f"--queue={queue}",
              "--ecnK=30","--out=flows.csv","--qtrace=queue.txt",f"--qtrace_until={gold['cutoff_ps']}"]
        rc,log=launch(args,d)
        ours=fct(d/"flows.csv") if (d/"flows.csv").exists() else []
        q=d/"queue.txt"
        payload = b""
        if q.exists():
            payload=b"".join(row for row in q.read_bytes().splitlines(keepends=True)
                             if int(row.split(maxsplit=1)[0])<=gold["cutoff_ps"])
        counts=re.findall(r"\brto (\d+)",log)
        match=fct(a.inputs/f"{label}.fct")==ours
        digest=hashlib.sha256(payload).hexdigest()
        passes[label]=dict(command=args,rc=rc,flows=len(ours),fct_equal=match,packets=payload.count(b"\n"),
                           queue_sha256=digest,queue_equal=digest==gold["ordered_queue_sha256"][0],
                           rto=int(counts[-1]) if counts else None)
for cc in ("newreno","dctcp","cubic"):
    d=a.out/f"{cc}-incast"
    d.mkdir()
    args=[str(a.binary),"--k=8",f"--tcp={cc}",f"--flows={a.inputs/'k8_incast16.csv'}","--out=flows.csv"]
    rc,_=launch(args,d)
    passes[d.name]=dict(rc=rc)
for policies in (["scalable","newreno","dctcp","highspeed","cubic"], ["cubic","highspeed","dctcp","newreno","scalable"]):
    d=a.out/f"batch-{policies[0]}"
    d.mkdir()
    (d/"worlds").write_text("".join(f"flows={a.inputs/'k8_incast16.csv'},tcp={cc},out={cc}.fct\n" for cc in policies))
    rc,_=launch([str(a.binary),"--k=8","--worlds=worlds"],d)
    passes[d.name]=dict(rc=rc,matches={cc:(d/f"{cc}.fct").exists() and fct(d/f"{cc}.fct")==fct(a.out/f"{cc}-incast"/"flows.csv")
                                      for cc in policies})
(a.out/"summary.json").write_text(json.dumps(passes,indent=2)+"\n")
