"""Record native DCQCN timer order without changing the installed HPCC library."""
import argparse
from pathlib import Path
import subprocess

p=argparse.ArgumentParser(description=__doc__)
for name in ("hpcc","reference_source","output"):
    p.add_argument(name,type=lambda value:Path(value).resolve())
a=p.parse_args()
a.output.mkdir(parents=True,exist_ok=False)
model=a.hpcc/"simulation/src/point-to-point/model"
libraries=a.hpcc/"simulation/build"
orig=(model/"rdma-hw.cc").read_text()
for signature,label in (("RdmaHw::UpdateAlphaMlx","alpha"),("RdmaHw::CheckRateDecreaseMlx","dec"),
                        ("RdmaHw::RateIncEventTimerMlx","rp"),("RdmaHw::cnp_received_mlx","cnp"),("RdmaHw::QpComplete","done")):
    if label=="done":
        continue
    anchor=f"void {signature}(Ptr<RdmaQueuePair> q){{"
    assert orig.count(anchor)==1, signature
    msg=f'\n\tprintf("DCQCN %lu %u %u %u {label} %d %d %lu\\n", Simulator::Now().GetTimeStep(), (q->sip.Get()>>8)&0xffff, (q->dip.Get()>>8)&0xffff, q->sport, int(q->mlx.m_alpha_cnp_arrived), int(q->mlx.m_decrease_cnp_arrived), q->m_rate.GetBitRate());'
    orig=orig.replace(anchor,anchor+msg)
source=a.output/"rdma-hw-audit.cc"
source.write_text(orig)
cmd=["g++","-O3","-DNDEBUG","-std=c++11",f"-I{libraries}",f"-I{model}",str(a.reference_source),str(source),
     "-Wl,--export-dynamic","-Wl,--no-as-needed",*map(str,sorted(libraries.glob("libns3.*-optimized.so"))),
     "-Wl,--as-needed","-lpthread","-lrt","-o",str(a.output/"reference")]
with (a.output/"build.log").open("wb") as log:
    subprocess.run(cmd,stdout=log,stderr=subprocess.STDOUT,check=True,timeout=150)
