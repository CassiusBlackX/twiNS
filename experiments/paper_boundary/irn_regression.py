"""Compare the original 32 IRN configurations to their accepted frozen records."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time
p=argparse.ArgumentParser(description=__doc__)
for name in ("binary","inputs","manifest","output"):
    p.add_argument(name,type=lambda v:Path(v).resolve())
a=p.parse_args()
a.output.mkdir(parents=True,exist_ok=False)
rows={}
for job in json.loads(a.manifest.read_text()):
    d=a.output/job["case"]
    d.mkdir(parents=True)
    cmd=[a.binary,a.inputs/job["config"],f"--topo={a.inputs/job['topo']}",f"--flows={a.inputs/job['flows']}",
         "IRN_BDP=156000","--fp-bases=1000003,1000033","--out=flows.fct"]
    start=time.monotonic_ns()
    with (d/"run.log").open("wb") as log:
        try:
            rc=subprocess.run(list(map(str,cmd)),cwd=d,stdout=log,stderr=log,timeout=120).returncode
        except subprocess.TimeoutExpired:
            rc=124
    f=d/"flows.fct"
    vals=sorted(tuple(map(int,line.split())) for line in f.open()) if f.exists() else []
    sig=hashlib.sha256("".join(" ".join(map(str,r))+"\n" for r in vals).encode()).hexdigest()
    flags=re.findall(r"undecided ties (\d+), on lost tails (\d+) \| overflows: tx (\d+) queue (\d+) ackq (\d+) qps (\d+) due (\d+)",(d/"run.log").read_text())
    passed=rc==0 and sig==job["gold"] and bool(flags) and not any(map(int,flags[-1]))
    row=dict(case=job["case"],rc=rc,wall_ns=time.monotonic_ns()-start,accepted=passed,flows=len(vals),sha256=sig,flags=flags,command=list(map(str,cmd)))
    (d/"receipt.json").write_text(json.dumps(row,indent=2)+"\n")
    rows[job["case"]]=row
    print(job["case"],passed,flush=True)
(a.output/"summary.json").write_text(json.dumps(dict(tested=len(rows),accepted=sum(r["accepted"] for r in rows.values())),indent=2)+"\n")
