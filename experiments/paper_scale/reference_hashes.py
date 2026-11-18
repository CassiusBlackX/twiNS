"""Hash the six ordered integer fields used in CPU/GPU acceptance."""
import argparse
import hashlib
import json
from pathlib import Path
from ecn import fields

def signature(path):
    h=hashlib.sha256()
    for row in fields(path):
        h.update((','.join(map(str,row))+'\n').encode())
    return h.hexdigest()

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('root',type=Path);p.add_argument('output',type=Path);a=p.parse_args()
    rows={str(path.relative_to(a.root)):signature(path) for pattern in ('receipts/*-o0-*/flows.csv','cpu-scale/uniform/*/reference.fct','cpu-scale/heterogeneous/*/reference.fct') for path in a.root.glob(pattern)}
    if len(rows)!=88:raise ValueError(len(rows))
    a.output.write_text(json.dumps(rows,indent=2,sort_keys=True))
