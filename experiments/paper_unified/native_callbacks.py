"""Write callback-entry times from live native SimAI, including its final callback."""
import argparse
import os
from pathlib import Path
import re
import shlex
import subprocess

p=argparse.ArgumentParser(description=__doc__)
for key in ("simulation","topology","inputs","out"):
    p.add_argument(key,type=lambda v:Path(v).resolve())
a=p.parse_args()
a.out.mkdir()
source=a.simulation/"scratch/AstraSimNetwork.cc"
entry=source.with_name("entry.h")
text=entry.read_text()
anchor="    o0 = ns3::TwinsOrd ();"
assert text.count(anchor)==1
text=text.replace(anchor,anchor+'''
    if (tw_out)
      fprintf (tw_out, "B %c %ld %lld\\n", tw_kind, tw_id,
               (long long) Simulator::Now ().GetNanoSeconds ());
''')
(a.out/"entry.h").write_text(text)
(a.out/source.name).write_text(source.read_text())
build=a.simulation/"cmake-cache"
rules=subprocess.check_output(["ninja","-C",str(build),"-t","commands"],text=True).splitlines()
cmd=next(shlex.split(line) for line in rules if line.endswith("/scratch/AstraSimNetwork.cc") and " -c " in line)
original=cmd[-1]
overlay=a.out/"include/ns3"
overlay.mkdir(parents=True)
archive=a.simulation.parents[4]
for header in (a.simulation/"build/include/ns3").iterdir():
    name=(Path(os.readlink(header)) if header.is_symlink() else header)
    if str(name).startswith("/simai/"):
        name=archive/str(name)[len("/simai/"):]
    elif not name.is_absolute():
        name=header.parent/name
    (overlay/header.name).symlink_to(name)
def locate(arg):
    return arg.replace("/simai/",str(archive)+"/")
obj=cmd[cmd.index("-o")+1]
result=a.out/"reference"
dst=a.out/"AstraSimNetwork.o"
cmd=[str(a.out/source.name) if x==original else str(dst) if x==obj else str(dst)+".d" if x==obj+".d" else locate(x)
     for x in cmd]
cmd[1:1]=["-include","cstdint",f"-I{overlay.parent}",f"-I{source.parent}"]
link=next(shlex.split(x)[2:-2] for x in rules
          if x.startswith(": && /usr/bin/c++ ") and "/ns3.36.1-AstraSimNetwork-optimized " in x)
link=[str(dst) if x==obj else str(result) if x.endswith("/ns3.36.1-AstraSimNetwork-optimized") else locate(x) for x in link]
with (a.out/"build.log").open("wb") as log:
    for c in (cmd,link):
        subprocess.run(c,cwd=build,stdout=log,stderr=subprocess.STDOUT,check=True,timeout=150)
base=(a.inputs/"config.log").read_text()
config=re.sub(r"(?m)^BUFFER_SIZE\s+\d+", "BUFFER_SIZE 64",base)
config=re.sub(r"(?m)^(\S+\s+)/etc/[^\s]+/([^/\s]+)$",r"\1"+str(a.out)+r"/\2",config)
(a.out/"config").write_text(config)
env={**os.environ,"LD_LIBRARY_PATH":str(a.simulation/"build/lib"),"AS_SEND_LAT":"3",
     "TWINS_TRACE":str(a.out/"events.trace")}
with (a.out/"run.log").open("wb") as log:
    r=subprocess.run([result,"-t","1","-w",a.inputs/"simai-workload","-n",a.topology,"-c",a.out/"config"],
                     cwd=a.out,env=env,stdout=log,stderr=log,timeout=120)
(a.out/"rc").write_text(f"{r.returncode}\n")
with (a.out/"events.trace").open() as source_log,(a.out/"callbacks.txt").open("w") as out:
    for row in source_log:
        if row.startswith("B "):
            out.write("C "+row[2:])
