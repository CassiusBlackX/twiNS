"""Instrument native HPCC completion order and disable post-completion callbacks in a control."""
from pathlib import Path
import os
import subprocess

root=Path.cwd()
hpcc=Path("/home/cassius/netsim/hpcc/simulation")
out=root/"hpcc-close-final"
out.mkdir()
model=hpcc/"src/point-to-point/model"
src=(model/"rdma-hw.cc").read_text()
src="#include <cstdlib>\n"+src
for func,param,name in (("UpdateAlphaMlx","q","alpha"),("CheckRateDecreaseMlx","q","dec"),
                         ("RateIncEventTimerMlx","q","rp"),("cnp_received_mlx","q","cnp"),("QpComplete","qp","close")):
    anchor=f"void RdmaHw::{func}(Ptr<RdmaQueuePair> {param}){{"
    assert src.count(anchor)==1
    log=f'\n\tprintf("LIFE %lu %u %u %u {name} %d\\n", Simulator::Now().GetTimeStep(), ({param}->sip.Get()>>8)&0xffffu, ({param}->dip.Get()>>8)&0xffffu, {param}->sport, int({param}->IsFinished()));'
    if name!="close":
        log+="\n\tif (getenv(\"TWINS_SUPPRESS_AFTER_CLOSE\") && q->IsFinished()) return;"
    src=src.replace(anchor,anchor+log,1)
instrumented=out/"rdma-hw-audit.cc"
instrumented.write_text(src)
build=hpcc/"build"
driver=Path("/home/cassius/netsim/twins-full-review-20261126/twins-paper-tcp-options/validation/hpcc/rdma-ref.cc")
binary=out/"reference"
with (out/"build.log").open("wb") as log:
    subprocess.run(["g++","-O3","-DNDEBUG","-std=c++11",f"-I{build}",f"-I{model}",driver,instrumented,
                    "-Wl,--export-dynamic","-Wl,--no-as-needed",*sorted(build.glob("libns3.*-optimized.so")),
                    "-Wl,--as-needed","-lpthread","-lrt","-o",binary],stdout=log,stderr=log,check=True,timeout=150)
inputs=Path("/home/cassius/netsim/twins-full-review-20261126/inputs")
jobs=[]
for delay in (2000,5000,10000):
    for mode in ("original","suppressed"):
        d=out/f"{delay}-{mode}"
        d.mkdir()
        env={**os.environ,"LD_LIBRARY_PATH":str(build)}
        if mode=="suppressed": env["TWINS_SUPPRESS_AFTER_CLOSE"]="1"
        cmd=[binary,inputs/"hpcc.cfg",f"--flows={inputs/'k8_a2a30.csv'}",f"--topo={inputs/'fattree8.txt'}",
             f"--delay={delay}ns","--out=flows.fct","--txtrace=wire.raw","--until=4500000","CC_MODE=1"]
        jobs.append((d,cmd,env))
from concurrent.futures import ThreadPoolExecutor
def run(item):
    d,cmd,env=item
    with (d/"run.log").open("wb") as log:
        subprocess.run(cmd,cwd=d,env=env,stdout=log,stderr=log,check=True,timeout=90)
with ThreadPoolExecutor(max_workers=6) as workers:
    list(workers.map(run,jobs))
(out/"rc").write_text("0\n")
