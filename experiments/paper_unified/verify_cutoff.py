"""Compare trace completeness, per-world stop and local port order."""
import argparse
from collections import defaultdict
import json
from pathlib import Path

p=argparse.ArgumentParser(description=__doc__)
p.add_argument("results",type=Path)
p.add_argument("reference",type=Path)
a=p.parse_args()
base=a.results
reference=a.reference
native=[row.split() for row in (reference/"wire.raw").read_text().splitlines()]
fct=[list(map(int,line.split())) for line in (reference/"reference.fct").read_text().splitlines()]
cases=[]

def compare(rows, end):
    gold=defaultdict(list)
    got=defaultdict(list)
    for row in native:
        if int(row[0]) < end:
            gold[tuple(row[1:3])].append(row)
    for row in rows:
        got[tuple(row[1:3])].append(row)
    assert got == gold
    return sum(map(len,gold.values()))

for label in json.loads((base/"runs.json").read_text()):
    d=base/label
    rc=int((d/"rc").read_text())
    log=(d/"run.log").read_text()
    if label.startswith("trace-cap"):
        cap=int(label.rsplit("-",1)[1])
        assert len((d/"wire.raw").read_text().splitlines()) == cap
        if cap == 40:
            assert rc==0 and "txtrace complete: 40 records" in log
            compare([row.split() for row in (d/"wire.raw").read_text().splitlines()],2000100000)
        else:
            assert rc==3 and "txtrace incomplete: 40 records, 39 kept" in log
        cases.append(dict(case=label,rc=rc,capacity=cap))
        continue
    assert rc==2 and "txtrace complete:" in log
    mode,first,entry=label.split("-")
    grouped=defaultdict(list)
    if entry=="batch":
        for row in (d/"wire.raw").read_text().splitlines():
            world,*fields=row.split()
            grouped[int(world[1:])].append(fields)
    else:
        grouped[int(first)]=[row.split() for row in (d/"wire.raw").read_text().splitlines()]
    for end_in_ns,rows in grouped.items():
        end=2_000_000_000+end_in_ns
        count=compare(rows,end)
        expected=[f"{i} {r[5]} {r[6]}" for i,r in enumerate(sorted(fct,key=lambda x:x[5])) if r[5]+r[6]<end]
        observed=(d/(f"w{end_in_ns}.fct" if entry=="batch" else "out.fct")).read_text().splitlines()
        assert observed==expected,(label,observed,expected)
        cases.append(dict(case=label,end_ns=end,wire=count,completed=len(observed)))
(base/"acceptance.json").write_text(json.dumps(cases,indent=2)+"\n")
print(f"{len(cases)} cutoff/trace comparisons passed")
