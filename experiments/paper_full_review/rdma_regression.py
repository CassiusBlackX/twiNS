"""Rerun the clean RDMA subset of the published network inventory."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

p=argparse.ArgumentParser(description=__doc__)
for name in ("binary","config","data","inventory","out"):
    p.add_argument(name,type=lambda value:Path(value).resolve())
a=p.parse_args()
a.out.mkdir(parents=True,exist_ok=False)
outcomes=[]
rows=json.loads(a.inventory.read_text())
for index,row in enumerate(rows):
    name=row["case"]
    target=a.out/f"{index:03d}-{name}"
    target.mkdir()
    fct=target/"output.fct"
    cmd=[str(a.binary),str(a.config),*row["command"][2:-1],f"--out={fct}"]
    with (target/"run.log").open("wb") as log:
        try:
            rc=subprocess.run(cmd,cwd=a.data,stdout=log,stderr=subprocess.STDOUT,timeout=120).returncode
        except subprocess.TimeoutExpired:
            rc=124
    result=(target/"run.log").read_text()
    flags=re.findall(r"undecided ties (\d+), on lost tails (\d+) \| overflows: tx (\d+) queue (\d+) ackq (\d+) qps (\d+) due (\d+)",result)
    found=None
    if fct.exists():
        entries=sorted(" ".join(str(int(n)) for n in line.split()) for line in fct.open())
        found=hashlib.sha256("".join(line+"\n" for line in entries).encode()).hexdigest()
    passed=rc==0 and found==row["expected"] and bool(flags) and not any(map(int,flags[-1]))
    record=dict(name=name,rc=rc,passed=passed,expected=row["expected"],observed=found,flags=flags,command=cmd)
    (target/"receipt.json").write_text(json.dumps(record,indent=2)+"\n")
    outcomes.append(record)
    print(index,name,passed,flush=True)
(a.out/"summary.json").write_text(json.dumps(dict(total=len(rows),tested=len(outcomes),accepted=sum(r["passed"] for r in outcomes)),indent=2)+"\n")
raise SystemExit(0 if all(r["passed"] for r in outcomes) else 1)
