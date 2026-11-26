"""Measure when bounded RDMA queue recovery stops failed attempts."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import time

p = argparse.ArgumentParser(description=__doc__)
for name in ("binary","inputs","out"):
    p.add_argument(name,type=lambda s:Path(s).resolve())
p.add_argument("--hip-probe",action="store_true")
p.add_argument("--case",choices=["late","immediate","collect","auto","preset","fixed"])
a=p.parse_args()
a.out.mkdir()
if a.hip_probe:
    config, topo, flows, cap = ("hpcc.cfg","fattree8.txt","k8_a2a30.csv",4)
    env = os.environ.copy()
    common = ["CC_MODE=1","--delay=2000ns","SIMULATOR_STOP_TIME=0.01"]
    default, trials = 4096, 1
else:
    config, topo, flows, cap = ("strong.cfg","topo.log","incast.csv",2048)
    env = {**os.environ,"TWINS_TRACE_PORTS":str(a.inputs/"ports.log")}
    common = ["CC_MODE=1"]
    default, trials = 8192, 3
cases = ["late","immediate","collect","auto","preset","fixed"]
if a.case:
    cases = [a.case]
summary = []
for trial in range(trials):
    for case in cases[trial:]+cases[:trial]:
        if trial and case == "fixed":
            continue
        d = a.out / f"{trial}-{case}"
        d.mkdir()
        args = [str(a.binary),str(a.inputs/config),f"--topo={a.inputs/topo}",f"--flows={a.inputs/flows}",
                "--fp-bases=1000003,1000033","--out=output.fct","--txtrace=wire.raw",
                "--until=300000" if a.hip_probe else "--until=10000000",*common]
        if case == "preset":
            if not a.hip_probe:
                (d/"overrides").write_text("16000 1 8192\n")
                args += [f"--q3cap={cap}","--q3-overrides=overrides"]
            else:
                args += [f"--q3cap={default}"]
        else:
            args += [f"--q3cap={cap}"]
            if case != "auto":
                grace = dict(late=-1,immediate=0,collect=100000,fixed=0)[case]
                args += [f"--q3-stop-grace-ns={grace}"]
            if case != "fixed":
                args += ["--q3-auto","--reuse-topology"]
            else:
                args += ["--queue-capacity-report=overflow.txt"]
        start=time.monotonic_ns()
        with (d/"run.log").open("wb") as log:
            try:
                rc=subprocess.run(args,cwd=d,env=env,stdout=log,stderr=subprocess.STDOUT,timeout=150).returncode
            except subprocess.TimeoutExpired:
                rc=124
        wall=time.monotonic_ns()-start
        log=(d/"run.log").read_text(errors="replace")
        failures=[tuple(map(int,row)) for row in re.findall(
            r"first_ns=(\d+) stopped_ns=(\d+) port_classes=(\d+) storage_overflows=(\d+) grace_ns=(-?\d+)",log)]
        rec=dict(case=case, trial=trial, command=args, rc=rc, wall_ns=wall, failures=failures,
                 terminal=[line for line in log.splitlines() if "overflows:" in line][-1:])
        summary.append(rec)
        (d/"receipt.json").write_text(json.dumps(rec,indent=2)+"\n")
        (a.out/"summary.json").write_text(json.dumps(summary,indent=2)+"\n")
        print(case,trial,rc,len(failures),wall,flush=True)
