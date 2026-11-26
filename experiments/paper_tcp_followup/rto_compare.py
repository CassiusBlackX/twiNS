"""Match each native/GPU RTO by connection (including source port), exact time and TCP state."""
import argparse
from collections import defaultdict
import csv
import json
from pathlib import Path
import re

p=argparse.ArgumentParser(description=__doc__)
for name in ("offered","native","gpu","output"):
    p.add_argument(name,type=Path)
a=p.parse_args()
flows=list(csv.DictReader(a.offered.open()))
hosts=defaultdict(list)
for f,row in enumerate(flows): hosts[int(row["src"])].append(f)
ports={}
endpoint={}
for src,ids in hosts.items():
    for rank,f in enumerate(sorted(ids,key=lambda f:(int(flows[f]["start_ns"]),f))):
        port=49153+rank
        ports[f]=port
        endpoint[(src,int(flows[f]["dst"]),port)]=f
native=[]
gpu=[]
for line in a.native.open():
    if line.startswith("NATIVE_TCP_RTO "):
        t,src,local,peer,port,state,una,high=map(int,line.split()[1:])
        assert local==0x0a000001|(src<<8)
        dst=(peer-0x0a000001)//256
        f=endpoint[(src,dst,port)]
        native.append((t,f,src,dst,port,state,una,high))
for line in a.gpu.open():
    if line.startswith("TCP_RTO "):
        t,f,src,dst,state,una,high=map(int,line.split()[1:])
        assert (src,dst)==(int(flows[f]["src"]),int(flows[f]["dst"]))
        gpu.append((t,f,src,dst,ports[f],state,una,high))
report=dict(accepted=bool(native) and native==gpu,native_expirations=len(native),gpu_expirations=len(gpu),
            distinct_connections=len({row[1] for row in native}),
            already_in_loss=[row for row in native if row[5]==4],
            columns=["time_ps","connection","src","dst","source_port","congestion_state","una","high"],
            events=native)
a.output.write_text(json.dumps(report,indent=2)+"\n")
print(json.dumps({key:value for key,value in report.items() if key!="events"},indent=2))
raise SystemExit(0 if report["accepted"] else 1)
