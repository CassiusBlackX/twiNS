"""Compare frozen RDMA flow records, ordered link traces and terminal counters."""
import argparse
import json
from pathlib import Path
import re

p=argparse.ArgumentParser(description=__doc__)
p.add_argument("results",type=lambda value:Path(value).resolve())
a=p.parse_args()
root=a.results

def fct(path):
    if not path.is_file():
        return None
    return sorted(tuple(map(int,row.split())) for row in path.open())

def wire(path, ports=None):
    if not path.is_file():
        return None
    rows=[]
    with path.open() as stream:
        for line in stream:
            t,node,dev,*packet=line.split()
            if ports is None or (node,dev) in ports:
                rows.append((int(t),int(node),int(dev),tuple(packet)))
    # Sorting by owning queue keeps original order within each same-time queue.
    return sorted(rows,key=lambda row:row[:3])

def terminal(log):
    keys=("ties","lost","tx","queue","ackq","qps","due")
    text=log.read_text()
    count=re.findall(r"undecided ties (\d+), on lost tails (\d+) \| overflows: tx (\d+) queue (\d+) ackq (\d+) qps (\d+) due (\d+)",text)
    return dict(zip(keys,map(int,count[-1]))) if count else None

report={}
native=root/"native/dcqcn"
if (native/"summary.json").exists():
    jobs=json.loads((native/"summary.json").read_text())
    for backend,path in (("hip",root/"hip-dcqcn"),("cuda",root/"cuda-rdma/dcqcn")):
        if not (path/"summary.json").exists():
            continue
        rows={}
        for key,job in jobs.items():
            expect=native/key
            found=path/key
            af,bf=fct(expect/"output.fct"),fct(found/"output.fct")
            aw,bw=wire(expect/"wire.raw"),wire(found/"wire.raw")
            flags=terminal(found/"run.log")
            receipt=json.loads((found/"receipt.json").read_text())
            row=dict(native_fct_frozen=(job["sha256"]==job["frozen"]["reference_sha256"]),
                     matched_fct=af==bf and af is not None, matched_wire=aw==bw and aw is not None,
                     flows=len(af),wire_records=len(aw),flags=flags,rc=receipt["rc"])
            row["accepted"]=row["native_fct_frozen"] and row["matched_fct"] and row["matched_wire"] and int(row["rc"])==0 and flags is not None and not any(flags.values())
            rows[key]=row
        report[backend]=rows
scale=root/"cuda-rdma/scale"
strong=root/"native/strong"
if (scale/"strong-2/receipt.json").exists():
    ports={("16000","1"),("16000","21")}
    aw=wire(strong/"wire.raw",ports)
    hit=scale/"strong-2"
    report["scale"]={"fct_equal":fct(strong/"output.fct")==fct(hit/"output.fct"),
                     "wire_equal":aw==wire(hit/"wire.raw"),"wire_records":len(aw),
                     "flags":terminal(hit/"run.log"),"timings":{}}
    for folder in scale.iterdir():
        receipt=folder/"receipt.json"
        if receipt.exists(): report["scale"]["timings"][folder.name]=json.loads(receipt.read_text())
nlate=root/"native/late-final"
for name,path in (("late_hip",root/"hip-late-v3"),("late_cuda",root/"cuda-diagnostics/late")):
    if not (path/"exit-status").exists() or not (nlate/"exit-status").exists():
        continue
    rows={}
    for test in ("control","before","equal-before","equal-after","after"):
        af,bf=fct(nlate/f"{test}.fct"),fct(path/f"{test}.fct")
        aw,bw=wire(nlate/f"{test}.wire"),wire(path/f"{test}.wire")
        rows[test]=dict(fct_equal=af is not None and af==bf,wire_equal=aw is not None and aw==bw,nacks=sum(row[3][0]=="N" and row[1]==1 for row in aw),records=len(aw),flags=terminal(path/f"{test}.log"))
    report[name]=rows
(root/"acceptance.json").write_text(json.dumps(report,indent=2)+"\n")
for key,val in report.items():
    if key in ("hip","cuda"):
        print(key,sum(row["accepted"] for row in val.values()),"of",len(val),sum(row["wire_records"] for row in val.values()),"wire records")
    elif key.startswith("late"):
        print(key,{k:(r["fct_equal"],r["wire_equal"],r["nacks"]) for k,r in val.items()})
    else:
        print(key,{k:x for k,x in val.items() if k!="timings"})
