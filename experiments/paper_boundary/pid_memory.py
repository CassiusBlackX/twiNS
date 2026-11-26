"""Recheck the frozen TCP timings and attribute GPU memory by process group."""
import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time

p = argparse.ArgumentParser(description=__doc__)
for name in ("binaries", "inputs", "out"):
    p.add_argument(name, type=lambda s: Path(s).resolve())
p.add_argument("--expected", required=True)
p.add_argument("--phase-logs", action="store_true")
a = p.parse_args()
a.out.mkdir(parents=True, exist_ok=False)
rows = []
for trial in range(6):
    for mode in (("default", "direct") if trial % 2 == 0 else ("direct", "default")):
        folder = a.out / str(trial) / mode
        folder.mkdir(parents=True)
        args = [a.binaries / "twins"] if mode == "default" else [a.binaries / "twins2048-options-links", "--single-pass"]
        cmd = list(map(str, args + ["--tcp=dctcp", "--queue=200", "--spray=0", "--tcp-sack=0",
                    "--tcp-timestamps=1", "--topo=graph", f"--topofile={a.inputs/'1024.graph'}", "--graph_k=16",
                    f"--flows={a.inputs/'1024.csv'}", "--out=flows.csv"]))
        sample = trial >= 3
        env = os.environ.copy()
        if a.phase_logs:
            env["TWINS_CUDA_MEMORY_PHASES"] = "1"
        start = time.monotonic_ns()
        records = []
        with (folder/"run.log").open("wb") as log:
            job = subprocess.Popen(cmd, cwd=folder, stdout=log, stderr=subprocess.STDOUT,
                                   start_new_session=True, env=env)
            while sample and job.poll() is None:
                now = time.monotonic_ns()
                report = subprocess.run(["nvidia-smi", "--query-compute-apps=pid,used_gpu_memory",
                    "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=3, check=True)
                for items in csv.reader(report.stdout.splitlines()):
                    if len(items) != 2 or not all(x.strip().isdigit() for x in items):
                        continue
                    pid, mib = map(int, items)
                    try:
                        ours = os.getpgid(pid) == job.pid
                    except ProcessLookupError:
                        ours = None
                    records.append((now-start, pid, mib, ours))
                if now-start > 150_000_000_000:
                    os.killpg(job.pid, 9)
                    break
                time.sleep(.02)
            rc = job.wait(timeout=150)
        elapsed = time.monotonic_ns()-start
        (folder/"samples.json").write_text(json.dumps(records)+"\n")
        fct = folder/"flows.csv"
        digest = None
        if fct.exists():
            with fct.open() as stream:
                rd = csv.reader(stream)
                next(rd)
                values = [tuple(map(int, row[:6])) for row in rd]
            assert len(values) == 10240 and all(row[-1] >= 0 for row in values)
            ordered = values
            digest = hashlib.sha256("".join(",".join(map(str,r))+"\n" for r in ordered).encode()).hexdigest()
        per_pid = {}
        totals = {}
        for tick,pid,mib,ours in records:
            row = per_pid.setdefault(pid, {"peak_mib":0,"owned_samples":0,"foreign_samples":0,"vanished_samples":0})
            row["peak_mib"] = max(row["peak_mib"],mib)
            row["owned_samples" if ours is True else "foreign_samples" if ours is False else "vanished_samples"] += 1
            totals[tick] = totals.get(tick,0)+mib
        text = (folder/"run.log").read_text()
        row = dict(trial=trial, mode=mode, sampled=sample, cmd=cmd, rc=rc, wall_ns=elapsed, fct_sha256=digest,
                   pids=per_pid, owned_max=max((mib for _,_,mib,ours in records if ours), default=None),
                   all_processes_max=max(totals.values(),default=None),
                   terminal=re.findall(r"^flows .*\btx-cap .*$",text,re.M)[-1:] or [],
                   passes=[line for line in text.splitlines() if line.startswith(("TCP pass ","TCP memory "))])
        row["accepted"] = rc == 0 and digest == a.expected
        (folder/"receipt.json").write_text(json.dumps(row,indent=2)+"\n")
        print(trial, mode, row["accepted"], row["owned_max"], row["all_processes_max"], flush=True)
        rows.append(row)
        (a.out/"summary.json").write_text(json.dumps(rows,indent=2)+"\n")
raise SystemExit(0 if all(row["accepted"] for row in rows) else 1)
