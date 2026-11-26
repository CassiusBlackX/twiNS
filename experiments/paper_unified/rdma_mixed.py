"""Exercise per-world terminal states with a frozen stranded HPCC reference."""
import argparse
import json
from pathlib import Path
import re
import subprocess

p=argparse.ArgumentParser(description=__doc__)
for label in ("binary","inputs","out"):
    p.add_argument(label,type=lambda s:Path(s).resolve())
p.add_argument("--initial-fcap",type=int,default=4)
a=p.parse_args()
a.out.mkdir()
by_name={
    "healthy": ("healthy.csv", ["CC_MODE=1","SIMULATOR_STOP_TIME=0.02"]),
    "stranded": ("short.csv", ["CC_MODE=8","BUFFER_SIZE=1","PACKET_PAYLOAD_SIZE=9000","SIMULATOR_STOP_TIME=0.01"]),
    "loaded": ("k8_a2a30.csv", ["CC_MODE=1","SIMULATOR_STOP_TIME=0.02"]),
}
base=[str(a.binary),str(a.inputs/"hpcc.cfg"),"--fp-bases=1000003,1000033"]
topo=a.inputs/"fattree8.txt"
summary={}
for case in (*by_name,"forward","reverse"):
    d=a.out/case
    d.mkdir()
    if case in by_name:
        f,options=by_name[case]
        args=[*base,f"--topo={topo}",f"--flows={a.inputs/f}","--out=output.fct",*options]
        if case=="loaded":
            args.append(f"--fcap-auto={a.initial_fcap}")
    else:
        seq=list(by_name) if case=="forward" else reversed(by_name)
        (d/"worlds").write_text("".join(f"{name} {topo} {a.inputs/by_name[name][0]} {' '.join(by_name[name][1])}\n"
                                       for name in seq))
        args=[*base,"--batch=worlds",f"--fcap-auto={a.initial_fcap}"]
    with (d/"run.log").open("wb") as log:
        rc=subprocess.run(args,cwd=d,stdout=log,stderr=subprocess.STDOUT,timeout=110).returncode
    text=(d/"run.log").read_text(errors="replace")
    summary[case]=dict(command=args,rc=rc,passes=re.findall(r"flows done .*",text),
                       retries=re.findall(r"RDMA frame retry: .*",text),
                       flags=re.findall(r".*overflows: .*",text))
(a.out/"summary.json").write_text(json.dumps(summary,indent=2)+"\n")
for name in by_name:
    for order in ("forward","reverse"):
        left=(a.out/name/"output.fct").read_text().splitlines()
        right=(a.out/order/f"{name}.fct").read_text().splitlines()
        assert sorted(left)==sorted(right),(name,order,len(left),len(right))
assert summary["stranded"]["rc"]==2 and summary["healthy"]["rc"]==0 and summary["loaded"]["rc"]==0
assert summary["forward"]["rc"]==2 and summary["reverse"]["rc"]==2
print("All mixed-batch FCT identities and terminal statuses agree with the single-world controls.")
