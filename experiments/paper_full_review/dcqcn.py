"""Replay the twelve frozen DCQCN entries with isolated receipts."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import subprocess
from time import monotonic_ns

p = argparse.ArgumentParser(description=__doc__)
p.add_argument("backend", choices=("native", "gpu"))
for arg in ("binary", "inputs", "output"):
    p.add_argument(arg, type=lambda s: Path(s).resolve())
args = p.parse_args()
args.output.mkdir(parents=True, exist_ok=False)
jobs = json.loads((args.inputs / "dcqcn.json").read_text())
count = 300_000

def run(item):
    identity, frozen = item
    folder = args.output / identity
    folder.mkdir(parents=True)
    cmd = [str(args.binary), str(args.inputs / "hpcc.cfg")]
    for token in frozen["command"][2:-1]:
        if token.startswith(("--topo=", "--flows=")):
            option, path = token.split("=", 1)
            token = f"{option}={args.inputs / Path(path).name}"
        if token.startswith("--delay=") and args.backend == "native" and not token.endswith("ns"):
            token += "ns"
        cmd.append(token)
    cmd += ["--out=output.fct", "--txtrace=wire.raw", f"--until={count}"]
    t = monotonic_ns()
    with (folder / "run.log").open("wb") as log:
        try:
            rc = subprocess.run(cmd, cwd=folder, stdout=log, stderr=subprocess.STDOUT, timeout=240).returncode
        except subprocess.TimeoutExpired:
            rc = 124
    fct = folder / "output.fct"
    row = dict(command=cmd, rc=rc, wall_ns=monotonic_ns()-t, frozen=frozen,
               flows=sum(1 for _ in fct.open()) if fct.exists() else 0,
               sha256=hashlib.sha256(fct.read_bytes()).hexdigest() if fct.exists() else None)
    (folder / "receipt.json").write_text(json.dumps(row, indent=2)+"\n")
    print(identity, rc, row["flows"], flush=True)
    return identity, row

with ThreadPoolExecutor(max_workers=6 if args.backend=="native" else 1) as pool:
    result = dict(pool.map(run, sorted(jobs.items())))
(args.output / "summary.json").write_text(json.dumps(result,indent=2)+"\n")
