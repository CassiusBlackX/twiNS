"""Measure one fixed-capacity failure and a clean restart on the frozen TCP input."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

binary, inputs, out = (Path(arg).resolve() for arg in sys.argv[1:])
out.mkdir()
start = time.monotonic_ns()
attempts = []
for cap in (4096, 8192):
    d = out / str(cap)
    d.mkdir()
    env = {**os.environ, "TWINS_TCP_ARRIVAL_TAIL_CAP":str(cap)}
    command = [str(binary), "--single-pass", "--tcp=dctcp", "--queue=200", "--spray=0", "--tcp-sack=1",
               "--tcp-timestamps=1", "--topo=graph", f"--topofile={inputs/'1024.graph'}", "--graph_k=16",
               f"--flows={inputs/'1024.csv'}", "--out=flows.csv"]
    tick = time.monotonic_ns()
    with (d / "run.log").open("wb") as log:
        rc = subprocess.run(command, cwd=d, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=150).returncode
    attempts.append(dict(cap=cap, rc=rc, wall_ns=time.monotonic_ns()-tick, command=command))
    if rc != (3 if cap == 4096 else 0):
        break
(out / "receipt.json").write_text(json.dumps(dict(wall_ns=time.monotonic_ns()-start, attempts=attempts), indent=2)+"\n")
raise SystemExit(0 if [row["rc"] for row in attempts] == [3,0] else 1)
