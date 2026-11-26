"""Replay the predeclared packet inputs; use the sealed manifest for case selection."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

p=argparse.ArgumentParser(description=__doc__)
p.add_argument("mode",choices=["native","hip","cuda"])
for name in ("inputs","irn_input","tcp","rdma","output"):
    p.add_argument(name,type=lambda s:Path(s).resolve())
p.add_argument("--native-rdma-library")
p.add_argument("--reserved",action="store_true")
p.add_argument("--engine",choices=["tcp","irn"])
p.add_argument("--decimal-time-guard",action="store_true")
a=p.parse_args()
a.output.mkdir()
for line in (a.inputs/"sealed.sha256").read_text().splitlines():
    expected,path=line.split(maxsplit=1)
    assert hashlib.sha256((a.inputs/path).read_bytes()).hexdigest()==expected, path
cases=[case for case in json.loads((a.inputs/"scenarios.json").read_text())
       if case["engine"]!="closed" and bool(case["reserved"])==a.reserved
       and (a.engine is None or case["engine"]==a.engine)]
if a.mode == "native" and any(case["engine"]=="irn" for case in cases):
    if not a.native_rdma_library or not (a.irn_input/"leaf_spine_128_100G_OS2.txt").is_file():
        p.error("native IRN requires the frozen topology and libraries")

def execute(case):
    label=case["case"]
    d=a.output/label
    d.mkdir()
    payload=a.inputs/label
    env=os.environ.copy()
    if case["engine"]=="tcp":
        cc=case["protocol"]
        trace_to=case["cutoff_ns"]*1000
        if a.mode=="native":
            protocol={"dctcp":"Dctcp","newreno":"NewReno","cubic":"Cubic"}[cc]
            env["TRACE_ALLQ"]=str(trace_to)
            cmd=[str(a.tcp),f"--tcp=ns3::Tcp{protocol}","--threads=0","--topo=file","--hostq=red",
                 f"--stop={case['cutoff_ns']/1e9}",f"--flowEcmp={1-case['spray']}"]
        else:
            cmd=[str(a.tcp),f"--tcp={cc}","--topo=graph","--graph_k=16","--drain",
                 f"--max_slots={trace_to//500000}",f"--spray={case['spray']}","--qtrace=queue.txt",
                 f"--qtrace_until={trace_to-1}"]
        cmd += [f"--topofile={payload/'topology'}",f"--flows={payload/'flows.csv'}",
                f"--queue={case['queue']}","--ecnK=30",f"--tcp-sack={case['sack']}",
                f"--tcp-timestamps={case['timestamps']}","--RngSeed=1",f"--RngRun={case['seed']}","--out=flows.csv"]
    else:
        end=case["cutoff_ns"]
        low=case["rto_ns"]
        if a.mode=="native":
            topo=a.irn_input/"leaf_spine_128_100G_OS2.txt"
            text=(a.irn_input/"irn.cfg").read_text()
            text=re.sub(r"(?m)^TOPOLOGY_FILE .*",f"TOPOLOGY_FILE {topo}",text)
            ref_flows=payload/"ref.flows"
            if a.decimal_time_guard:
                # The author parses seconds as binary double and truncates the
                # result to native ns. Use the interior of the intended ns,
                # keeping its timestamp at the simulator's integer resolution.
                first,*rest=ref_flows.read_text().splitlines()
                ref_flows=d/"native.flows"
                with ref_flows.open("w") as stream:
                    print(first,file=stream)
                    for line in rest:
                        *fields,seconds=line.split()
                        print(*fields,Decimal(seconds)+Decimal("0.00000000025"),file=stream)
            text=re.sub(r"(?m)^FLOW_FILE .*",f"FLOW_FILE {ref_flows}",text)
            (d/"config").write_text(text)
            env.update(LD_LIBRARY_PATH=a.native_rdma_library,TWINS_IRN_LOW_NS=str(low),TWINS_TXTRACE="wire.raw",
                       TWINS_TXTRACE_MAX_NS=str(end-1))
            cmd=[str(a.rdma),"config"]
        else:
            cmd=[str(a.rdma),str(a.irn_input/"irn.cfg"),f"--topo={a.irn_input/'irn.graph'}",
                 f"--flows={payload/'flows.csv'}","--out=flows.fct","--txtrace=wire.raw",f"--until={end-1}",
                 f"--stop-ns={end}",f"IRN_RTO_LOW_NS={low}","IRN_RTO_HIGH_NS=320000","IRN_BDP=104000",
                 "--fp-bases=1000003,1000033"]
    with (d/"run.log").open("wb") as log:
        try:
            rc=subprocess.run(cmd,env=env,cwd=d,stdout=log,stderr=subprocess.STDOUT,timeout=100).returncode
        except subprocess.TimeoutExpired:
            rc=124
    if a.mode=="native" and case["engine"]=="tcp":
        with (d/"run.log").open("rb") as data:
            items=[line[2:] for line in data if line.startswith(b"Q ")]
        items.sort(key=lambda v:tuple(map(int,v.split(maxsplit=3)[:3])))
        (d/"queue.txt").write_bytes(b"".join(items))
    result=dict(**case,rc=rc,command=cmd)
    (d/"receipt.json").write_text(json.dumps(result,indent=2)+"\n")
    print(label,rc,flush=True)
    return result

with ThreadPoolExecutor(max_workers=6 if a.mode=="native" else 1) as pool:
    result=list(pool.map(execute,cases))
(a.output/"summary.json").write_text(json.dumps(result,indent=2)+"\n")
