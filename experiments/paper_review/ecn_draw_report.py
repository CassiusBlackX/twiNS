"""Check an explicit original stream allocator against two independent reference repeats."""
from collections import Counter
import json
from pathlib import Path
import sys

root=Path(sys.argv[1])


def load(name):
    d=root/f"hpcc-v2-{name}"
    if (d/"rc").read_text().strip()!="0":
        raise ValueError(f"failed run {name}")
    draws=[line.split()[1:] for line in (d/"run.log").read_text().splitlines() if line.startswith("ecn-draw ")]
    if not draws:
        raise ValueError(f"no draws {name}")
    return dict(draws=draws, flows=sorted((d/"flows").read_text().splitlines()),
                wire=(d/"wire").read_bytes())


a,b,explicit,ports=map(load,("original-a","original-b","explicit","ports"))
rows=a["draws"]
switches={int(r[2]) for r in rows}
allocations=[int(r[1]) for r in rows]
assert len(switches)>1 and all(x<y for x,y in zip(allocations,allocations[1:]))
result=dict(draws=len(rows), marking_switches=sorted(switches),
            stream_index_first=allocations[0], stream_index_last=allocations[-1],
            original_repeats_equal=a==b, explicit_global_equal=a==explicit,
            per_port_same_fct=a["flows"]==ports["flows"], per_port_same_wire=a["wire"]==ports["wire"],
            marks={name:sum(int(r[-1]) for r in d["draws"])
                   for name,d in (("original",a),("per_port",ports))})
print(json.dumps(result,indent=2))
if not (result["original_repeats_equal"] and result["explicit_global_equal"]):
    raise SystemExit(1)
