"""Compare the frozen 1,024-host TCP workloads with options off and both on."""
import argparse
import csv
from contextlib import nullcontext
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time

p = argparse.ArgumentParser(description=__doc__)
p.add_argument("part", choices=["cpu", "gpu"])
p.add_argument("binary", type=lambda v: Path(v).resolve())
p.add_argument("flows", type=lambda v: Path(v).resolve())
p.add_argument("out", type=lambda v: Path(v).resolve())
p.add_argument("--trials", type=int, default=3)
p.add_argument("--lock", type=Path)
p.add_argument("--resume", action="store_true")
p.add_argument("--case",choices=["k16_a2a30","k16_a2a50s","k16_incast16"])
a = p.parse_args()
a.out.mkdir(exist_ok=a.resume)
cases = [("dctcp", "k16_a2a30"), ("cubic", "k16_a2a50s"), ("newreno", "k16_incast16")]
if a.case:
    cases = [case for case in cases if case[1] == a.case]
summary = a.out / "summary.json"
results = json.loads(summary.read_text()) if a.resume and summary.is_file() else []
finished = {(row["trial"], row["case"], row["both"], row["engine"]) for row in results}
for trial in range(a.trials):
    for cc, case in cases:
        for flags in (trial % 2, 1 - trial % 2):
            for engine in (("ns3", "unison32") if a.part == "cpu" else ("twins",)):
                if (trial, case, flags, engine) in finished:
                    continue
                work = a.out / f"{trial}-{case}-{flags}-{engine}"
                if work.exists() and a.resume:
                    work.rename(work.with_name(f"{work.name}.interrupted-{time.monotonic_ns()}"))
                work.mkdir()
                flow = a.flows / f"{case}.csv"
                opts = [f"--tcp-sack={flags}", f"--tcp-timestamps={flags}"]
                if engine == "twins":
                    command = ["taskset", "-c", "0", str(a.binary), "--k=16",
                               f"--tcp={cc}", f"--flows={flow}", "--max_slots=100000"]
                else:
                    name = {"dctcp": "Dctcp", "cubic": "Cubic", "newreno": "NewReno"}[cc]
                    command = ["taskset", "-c", "0" if engine == "ns3" else "0-31",
                               str(a.binary), "--k=16", f"--tcp=ns3::Tcp{name}", f"--flows={flow}",
                               "--threads=0" if engine == "ns3" else "--threads=32",
                               "--hostq=red", "--flowEcmp=1", "--stop=0.1"]
                command += ["--queue=200", "--ecnK=30", "--out=flows.csv", *opts]
                with (a.lock.open("a") if a.lock else nullcontext()) as lock:
                    if lock:
                        fcntl.flock(lock, fcntl.LOCK_EX)
                    start = time.monotonic_ns()
                    load = os.getloadavg()
                    with (work / "run.log").open("wb") as log:
                        try:
                            rc = subprocess.run(command, cwd=work, stdout=log, stderr=subprocess.STDOUT,
                                                timeout=720).returncode
                        except subprocess.TimeoutExpired:
                            rc = 124
                    wall = time.monotonic_ns() - start
                record = dict(case=case, trial=trial, both=flags, engine=engine, wall_ns=wall, rc=rc,
                              command=command, loadavg=load, input_sha256=hashlib.sha256(flow.read_bytes()).hexdigest())
                log = (work / "run.log").read_text(errors="replace")
                record["event_loop_report"] = re.findall(r"^.*(?:event_loop_ns|Simulation Duration:|wall |CPU Engine).*$", log, re.M)
                data = work / "flows.csv"
                if data.exists():
                    digest = hashlib.sha256()
                    completed = 0
                    count = 0
                    rtx = 0
                    with data.open() as stream:
                        for row in csv.DictReader(stream):
                            wanted = [row[key] for key in ("src", "dst", "flow_size", "start_ns", "established_ns", "completed_ns")]
                            digest.update((",".join(wanted) + "\n").encode())
                            completed += int(wanted[-1]) >= 0
                            count += 1
                            rtx += int(row.get("retx") or 0)
                    record.update(fct_sha256=digest.hexdigest(), rows=count, completed=completed)
                    match = re.findall(r"\bretx ([0-9]+)", log)
                    record["retx"] = rtx if engine != "twins" else (int(match[-1]) if match else None)
                (work / "receipt.json").write_text(json.dumps(record, indent=2) + "\n")
                results.append(record)
                snapshot = a.out / "summary.pending"
                snapshot.write_text(json.dumps(results, indent=2) + "\n")
                snapshot.replace(summary)
                print(trial, case, flags, engine, rc, flush=True)
