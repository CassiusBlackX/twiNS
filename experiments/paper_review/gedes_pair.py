"""Measure the two audited HIP implementations on paired routes and arrivals."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import os
import re
import subprocess
import time


def inputs(reference, source, h):
    with reference.open() as f:
        flows=list(csv.DictReader(f))
    with source.open() as f:
        offered=list(csv.DictReader(f))
    expected=[(int(f["src"]),int(f["flow_size"]),int(f["start_ns"])) for f in flows]
    actual=[(int(f["node"]),int(f["flow_size"]),int(f["start_ns"])) for f in offered]
    if expected!=actual or len(flows)!=h or any(int(f["dst"])!=(int(f["src"])+h//2)%h for f in flows):
        raise ValueError("the two engines do not receive the same frozen paired inputs")


def run(engine, command, env, output, expected):
    start=time.monotonic_ns()
    with (output/"run.log").open("w") as f:
        try:
            rc=subprocess.run(command,env=env,cwd=output,stdout=f,stderr=subprocess.STDOUT,timeout=180).returncode
        except subprocess.TimeoutExpired:
            rc=124
    wall=time.monotonic_ns()-start
    log=(output/"run.log").read_text()
    count=0
    last=None
    if (output/"flows.csv").is_file():
        with (output/"flows.csv").open() as f:
            finished="completed_timestamp" if engine=="gedes" else "completed_ns"
            rows=[int(r[finished]) for r in csv.DictReader(f)]
        count=sum(x>0 for x in rows)
        last=max(rows,default=None)
    if engine=="twins" and re.search(r"^flows 0 completed 0",log,re.M):
        raise ValueError("TwiNS loaded no flows")
    slots=re.search(r"Executed Slots:\s*(\d+)",log)
    sim=re.search(r"Simulation Duration:\s*([\d.]+)s",log)
    return dict(rc=rc, command=command, wall_ns=wall, expected=expected, completed=count,
                complete=rc==0 and count==expected, last_completion_ns=last,
                slots=int(slots[1]) if slots else None, loop_s=float(sim[1]) if sim else None,
                bound_ns=10_000_000,
                ecmp_report=next((l for l in log.splitlines() if "per-flow ECMP" in l),None))


def main():
    p=argparse.ArgumentParser()
    for key in ("workspace","gedes","twins","out"):p.add_argument(key,type=Path)
    p.add_argument("--light-control", action="store_true")
    a=p.parse_args()
    root=a.workspace.resolve()
    for k in (8,16):
        h=k**3//4
        hashfile=root/f"specsim/fidelity/audit/ns3/k{k}_paper_Dctcp_flow_qb.csv"
        offered=root/f"twiNS/data/flows/k{k}_paper.csv"
        if a.light_control:
            # Use the same deterministic pairing and static path mapping.
            # A separate low-load control fits both models' native recovery horizon.
            frozen=a.out.resolve()/f"k{k}"/"inputs"
            frozen.mkdir(parents=True)
            hashfile,offered=frozen/"twins.csv",frozen/"gedes.csv"
            with hashfile.open("w",newline="") as f, offered.open("w",newline="") as g:
                twin_rows=csv.writer(f)
                twin_rows.writerow(("src","dst","flow_size","start_ns","established_ns",
                                    "completed_ns","hash_data","hash_ack"))
                gedes_rows=csv.writer(g)
                gedes_rows.writerow(("node","flow_size","start_ns"))
                for source in range(h):
                    dest=(source+h//2)%h
                    size=32*1460
                    start=1000+(source//(k//2))*2000
                    twin_rows.writerow((source,dest,size,start,-1,-1,source%(k//2),dest%(k//2)))
                    gedes_rows.writerow((source,size,start))
        inputs(hashfile,offered,h)
        for trial in range(1,4):
            for engine in (("twins","gedes") if trial%2 else ("gedes","twins")):
                d=a.out.resolve()/f"k{k}"/f"trial-{trial}"/engine
                d.mkdir(parents=True)
                cmd=([str(a.gedes.resolve()),f"--ft_k={k}",f"--flow_file={offered}","--max_slots=10000","--output=flows.csv"]
                     if engine=="gedes" else
                     [str(a.twins.resolve()),f"--k={k}","--tcp=dctcp",f"--flows={offered}",f"--ecmp_from={hashfile}",
                      "--ecnK=30","--queue=200","--max_slots=10000","--out=flows.csv"])
                env=dict(os.environ)
                if engine=="gedes":env["GEDES_ECMP_FILE"]=str(hashfile)
                r=run(engine,cmd,env,d,h)
                r["input_sha256"]={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (offered,hashfile)}
                (d/"receipt.json").write_text(json.dumps(r,indent=2))


if __name__=="__main__":
    main()
