"""Normal-entry arrival layout with whole-invocation time and per-process GPU memory."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time

p = argparse.ArgumentParser(description=__doc__)
p.add_argument("backend", choices=["hip","cuda"])
for name in ("binary","inputs","out"):
    p.add_argument(name,type=lambda s:Path(s).resolve())
a=p.parse_args()
a.out.mkdir()
cases = {"default":[], "forced-auto":["--arrival-layout=split"],
         "forced-fixed":["--arrival-tail-cap=1"], "preset":["--arrival-tail-cap=8192"]}
if a.backend == "cuda":
    cases["wide"] = ["--arrival-layout=wide","--start-history=2048"]
summary = {}


def children(pid):
    path = Path(f"/proc/{pid}/task/{pid}/children")
    try:
        return [pid, *(child for name in path.read_text().split() for child in children(int(name)))]
    except FileNotFoundError:
        return [pid]


def memory(pids):
    if a.backend == "cuda":
        out = subprocess.check_output(["nvidia-smi","--query-compute-apps=pid,used_gpu_memory",
                                       "--format=csv,noheader,nounits"],text=True,timeout=4)
        return {int(row[0]):int(row[1])*1024 for row in csv.reader(out.splitlines())
                if len(row)==2 and row[0].strip().isdigit() and int(row[0]) in pids and row[1].strip().isdigit()}
    total = {}
    seen = set()
    for pid in pids:
        for fd in Path(f"/proc/{pid}/fdinfo").glob("*"):
            try:
                data = fd.read_text()
            except (OSError,UnicodeError):
                continue
            client = re.search(r"^drm-client-id:\s+(\d+)",data,re.M)
            resident = re.search(r"^drm-resident-vram:\s+(\d+) KiB",data,re.M)
            allocated = re.search(r"^drm-memory-vram:\s+(\d+) KiB",data,re.M)
            match = resident or allocated
            if client and match and client[1] not in seen:
                seen.add(client[1])
                total[pid] = total.get(pid,0) + int(match[1])
    return total


for label, extra in cases.items():
    d = a.out/label
    d.mkdir()
    args = [str(a.binary),"--tcp=dctcp","--queue=200","--spray=0","--tcp-sack=1","--tcp-timestamps=1",
            "--topo=graph",f"--topofile={a.inputs/'1024.graph'}","--graph_k=16",f"--flows={a.inputs/'1024.csv'}",
            "--out=flows.csv",*extra]
    start = time.monotonic_ns()
    samples = []
    with (d/"run.log").open("wb") as log:
        child = subprocess.Popen(args,cwd=d,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        while child.poll() is None:
            now = time.monotonic_ns()
            pids = children(child.pid)
            samples.append((now-start,memory(pids)))
            if now-start > 100_000_000_000:
                import os
                os.killpg(child.pid,9)
                break
            time.sleep(.03)
        rc=child.wait()
    receipt = dict(command=args, rc=rc, wall_ns=time.monotonic_ns()-start, samples=samples,
                   peak_kib=max((sum(row.values()) for _,row in samples),default=0))
    fct=d/"flows.csv"
    if fct.exists():
        with fct.open("rb") as data:
            receipt["fct_sha256"]=hashlib.file_digest(data,"sha256").hexdigest()
    summary[label]=receipt
    (d/"receipt.json").write_text(json.dumps(receipt,indent=2)+"\n")
    print(label,rc,receipt["peak_kib"],flush=True)
(a.out/"summary.json").write_text(json.dumps({k:{x:y for x,y in r.items() if x!="samples"} for k,r in summary.items()},indent=2)+"\n")
# Share the initial kernel launch between an options world and a double-off world.
for label in ("off","mixed"):
    d=a.out/label
    d.mkdir()
    common=[str(a.binary),"--tcp=dctcp","--queue=200","--topo=graph",f"--topofile={a.inputs/'128.graph'}","--graph_k=16"]
    if label=="off":
        args=[*common,f"--flows={a.inputs/'128.csv'}","--out=off.csv"]
    else:
        inputs=f"flows={a.inputs/'128.csv'},"
        (d/"worlds").write_text(inputs+"tcp-sack=0,tcp-timestamps=0,out=off.csv\n"+
                                 inputs+"tcp-sack=1,tcp-timestamps=1,out=both.csv\n")
        args=[*common,"--worlds=worlds"]
    with (d/"run.log").open("wb") as log:
        run=subprocess.run(args,cwd=d,stdout=log,stderr=subprocess.STDOUT,timeout=100)
    (d/"rc").write_text(f"{run.returncode}\n")
