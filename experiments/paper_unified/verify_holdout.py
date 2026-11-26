"""Compare packet holdout runs with independent sequential traces."""
import argparse
from collections import defaultdict
import csv
from itertools import zip_longest
import json
from pathlib import Path

p=argparse.ArgumentParser(description=__doc__)
p.add_argument("manifest",type=Path)
p.add_argument("native_tcp",type=Path)
p.add_argument("native_irn",type=Path)
p.add_argument("candidate",type=Path)
p.add_argument("--reserved",action="store_true")
a=p.parse_args()
items=[]

def fct(file):
    with file.open() as f:
        r=csv.reader(f)
        next(r)
        return [row[:6] for row in r]

def first(a,b):
    for n,(x,y) in enumerate(zip_longest(a,b),1):
        if x!=y:
            return [n,x,y]
    return None

def port_seq(file, cutoff):
    ports=defaultdict(list)
    for row in file.read_text().splitlines():
        parts=row.split()
        if int(parts[0]) < cutoff:
            ports[tuple(parts[1:3])].append(parts)
    return dict(ports)

for r in json.loads(a.manifest.read_text()):
    if r["engine"]=="closed" or bool(r["reserved"])!=a.reserved:
        continue
    obs=a.candidate/r["case"]
    is_tcp=r["engine"]=="tcp"
    ref=(a.native_tcp if is_tcp else a.native_irn)/r["case"]
    if is_tcp:
        x,y=fct(ref/"flows.csv"),fct(obs/"flows.csv")
        p0=(ref/"queue.txt").read_text().splitlines()
        p1=(obs/"queue.txt").read_text().splitlines()
        wire=first(p0,p1)
    else:
        x=[(i,row[5],row[6]) for i,row in enumerate(sorted((list(map(int,row.split())) for row in
              (ref/"reference.fct").read_text().splitlines()),key=lambda x:x[5])) if row[5]+row[6]<r["cutoff_ns"]]
        y=[tuple(map(int,row.split())) for row in (obs/"flows.fct").read_text().splitlines()]
        p0=port_seq(ref/"wire.raw",r["cutoff_ns"])
        p1=port_seq(obs/"wire.raw",10**30)
        wire=next(({"port":k,"first":first(p0.get(k,[]),p1.get(k,[]))}
                   for k in sorted(p0.keys()|p1.keys()) if p0.get(k)!=p1.get(k)),None)
    case=dict(case=r["case"], fct_matched=x==y, reference_completed=sum(z[-1]!="-1" for z in x),
              first_flow_difference=first(x,y), wire_matched=wire is None, first_wire_difference=wire,
              rc=json.loads((obs/"receipt.json").read_text())["rc"])
    items.append(case)
(a.candidate/"acceptance.json").write_text(json.dumps(items,indent=2)+"\n")
print(sum(r["fct_matched"] and r["wire_matched"] for r in items),"/",len(items),"matched")
raise SystemExit(int(any(not (r["fct_matched"] and r["wire_matched"]) for r in items)))
