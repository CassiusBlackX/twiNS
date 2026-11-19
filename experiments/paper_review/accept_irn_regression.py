"""Accept the shortened 512-flow reproducer only if the baseline diverges and the fix agrees."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"paper_scale"))
from scientific_summary import irn_case
from first_wire import compare

parser=argparse.ArgumentParser()
for name in ("reference","baseline","fixed"):parser.add_argument(name,type=Path)
args=parser.parse_args()


def verify(path):
    r=json.loads((path/"receipt.json").read_text())
    if r["rc"] or r["last_start_ns"]!=2_000_600_000:
        raise ValueError(f"failed or differently truncated run: {path}")
    return r


a,b,c=map(verify,(args.reference,args.baseline,args.fixed))
wire=args.reference/"reference.tx"
original=compare(wire,args.baseline/"gpu.tx",None)
fixed=compare(wire,args.fixed/"gpu.tx",None)
flow=irn_case(args.reference/"flows.csv",args.reference,args.fixed)
assert original["first_observed_wire_difference_ns"]==2_000_651_058
assert fixed["first_observed_wire_difference_ns"] is None and fixed["equal_records"]>800_000
assert flow["input_flows"]==512 and flow["accepted"]
print(json.dumps(dict(passed=True,old_wire=original,new_wire=fixed,corrected_flow=flow),indent=2))
