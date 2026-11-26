"""Compare frozen legal packet traces and common MMU slot boundaries."""
import argparse
from collections import defaultdict
import json
from pathlib import Path

p=argparse.ArgumentParser(description=__doc__)
p.add_argument("observed",type=Path)
p.add_argument("reference",type=Path)
args=p.parse_args()

def ports(path):
    out=defaultdict(list)
    with path.open("rb") as f:
        for line in f:
            parts=line.split()
            out[tuple(parts[1:3])].append(parts)
    return out

def boundaries(path):
    data={}
    with path.open() as f:
        for line in f:
            if not line.startswith("mmu_boundary "):
                continue
            fields=list(map(int,line.split()[1:]))
            key=tuple(fields[:3])
            if key in data:
                raise ValueError(f"{path}: duplicated boundary {key}")
            data[key]=fields[3:]
    return data

def fct(file):
    return sorted(tuple(map(int,line.split())) for line in file.read_text().splitlines())

results=[]
for c in sorted(args.observed.iterdir()):
    ref=args.reference/c.name/"reference"
    if not c.is_dir() or not ref.is_dir():
        continue
    gold=ports(ref/"tx")
    state=boundaries(ref/"boundaries")
    last=max(k[0] for k in state)
    for mode in sorted(c.iterdir()):
        observed=ports(mode/"tx")
        ms=boundaries(mode/"process.log")
        common=ms.keys() & state.keys()
        differences=[key for key in sorted(common) if state[key][:6]!=ms[key][:6]]
        record=dict(case=c.name,mode=mode.name,rc=int((mode/"rc").read_text()),
                    fct_equal=fct(mode/"fct")==fct(ref/"fct"), wire_equal=observed==gold,
                    wire_rows=sum(map(len,gold.values())), comparable=len(common),
                    boundaries_different=len(differences), first_difference=differences[:1],
                    missing=sum(k[0]<=last for k in ms.keys()-state.keys()),
                    checked=sum(v[6]==1 for v in ms.values()), serial=sum(v[6]==0 for v in ms.values()),
                    multislot=sum(v[7]>1 for v in ms.values()))
        results.append(record)
(args.observed/"acceptance.json").write_text(json.dumps(results,indent=2)+"\n")
assert len(results)==24
assert all(r["rc"] in (0,2) and r["fct_equal"] and r["wire_equal"] and r["comparable"]
           and not r["boundaries_different"] and not r["missing"] for r in results)
print(f"{len(results)} reachable MMU/port-state rechecks passed")
