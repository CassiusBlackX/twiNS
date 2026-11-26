"""Report same-host speed and agreement separately from completed flow counts."""
import argparse
import csv
import json
import re
from pathlib import Path
from statistics import median

p=argparse.ArgumentParser(description=__doc__)
for k in ("cpu","gpu","corrected","out"):
    p.add_argument(k,type=Path)
a=p.parse_args()
collect=[]
for root in (a.cpu,a.gpu,a.corrected):
    if not (root/"summary.json").is_file():
        continue
    for row in json.loads((root/"summary.json").read_text()):
        row["folder"]=str(root/f"{row['trial']}-{row['case']}-{row['both']}-{row['engine']}")
        if root==a.corrected:
            collect=[r for r in collect if tuple(r[x] for x in ("trial","case","both","engine")) !=
                     tuple(row[x] for x in ("trial","case","both","engine"))]
        collect.append(row)

def rows(table):
    with table.open() as f:
        r=csv.reader(f)
        next(r)
        return [row[:6] for row in r]

summary={}
for case in sorted(set(r["case"] for r in collect)):
    for arm in (0,1):
        entries=[r for r in collect if r["case"]==case and r["both"]==arm]
        refs=[r for r in entries if r["engine"]=="ns3" and r.get("fct_sha256")]
        if not refs:
            continue
        reference=rows(Path(refs[0]["folder"])/"flows.csv")
        result={}
        for engine in ("ns3","unison32","twins"):
            trials=[r for r in entries if r["engine"]==engine]
            times=[r["wall_ns"]/1e9 for r in trials]
            for r in trials:
                log=(Path(r["folder"])/"run.log").read_text(errors="replace")
                if engine=="twins":
                    loops=re.findall(r"Simulation Duration: ([\d.]+)s",log)
                else:
                    loops=re.findall(r"flows \d+ completed \d+ wall ([\d.]+) s",log)
                r["loop_s"]=float(loops[-1]) if loops else None
                r["process_minus_loop_s"]=(r["wall_ns"]/1e9-r["loop_s"]) if r["loop_s"] is not None else None
                r["attempts"]=re.findall(r"^TCP pass .*",log,re.M)
                r["terminal_counts"]=re.findall(r"^flows \d+ completed .*",log,re.M)[-1:]
                path=Path(r["folder"])/"flows.csv"
                if path.exists():
                    found=rows(path)
                    r["six_field_differences"]=abs(len(reference)-len(found))+sum(x!=y for x,y in zip(reference,found))
            if trials:
                result[engine]=dict(turns=len(trials), wall_s=[min(times),median(times),max(times)], observations=trials)
        summary[f"{case}/{arm}"]=result
a.out.write_text(json.dumps(summary,indent=2)+"\n")
