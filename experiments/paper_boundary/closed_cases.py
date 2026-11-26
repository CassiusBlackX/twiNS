"""Exercise normal closed-loop status and cache publication on one device."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time

p = argparse.ArgumentParser(description=__doc__)
for name in ("binary", "inputs", "out"):
    p.add_argument(name, type=lambda v: Path(v).resolve())
p.add_argument("--baseline", action="store_true")
a = p.parse_args()
a.out.mkdir(parents=True,exist_ok=False)
s = a.inputs
out = a.out
rows = {}
short = out/"short.wl"
short.write_text((s/"workload.log").read_text().replace("8388608","1048576"))
bigger = out/"bigger.wl"
bigger.write_text(short.read_text().replace("1048576","2097152"))
repeated = out/"repeated.wl"
head,_,work=short.read_text().splitlines()
repeated.write_text(f"{head}\n2\n{work}\n"+work.replace("coll_layer","coll_extra")+"\n")
topology = out/"alternate.topo"
topology.write_text((s/"topo.log").read_text()+"\n")
cache=out/"inventory.dag"

def run(name, cfg=s/"config.log", wl=short, topo=s/"topo.log", flags=(), timeout=75):
    d=out/name
    d.mkdir()
    command=[str(a.binary),str(cfg),f"--astra={wl}",f"--topo={topo}","--out=messages.fct","--dag-out=actions.dag",
             "--nvl-islands=0","--gwin=0","--fp-bases=1000003,1000033",*flags]
    start=time.monotonic_ns()
    with (d/"run.log").open("wb") as log:
        try:
            rc=subprocess.run(command,cwd=d,stdout=log,stderr=subprocess.STDOUT,timeout=timeout).returncode
        except subprocess.TimeoutExpired:
            rc=124
    elapsed=time.monotonic_ns()-start
    log=(d/"run.log").read_text(errors="replace")
    files={}
    for f in ("messages.fct","actions.dag","capacity.txt"):
        if (d/f).is_file():
            files[f]=hashlib.sha256((d/f).read_bytes()).hexdigest()
    info=dict(rc=rc,wall_ns=elapsed,command=command,files=files,
              last=[line for line in log.splitlines() if line.startswith(("closed status:","closed failure:","closed cutoff:","flows done ","drops "))],
              kept=bool(re.search(r"^the system layer.s flows: .*kept",log,re.M)))
    rows[name]=info
    print(name,rc,info["kept"],flush=True)
    (out/"summary.json").write_text(json.dumps(rows,indent=2)+"\n")
    return info

fixed_cache=[f"--flows-cache={cache}"]
if a.baseline:
    run("queue-fixed",cfg=s/"finite.cfg",flags=["--q3cap=1","--queue-capacity-report=capacity.txt"],timeout=25)
    run("cache-create",flags=fixed_cache)
    run("cache-stale",wl=bigger,flags=fixed_cache)
    raise SystemExit(0)
if (s/"stall.cfg").exists():
    run("reference-stall",cfg=s/"stall.cfg",wl=s/"workload.log")
run("queue-fixed",flags=["--q3cap=1","--queue-capacity-report=capacity.txt"])
run("action-fixed",flags=["--closed-action-cap=1"])
run("cutoff",cfg=s/"finite.cfg",wl=s/"workload.log")
run("cold")
run("cache-create",flags=fixed_cache)
run("cache-buffer",flags=[*fixed_cache,"BUFFER_SIZE=64"])
run("buffer-cold",flags=["BUFFER_SIZE=64"])
run("cache-size",wl=bigger,flags=fixed_cache)
run("size-cold",wl=bigger)
run("cache-repeated",wl=repeated,flags=fixed_cache)
run("repeated-cold",wl=repeated)
run("cache-topology",wl=repeated,topo=topology,flags=fixed_cache)
run("topology-cold",wl=repeated,topo=topology)
raw=cache.read_bytes()
cache.write_bytes(raw[:len(raw)//2])
run("cache-interrupted",wl=repeated,topo=topology,flags=fixed_cache)
run("cache-after-repair",wl=repeated,topo=topology,flags=fixed_cache)

pairs=(("cold","cache-create"),("cache-buffer","buffer-cold"),("cache-size","size-cold"),("cache-repeated","repeated-cold"),
       ("cache-topology","topology-cold"),("cache-interrupted","topology-cold"),("cache-after-repair","topology-cold"))
same={f"{x}/{y}":bool(rows[x]["files"] and rows[x]["rc"]==rows[y]["rc"]==0 and rows[x]["files"]==rows[y]["files"]) for x,y in pairs}
expected_reuse={"cache-create":False,"cache-buffer":True,"cache-size":False,"cache-repeated":False,"cache-topology":False,
                "cache-interrupted":False,"cache-after-repair":True}
flags={key:rows[key]["rc"]==rc and any(reason in line for line in rows[key]["last"])
       for key,rc,reason in (("queue-fixed",3,"switch-queue"),("action-fixed",3,"action-capacity"),("cutoff",2,"cutoff"))}
report=dict(matched=same,reuse={key:rows[key]["kept"]==value for key,value in expected_reuse.items()},flags=flags)
(out/"acceptance.json").write_text(json.dumps(report,indent=2)+"\n")
raise SystemExit(0 if all(x for group in report.values() for x in group.values()) else 1)
