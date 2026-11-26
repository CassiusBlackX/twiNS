"""Match the native live SimAI fixture against both GPU backends."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

p=argparse.ArgumentParser(description=__doc__)
p.add_argument("out",type=Path)
a=p.parse_args()
a.out=a.out.resolve()
root=Path(__file__).resolve().parents[2]
def fields(path,actions=False):
    with path.open() as stream:
        result=[]
        for row in stream:
            if row.startswith("#"):continue
            row=row.rstrip()
            if actions: row=re.sub(r"^(on [ft][0-9]* m).*",r"\1",row)
            result.append(row)
    return sorted(result)
def wires(path):
    with path.open() as stream:
        return sorted((line.rstrip() for line in stream),key=lambda line:tuple(map(int,line.split()[:3])))
report={}
for case in ("control","before","equal-before","equal-after","after"):
    native=a.out/"simai-native"/case
    dag=native/"normalized.dag"
    fct=native/"normalized.fct"
    with (native/"dag-extract.log").open("wb") as log:
        subprocess.run([sys.executable,root/"workloads/trace2dag.py",native/"events.trace",native/"llama_hpn7_fct.txt",dag,fct],
                       check=True,stdout=log,stderr=log)
    report[case]={}
    wire=wires(native/"wire.raw")
    for side in ("hip","cuda"):
        d=a.out/f"simai-{side}"/case
        if not (d/"rc").exists():continue
        other=wires(d/"wire.raw")
        summary=dict(rc=int((d/"rc").read_text()),fct=fields(fct)==fields(d/"messages.fct"),
                     actions=fields(dag,True)==fields(d/"actions.dag",True),wire=wire==other,wire_rows=len(wire),
                     first_difference=next(((x,y) for x,y in zip(wire,other) if x!=y),None))
        report[case][side]=summary
        print(case,side,summary,flush=True)
(a.out/"simai-acceptance.json").write_text(json.dumps(report,indent=2)+"\n")
