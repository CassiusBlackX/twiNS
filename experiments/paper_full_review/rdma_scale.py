"""Size only queue ports that overflow, rerunning the unchanged strong incast."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import time

p=argparse.ArgumentParser(description=__doc__)
for name in ("binary","strong","light","k32","output"):
    p.add_argument(name,type=lambda value:Path(value).resolve())
a=p.parse_args()
a.output.mkdir(parents=True,exist_ok=False)
sizes={}
def run(label, case, flow, cap, overrides=(), trace=False):
    out=a.output/label
    out.mkdir()
    config=case/("strong.cfg" if case==a.strong else "config.log")
    cmd=[str(a.binary),str(config),f"--topo={case/'topo.log'}",f"--flows={case/flow}",
         "--out=output.fct",f"--q3cap={cap}","CC_MODE=1","--fp-bases=1000003,1000033"]
    if case==a.strong:
        cmd.append("--queue-capacity-report=overflow.txt")
    if overrides:
        (out/"overrides.txt").write_text("".join(f"{k[0]} {k[1]} {v}\n" for k,v in sorted(overrides.items())))
        cmd.append("--q3-overrides=overrides.txt")
    if trace:
        cmd+=["--txtrace=wire.raw","--until=10000000"]
    env=os.environ.copy()
    if trace: env["TWINS_TRACE_PORTS"]=str(case/"ports.log")
    peak=0
    started=time.monotonic_ns()
    with (out/"run.log").open("wb") as log, subprocess.Popen(cmd,cwd=out,env=env,stdout=log,stderr=subprocess.STDOUT) as job:
        while job.poll() is None:
            values=subprocess.check_output(["nvidia-smi","--query-compute-apps=pid,used_gpu_memory","--format=csv,noheader,nounits"],text=True)
            for line in values.splitlines():
                fields=line.split(",")
                if int(fields[0])==job.pid and fields[1].strip().isdigit(): peak=max(peak,int(fields[1]))
            if time.monotonic_ns()-started>240_000_000_000: job.kill(); job.wait()
            time.sleep(.025)
    receipt=dict(command=cmd, rc=job.returncode, peak_mib=peak, wall_ns=time.monotonic_ns()-started)
    (out/"receipt.json").write_text(json.dumps(receipt,indent=2)+"\n")
    print(label,receipt["rc"],peak,flush=True)
    return receipt, out

for trial in range(6):
    row,path=run(f"strong-{trial}",a.strong,"incast.csv",2048,sizes,trace=True)
    if row["rc"]==0: break
    if row["rc"]!=3 or not (path/"overflow.txt").exists(): break
    limits=[tuple(map(int,line.split())) for line in (path/"overflow.txt").read_text().splitlines()[1:]]
    if not limits or any(r[3]!=3 for r in limits): break
    for _,node,dev,_,occ in limits: sizes[node,dev]=2*occ
else:
    raise SystemExit("hot-adaptation limit reached")
if row["rc"]!=0: raise SystemExit("strong incast did not finish cleanly")
run("light",a.light,"incast.csv",2048)
run("k32",a.k32,"distributed.csv",2048)
