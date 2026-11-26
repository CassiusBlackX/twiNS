"""Check configuration and batch against the frozen 834-ns IRN cutoff oracle."""
import argparse
from collections import defaultdict
import json
from pathlib import Path
import re
import subprocess

p = argparse.ArgumentParser(description=__doc__)
for name in ("binary", "inputs", "out"):
    p.add_argument(name, type=lambda s: Path(s).resolve())
a = p.parse_args()
a.out.mkdir()
flow = a.out / "two.csv"
flow.write_text("src,dst,flow_size,start_ns\n0,2,970,2000000000\n1,2,4001,2000000040\n")
common = [str(a.binary),str(a.inputs/"irn.cfg"), "--from=1999999000", "--until=2000099999",
          "--fp-bases=1000003,1000033", "IRN_BDP=104000", "IRN_RTO_LOW_NS=834", "IRN_RTO_HIGH_NS=320000"]
summary = {}
for drain in (False, True):
    for first in (4170, 4171):
        stamp = 2_000_000_000 + first
        for mode in ("config","override","batch"):
            label = f"{'drain' if drain else 'default'}-{first}-{mode}"
            folder = a.out / label
            folder.mkdir()
            cmd = [*common, "--txtrace=wire.raw"]
            if drain:
                cmd.append("--drain")
            topo = a.inputs / "irn.graph"
            if mode != "batch":
                cfg = folder/"config"
                cfg.write_text(re.sub(r"(?m)^SIMULATOR_STOP_TIME.*\n", "", (a.inputs/"irn.cfg").read_text()) +
                               f"\nSIMULATOR_STOP_TIME {stamp/1e9:.9f}\n")
                cmd[1] = str(cfg)
                cmd += [f"--topo={topo}", f"--flows={flow}", "--out=out.fct"]
                if mode == "override":
                    cmd.append(f"--stop-ns={stamp}")
            else:
                second = 4170 + 4171 - first
                (folder/"batch").write_text("".join(f"w{i} {topo} {flow} SIMULATOR_STOP_TIME={(2_000_000_000+i)/1e9:.9f}\n"
                                                         for i in (first,second)))
                cmd += ["--batch=batch"]
            with (folder/"run.log").open("wb") as log:
                run = subprocess.run(cmd, cwd=folder, stdout=log, stderr=subprocess.STDOUT, timeout=70)
            summary[label] = dict(command=cmd, rc=run.returncode)
            (folder/"rc").write_text(f"{run.returncode}\n")
# A fixed cap above and below the complete 40-transmission drain trace.
for cap in (40,39):
    folder = a.out/f"trace-cap-{cap}"
    folder.mkdir()
    cmd = [*common, "--txtrace=wire.raw", f"--txtrace-cap={cap}", "--stop-ns=2000100000",
           f"--topo={a.inputs/'irn.graph'}", f"--flows={flow}", "--out=out.fct"]
    with (folder/"run.log").open("wb") as log:
        run = subprocess.run(cmd, cwd=folder, stdout=log, stderr=subprocess.STDOUT, timeout=70)
    summary[folder.name] = dict(command=cmd, rc=run.returncode)
    (folder/"rc").write_text(f"{run.returncode}\n")
(a.out/"runs.json").write_text(json.dumps(summary,indent=2)+"\n")
