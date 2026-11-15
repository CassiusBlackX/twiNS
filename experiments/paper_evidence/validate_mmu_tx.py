#!/usr/bin/env python3
"""Exact recorded TX field multiset and per-(node,device) order, not unrecorded state."""
import collections,hashlib,json,os
from pathlib import Path
root=Path(os.environ.get('TWINS_MMU_REPRO_OUT','/home/cassius/Work/SpecDES/twiNS/out/paper-evidence-20261116/local/mmu/boundaries_v3'))
def load(p):
 rows=[tuple(l.split()) for l in p.read_text().splitlines() if l.strip() and not l.startswith('#')]
 if any(len(r)!=10 for r in rows):raise ValueError('expected ten recorded TX fields: '+str(p))
 links=collections.defaultdict(list)
 for r in rows:links[(r[1],r[2])].append(r)
 return rows,links
rows=[]
for p in sorted(root.glob('*/*/result.json')):
 x=json.loads(p.read_text());actual=p.parent/'tx';reference=p.parent.parent/'reference/tx';a,al=load(actual);b,bl=load(reference)
 v=dict(case=x['case'],variant=x['variant'],actual_rows=len(a),reference_rows=len(b),canonical_tx_equal=collections.Counter(a)==collections.Counter(b),per_link_order_equal=dict(al)==dict(bl),actual_sha256=hashlib.sha256(actual.read_bytes()).hexdigest(),reference_sha256=hashlib.sha256(reference.read_bytes()).hexdigest(),fields='ts,node,device,kind,source,dest,sport,sequence,wire_size,ecn')
 rows.append(v);(p.parent/'tx-validation.json').write_text(json.dumps(v,indent=2)+'\n')
(root/'tx-validation.json').write_text(json.dumps(rows,indent=2)+'\n')
print(json.dumps(dict(cases=len(rows),all_equal=bool(rows) and all(x['canonical_tx_equal'] and x['per_link_order_equal'] for x in rows))))
assert rows and all(x['canonical_tx_equal'] and x['per_link_order_equal'] for x in rows)
