"""Compare native and GPU handler order only while a sender queue pair is live."""
import argparse
from collections import defaultdict
import csv
import json
from pathlib import Path

p=argparse.ArgumentParser(description=__doc__)
for name in ("offered","native_fct","native_log","gpu_log","output"):
    p.add_argument(name,type=Path)
a=p.parse_args()
with a.offered.open() as stream:
    flows=list(csv.DictReader(stream))
pairs=defaultdict(lambda:10000)
keys={}
for f in sorted(range(len(flows)),key=lambda f:int(flows[f]["start_ns"])):
    row=flows[f]
    pair=int(row["src"]),int(row["dst"])
    keys[f]=(*pair,pairs[pair])
    pairs[pair]+=1
close={}
for line in a.native_fct.open():
    f,start,fct=map(int,line.split())
    close[keys[f]]=start+fct

def events(path):
    history=defaultdict(list)
    skipped=0
    for line in path.open():
        cols=line.split()
        if len(cols)!=9 or cols[0]!="DCQCN" or not cols[1].isdigit():
            continue
        t,src,dst,port=map(int,cols[1:5])
        key=(src,dst,port)
        if t>=close[key]:
            skipped+=1
            continue
        history[t,*key].append(cols[5:])
    return dict(history),skipped

native,native_after=events(a.native_log)
gpu,gpu_after=events(a.gpu_log)
different=[]
for key in sorted(native.keys() | gpu.keys()):
    if native.get(key)!=gpu.get(key):
        different.append(dict(key=key,native=native.get(key),gpu=gpu.get(key)))
same_time=[]
for key,kinds in native.items():
    if len(kinds)>1 and any(row[0]=="cnp" for row in kinds) and any(row[0]!="cnp" for row in kinds):
        same_time.append(dict(key=key,native=kinds,gpu=gpu.get(key)))
row=dict(accepted=bool(native) and not different,native_live=sum(map(len,native.values())),
         gpu_live=sum(map(len,gpu.values())),native_at_or_after_close=native_after,
         gpu_at_or_after_close=gpu_after,same_time=same_time,
         disagreements=len(different),first_disagreements=different[:10])
a.output.write_text(json.dumps(row,indent=2)+"\n")
print(json.dumps({k:v for k,v in row.items() if k not in ("same_time","first_disagreements")}))
raise SystemExit(0 if row["accepted"] else 1)
