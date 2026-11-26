"""Sample the simulator PID while measuring whole invocations, including recovery."""
import argparse
import csv
import json
import os
from pathlib import Path
import re
import subprocess
import time

p = argparse.ArgumentParser(description=__doc__)
p.add_argument("kind", choices=["rdma", "tcp"])
p.add_argument("binary", type=Path)
p.add_argument("candidate", type=Path)
p.add_argument("inputs", type=Path)
p.add_argument("out", type=Path)
p.add_argument("--trials", type=int, default=3)
p.add_argument("--compare-reuse", action="store_true")
p.add_argument("--tcp-sack", type=int, choices=[0, 1], default=1)
p.add_argument("--allocation-phases", action="store_true")
a = p.parse_args()
inputs = a.inputs.resolve()
modes = (
    {"auto": (a.binary, ["--q3-auto"]), "preset": (a.binary, ["--q3-overrides=overrides.txt"])}
    if a.kind == "rdma" else {"wide": (a.binary, []), "split": (a.candidate, [])}
)
if a.kind == "rdma" and a.compare_reuse:
    modes = {"auto": modes["auto"], "reuse": (a.candidate, ["--q3-auto", "--reuse-topology"]),
             "preset": modes["preset"]}
a.out.mkdir()
summary = []
for trial in range(a.trials):
    order = list(modes)
    for mode in order[trial % len(order):] + order[:trial % len(order)]:
        binary, options = modes[mode]
        d = a.out / f"{trial}-{mode}"
        d.mkdir()
        env = os.environ.copy()
        if a.allocation_phases:
            env["TWINS_CUDA_MEMORY_PHASES"] = "1"
        if a.kind == "rdma":
            (d / "overrides.txt").write_text("16000 1 8192\n")
            env["TWINS_TRACE_PORTS"] = str(inputs / "ports.log")
            args = [str(inputs / "strong.cfg"), f"--topo={inputs/'topo.log'}", f"--flows={inputs/'incast.csv'}",
                    "--out=output.fct", "--q3cap=2048", "CC_MODE=1", "--fp-bases=1000003,1000033",
                    "--txtrace=wire.raw", "--until=10000000"]
        else:
            args = ["--single-pass", "--tcp=dctcp", "--queue=200", "--spray=0", f"--tcp-sack={a.tcp_sack}",
                    "--tcp-timestamps=1", "--topo=graph", f"--topofile={inputs/'1024.graph'}", "--graph_k=16",
                    f"--flows={inputs/'1024.csv'}", "--out=flows.csv"]
        command = [str(binary.resolve()), *args, *options]
        samples = []
        with (d / "run.log").open("wb") as log:
            start = time.monotonic_ns()
            job = subprocess.Popen(command, cwd=d, env=env, stdout=log, stderr=subprocess.STDOUT)
            while job.poll() is None:
                dt = time.monotonic_ns() - start
                try:
                    status = Path(f"/proc/{job.pid}/status").read_text()
                except (FileNotFoundError, ProcessLookupError):
                    break
                rss = int((re.search(r"^VmHWM:\s+(\d+)", status, re.M) or [None, 0])[1])
                backing = 0
                if mode == "reuse":
                    for fd in Path(f"/proc/{job.pid}/fd").glob("*"):
                        try:
                            if os.readlink(fd).startswith("/memfd:twins-rdma-topology"):
                                backing = max(backing, fd.stat().st_size)
                        except (FileNotFoundError, ProcessLookupError):
                            pass
                gpu = subprocess.check_output(["nvidia-smi", "--query-compute-apps=pid,used_gpu_memory",
                                               "--format=csv,noheader,nounits"], text=True, timeout=4)
                mib = max((int(value) for pid, value in csv.reader(gpu.splitlines())
                           if pid.strip() == str(job.pid) and value.strip().isdigit()), default=0)
                samples.append((dt, rss, mib, backing))
                if dt > 180_000_000_000:
                    job.kill()
                    break
                time.sleep(.025)
            rc = job.wait()
            wall = time.monotonic_ns() - start
        row = dict(trial=trial, mode=mode, wall_ns=wall, pid=job.pid, rc=rc, command=command,
                   peak_rss_kib=max((s[1] for s in samples), default=0),
                   peak_gpu_mib=max((s[2] for s in samples), default=0),
                   topology_backing_bytes=max((s[3] for s in samples), default=0))
        (d / "samples.json").write_text(json.dumps(samples) + "\n")
        summary.append(row)
        (a.out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        print(row, flush=True)
