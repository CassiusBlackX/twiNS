"""Summarize timing pairs, island waits and PFC service from returned receipts."""
import argparse
import collections
import hashlib
import json
from pathlib import Path
import statistics

p=argparse.ArgumentParser(description=__doc__)
p.add_argument("results",type=Path)
a=p.parse_args()
root=a.results

def hash_file(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream,"sha256").hexdigest()

def collect(path):
    slices=collections.defaultdict(collections.Counter)
    previous=None
    with path.open() as stream:
        next(stream)
        for line in stream:
            limit,start,last,wait,copy,continuation,chunks,records=map(int,line.split())
            keys=("all","nonempty" if records else "empty","same_limit" if limit==previous else "advanced")
            for key in keys:
                row=slices[key]
                row.update(rounds=1, first_wait_ns=wait, last_ready_offset_ns=max(0,last-start),
                           copy_ns=copy, continuation_ns=continuation, chunks=chunks, records=records)
            previous=limit
    return dict(slices)

report={}
timing=root/"cuda-tcp-v2/timing.json"
if timing.exists():
    raw=json.loads(timing.read_text())
    rows=collections.defaultdict(list)
    for r in raw:
        if r["trial"]: rows[r["arm"],r["mode"]].append(r)
    def metric(rs,key,scale=1):
        vals=[r[key]/scale for r in rs if r[key] is not None]
        return dict(median=statistics.median(vals),minimum=min(vals),maximum=max(vals)) if vals else None
    result={}
    for (arm,mode),rs in rows.items():
        result[f"{arm}/{mode}"]=dict(samples=len(rs),wall_s=metric(rs,"wall_ns",1e9),loop_s=metric(rs,"loop_s"),
                                    peak_gib=metric(rs,"gpu_peak_mib",1024),
                                    accepted=all(r["rc"]==0 and r["accepted_flags"] and r["signature"]==next(x["signature"] for x in raw if x["trial"]==r["trial"] and x["arm"]==arm and x["mode"]=="native") for r in rs))
    report["tcp"]=result
ai=root/"cuda-rdma/ai"
if not ai.exists(): ai=root/"cuda-ai-progress"
if ai.exists():
    result={}
    for base in sorted(ai.glob("gpt*")):
        info={"runs":{}}
        for path in sorted(base.glob("p*")):
            if not (path/"receipt.json").is_file(): continue
            r=json.loads((path/"receipt.json").read_text())
            info["runs"][path.name]=r
            if (path/"collect.txt").exists(): r["collect"]=collect(path/"collect.txt")
        group=[r for k,r in info["runs"].items() if "changed" not in k]
        changed=[r for k,r in info["runs"].items() if "changed" in k]
        for key,rs in (("base_equal",group),("changed_equal",changed)):
            info[key]=len(rs)>1 and all(r["rc"]==0 and
                all(r["files"].get(f)==rs[0]["files"].get(f) is not None for f in ("messages.fct","actions.dag")) for r in rs)
        result[base.name]=info
    report["ai"]=result
pfc=root/"cuda-diagnostics/pfc"
inputs={pool:pfc/pool for pool in ("06","64")}
if not (inputs["64"]/"checked/run.log").is_file():
    inputs["64"]=root/"cuda-pfc64/64"
if all((folder/mode/"run.log").is_file() for folder in inputs.values() for mode in ("checked","serial")):
    result={}
    for pool,base in inputs.items():
        arms={}
        for mode in ("checked","serial"):
            folder=base/mode
            s=collections.defaultdict(collections.Counter)
            for line in (folder/"run.log").open():
                if not line.startswith("pfc_work "): continue
                _,node,reason,ports,slots,ticks,tx,arrivals,*eligible=line.split()
                reason,ports,slots,ticks,tx,arrivals=map(int,(reason,ports,slots,ticks,tx,arrivals))
                if not ports and not arrivals: continue
                group=s[node]
                group.update(active=slots,cycles=ticks,tx_frames=tx,arrivals=arrivals,
                             tx_ports=ports*slots,multi_tx_slots=slots if ports>=2 else 0)
                for name,bit in (("parallel",1),("pause_headroom",2),("bound",4),("layout",8),("incoming_pause",16)):
                    if reason&bit: group[name+"_slots"]+=slots
                if not reason&1:
                    group.update(serial_cycles=ticks,serial_slots=slots,serial_multi_tx_slots=slots if ports>=2 else 0,
                                 serial_departures_only_unpaused=int(eligible[0]) if eligible else 0)
            arms[mode]=dict(s)
        result[pool]=dict(runs=arms, outputs_equal=all(hash_file(base/"checked"/f)==hash_file(base/"serial"/f)
                         for f in ("messages.fct","actions.dag")))
    report["pfc"]=result
(root/"performance.json").write_text(json.dumps(report,indent=2)+"\n")
for name,r in report.items():
    print(name,list(r))
