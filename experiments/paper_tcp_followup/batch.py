"""Check that capacity and history retries preserve unaffected worlds."""
import argparse
import json
from pathlib import Path
import re
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"paper_tcp_options"))
from campaign import clean, invoke, signature

p=argparse.ArgumentParser(description=__doc__)
for name in ("binary","driver","inputs","expected","references","output"):
    p.add_argument(name,type=lambda value:Path(value).resolve())
a=p.parse_args()
expected=json.loads(a.expected.read_text())
references=json.loads(a.references.read_text())
a.output.mkdir(parents=True,exist_ok=False)
report={}
for case in ("callbacks","active","options"):
    folder=a.output/case
    worlds=a.output/(case+".worlds")
    outputs=[]
    if case=="callbacks":
        settings=[("healthy.csv",1,1,"healthy"),("forty.csv",1,1,"connections")]
        args=["--topo=graph",f"--topofile={a.inputs/'two-paths.graph'}","--graph_k=3","--queue=200",
              "--max_slots=96000000","--fragment-timers=1","--drain"]
    elif case=="active":
        settings=[("empty.csv",1,1,"empty"),("capacity.csv",1,1,"dense")]
        args=["--k=4","--queue=1024","--max_slots=300000"]
    else:
        settings=[("packet.csv",sack,ts,arm) for arm,sack,ts in (("off",0,0),("sack",1,0),("timestamps",0,1),("both",1,1))]
        args=["--k=8","--queue=200","--spray=1"]
    lines=[]
    for i,(flow,sack,ts,key) in enumerate(settings):
        file=a.output/f"{case}-{i}.csv"
        outputs.append((file,key))
        lines.append(f"flows={a.inputs/flow},tcp=newreno,tcp-sack={sack},tcp-timestamps={ts},out={file}\n")
    worlds.write_text("".join(lines))
    row=invoke([sys.executable,str(a.driver),str(a.binary/"twins"),*args,f"--worlds={worlds}"],folder)
    found={key:signature(file) if file.is_file() else None for file,key in outputs}
    if case=="options": gold={arm:references[f"packet-ecmp/{arm}"]["flows"] for _,arm in outputs}
    else: gold={key:expected[key] for _,key in outputs}
    passes=[int(n) for n in re.findall(r"^TCP pass .* worlds=(\d+)", (folder/"run.log").read_text(),re.M)]
    scoped=bool(passes) and passes[0]==len(settings) and all(n==1 for n in passes[1:]) if case!="options" else bool(passes)
    row.update(found=found,expected=gold,passes_worlds=passes,scoped=scoped)
    row["accepted"]=row["rc"]==0 and found==gold and clean(folder) and scoped
    report[case]=row
    print(case,row["accepted"],passes,flush=True)
    (a.output/"acceptance.json").write_text(json.dumps(report,indent=2)+"\n")
raise SystemExit(0 if all(row["accepted"] for row in report.values()) else 1)
