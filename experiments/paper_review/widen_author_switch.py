"""Adapt the pinned author driver to devices 128–256 without changing ECN/MMU rules."""
import argparse
from pathlib import Path
import shutil
import subprocess

parser=argparse.ArgumentParser()
for name in ("source","clone","aligned","objects"):
    parser.add_argument(name,type=Path)
args=parser.parse_args()
source,clone,aligned,objects=(p.resolve() for p in (args.source,args.clone,args.aligned,args.objects))
shutil.copytree(source,clone,symlinks=True)
model=clone/"src/point-to-point/model"
for name in ("switch-node.h","switch-mmu.h"):
    path=model/name
    old="static const unsigned pCnt = 128;"
    text=path.read_text()
    if text.count(old)!=1:
        raise ValueError(f"author port capacity changed: {name}")
    path.write_text(text.replace(old,"static const unsigned pCnt = QbbNetDevice::pCnt;" if name=="switch-node.h"
                                else "static const unsigned pCnt = 257;"))
    # Waf can expose headers via symlinks to the original tree. Force this
    # experiment's include directory to point at the matching enlarged types.
    exported=clone/"build/ns3"/name
    if exported.exists() or exported.is_symlink():
        exported.unlink()
    exported.symlink_to(path)
shutil.copytree(aligned,objects)
for name in ("switch-node","switch-mmu","rdma-hw","qbb-net-device"):
    src=objects/(name+".cc") if (objects/(name+".cc")).is_file() else model/(name+".cc")
    subprocess.run(["g++","-O3","-fPIC","-std=gnu++11","-w",
                    "-DHAVE_PACKET_H=1","-DHAVE_SQLITE3=1","-DHAVE_IF_TUN_H=1",
                    f"-I{clone/'build'}",f"-I{clone}",f"-I{model}","-c",src,
                    "-o",objects/(name+".o")],check=True)
