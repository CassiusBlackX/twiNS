"""Compile the original packet-layout reference with only the low timer configurable."""
from concurrent.futures import ThreadPoolExecutor
import os
import shutil
from pathlib import Path
import subprocess
import sys

root=Path.cwd()
stock=root/"conweave-stock"
first_size=int(os.environ.get("TWINS_IRN_FIRST_SIZE","3001"))
out=root/("irn-stock" if first_size==3001 else f"irn-stock-{first_size}")
out.mkdir(exist_ok=True)
binary=root/"irn-stock/reference"
libraries=stock/"build"
if not binary.exists():
    source=(stock/"scratch/network-load-balance.cc").read_text()
    line='rdmaHw->SetAttribute("IrnRtoLow", TimeValue(MicroSeconds(100)));'
    assert source.count(line)==1
    source=source.replace(line,'rdmaHw->SetAttribute("IrnRtoLow", TimeValue(NanoSeconds(std::stoll(getenv("TWINS_IRN_LOW_NS")))));')
    (out/"main.cc").write_text(source)
    with (out/"build.log").open("wb") as log:
        subprocess.run(["g++","-O3","-DNDEBUG","-std=c++11",f"-I{libraries}",out/"main.cc","-Wl,--no-as-needed",
                        *sorted(libraries.glob("libns3.19-*-optimized.so")),"-Wl,--as-needed","-lpthread","-lrt","-o",binary],
                        stdout=log,stderr=log,check=True,timeout=150)
topology=root/"leaf_spine_128_100G_OS2.txt"
shutil.copyfile(root/"irn-topo.txt",topology)
config="\n".join("TOPOLOGY_FILE "+str(topology) if line.startswith("TOPOLOGY_FILE ") else line
                 for line in (root/"irn.cfg").read_text().splitlines())+"\n"

def run(low):
    d=out/str(low)
    d.mkdir()
    (d/"config").write_text(config)
    (d/"ref.flows").write_text(f"2\n0 2 3 {first_size} 2.0\n1 2 3 4001 2.00000004\n")
    env={**os.environ,"LD_LIBRARY_PATH":str(libraries),"TWINS_IRN_LOW_NS":str(low),
         "TWINS_TXTRACE":str(d/"wire.raw"),"TWINS_TXTRACE_MAX_NS":"2000200000"}
    with (d/"run.log").open("wb") as log:
        rc=subprocess.run([binary,d/"config"],cwd=d,env=env,stdout=log,stderr=log,timeout=90).returncode
    (d/"rc").write_text(str(rc)+"\n")
    print(low,rc,flush=True)
with ThreadPoolExecutor(max_workers=6) as pool:
    list(pool.map(run,map(int,sys.argv[1:])))
