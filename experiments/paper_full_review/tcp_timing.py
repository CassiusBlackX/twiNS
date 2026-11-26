"""Interleave the four ns-3/GPU arms with direct wide-history controls."""
import argparse
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "paper_tcp_options"))
from campaign import ARMS, clean, gpu_args, invoke, signature
from native import digest

p = argparse.ArgumentParser(description=__doc__)
for name in ("build", "native", "inputs", "output"):
    p.add_argument(name, type=lambda value: Path(value).resolve())
a = p.parse_args()
rows = []
for trial in range(4):
    arms = list(ARMS)
    for arm in arms[trial:]+arms[:trial]:
        sack, ts = ARMS[arm]
        case = "timing", "dctcp", a.inputs/"1024.csv", a.inputs/"1024.graph", 200, False, False
        reference = [a.native, "--threads=0", "--topo=file", f"--topofile={case[3]}", f"--flows={case[2]}",
                     "--tcp=ns3::TcpDctcp", "--stop=0.1", "--hostq=red", "--flowEcmp=1", "--queue=200",
                     f"--tcp-sack={sack}", f"--tcp-timestamps={ts}"]
        gpu = gpu_args(case, arm)
        commands = [("native",reference),("default",[a.build/"twins",*gpu])]
        if ts: commands.append(("direct",[a.build/"twins2048-options-links","--single-pass",*gpu]))
        for name, cmd in commands[::(-1 if trial%2 else 1)]:
            folder = a.output/"timing"/str(trial)/arm/name
            observed = invoke([*cmd, "--out=flows.csv"], folder, gpu_sample=name!="native")
            file = folder/"flows.csv"
            log = (folder/"run.log").read_text()
            observed.update(trial=trial, arm=arm, mode=name,
                            signature=signature(file) if file.is_file() else None,
                            accepted_flags=clean(folder) if name!="native" else True,
                            passes=[line for line in log.splitlines() if line.startswith(("TCP pass","TCP arrival ring"))])
            rows.append(observed)
            (a.output/"timing.json").write_text(json.dumps(rows, indent=2)+"\n")
            print(trial,arm,name,observed["rc"],flush=True)
    for arm in arms:
        matched = [r for r in rows if r["trial"]==trial and r["arm"]==arm]
        native = next(r for r in matched if r["mode"]=="native")
        assert native["signature"] and native["signature"]["completed"]==10240
        assert all(r["signature"]==native["signature"] and r["rc"]==0 and r["accepted_flags"] for r in matched)
for arm in ("timestamps","both"):
    case = "diagnostic","dctcp",a.inputs/"128.csv",a.inputs/"128.graph",200,False,False
    results = []
    for mode,cmd in (("default",[a.build/"twins"]),("direct",[a.build/"twins2048-options-links","--single-pass"])):
        folder=a.output/"small"/arm/mode
        row=invoke([*cmd,*gpu_args(case,arm),"--out=flows.csv","--qtrace=queue.txt","--qtrace_until=300000000000"],folder)
        assert row["rc"]==0 and clean(folder)
        results.append((signature(folder/"flows.csv"),digest(folder/"queue.txt")))
    assert results[0]==results[1]
