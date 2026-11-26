"""Run bounded wall-time native oracles across the 30-second simulated expiry boundary."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

root = Path.cwd()
reference = root / "build/reference-v4/reference"
old = Path("/home/cassius/netsim/twins-paper-tcp-options-20261126/project/experiments/paper_tcp_options/fixtures")
inputs = root / "native/long-inputs"
inputs.mkdir(parents=True, exist_ok=True)
flow = inputs / "forty.csv"
flow.write_text("src,dst,flow_size,start_ns\n" + "".join(f"0,1,1460,{i * 1000000000}\n" for i in range(1,41)))
for arm, sack in (("timestamps", 0), ("both", 1)):
    folder = root / "native/long-connections" / arm
    folder.mkdir(parents=True, exist_ok=False)
    command = [str(reference), "--threads=0", "--topo=file", f"--topofile={old / 'two-paths.graph'}",
               f"--flows={flow}", "--tcp=ns3::TcpNewReno", "--stop=72", "--hostq=red", "--flowEcmp=1",
               "--queue=200", f"--tcp-sack={sack}", "--tcp-timestamps=1", "--out=flows.csv"]
    start = time.monotonic_ns()
    with (folder / "run.log").open("wb") as log:
        rc = subprocess.run(command, cwd=folder, stdout=log, stderr=subprocess.STDOUT,
                            env={**os.environ, "TRACE_ALLQ":"72000000000000", "TRACE_FRAGMENTS":"1"}, timeout=120).returncode
    (folder / "receipt.json").write_text(json.dumps(dict(command=command, rc=rc, wall_ns=time.monotonic_ns()-start),indent=2)+"\n")
    raw = folder / "queue.raw"
    with raw.open("w") as stream:
        for line in (folder / "run.log").open():
            if line.startswith("Q "): stream.write(line[2:])
    with (folder / "queue.txt").open("wb") as stream:
        subprocess.run(["sort", "-s", "-k1,1n", "-k2,2n", "-k3,3n", str(raw)], stdout=stream,
                        env={**os.environ,"LC_ALL":"C"},check=True)
    raw.unlink()
    if rc: raise SystemExit(rc)
