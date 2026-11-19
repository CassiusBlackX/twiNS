"""Characterize the opt-in IRN switch mode only after equality to the same author run."""
import argparse
import json
from pathlib import Path
import re
from statistics import median
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"paper_scale"))
from scientific_summary import irn_case

parser=argparse.ArgumentParser()
parser.add_argument("trials",type=Path)
parser.add_argument("reference",type=Path)
a=parser.parse_args()
result={}
for layout in ("spread","incast"):
    for hosts in (128,1024):
        root=Path(layout)/str(hosts)
        reference=a.reference/"cpu-scale"/root
        flows=a.reference/"inputs"/str(hosts)/layout/"flows.csv"
        entry={}
        for mode in ("serial","parallel"):
            runs=[]
            for i in (1,2,3):
                run=a.trials/"timing"/str(i)/mode/root
                receipt=json.loads((run/"gpu.json").read_text())
                if receipt["rc"]!=0:raise ValueError(f"GPU error in {run}")
                equality=irn_case(flows,reference,run)
                log=(run/"gpu.log").read_text()
                branch=re.search(r"IRN active switch slots: (\d+) parallel, (\d+) fallback",log)
                hits=tuple(map(int,branch.groups())) if branch else None
                runs.append(dict(wall_ns=receipt["wall_ns"],**equality,active_slots=hits))
            times=[r["wall_ns"] for r in runs]
            entry[mode]=dict(trials=runs,passed=all(r["accepted"] for r in runs),
                            median_ns=median(times),range_ns=[min(times),max(times)])
        result[str(root)]=entry
print(json.dumps(result,indent=2))
