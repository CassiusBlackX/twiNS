"""Validate real SimAI equal-lead callback order, creation order, and exclusive horizons."""
import argparse,json,re,subprocess
from collections import defaultdict
from pathlib import Path
p=argparse.ArgumentParser(description=__doc__)
for name in ['native','candidate']:p.add_argument(name,type=Path)
a=p.parse_args();root=Path(__file__).resolve().parents[2];receipts={}
for label,D,witness in [('r200d8',8,85696),('r400d9',9,85491)]:
 ref=a.native/label
 subprocess.run(['python3',str(root.parent/'twiNS/workloads/trace2dag.py'),str(ref/'events.trace'),str(ref/'llama_hpn7_fct.txt'),str(ref/'normalized.dag'),str(ref/'normalized.fct')],check=True,capture_output=True)
 callbacks=[tuple(row.split())for row in (ref/'callbacks.txt').read_text().splitlines()]
 groups=defaultdict(list)
 for row in callbacks:groups[int(row[-1])].append(row)
 raw=[row.split()for row in (ref/'events.trace').read_text().splitlines()]
 delays={row[1]:int(row[2])for row in raw if row[0]=='T'}
 shared=[(ts,seq)for ts,seq in groups.items() if any(row[1]=='t' and delays[row[2]]==10 for row in seq) and any(row[1]=='r' for row in seq)]
 assert shared
 time,seq=shared[0]
 timer=next(i for i,row in enumerate(seq) if row[1]=='t' and delays[row[2]]==10)
 receive=next(i for i,row in enumerate(seq) if row[1]=='r')
 assert (timer<receive)==(label=='r200d8')
 assert all(t in groups for t in [witness-1,witness,witness+1]) and witness%D==0
 receipt=dict(native_callbacks=len(callbacks),equal_lead_ns=10,first_time=time,timer_first=timer<receive,first_group=seq,boundary_ns=witness,modes={})
 for mode in ['regular','one-slot','island']:
  d=a.candidate/label/mode;assert (d/'rc').read_text().strip()=='0',d
  cmd=['python3',str(root/'validation/simai/compare_closed.py'),str(ref/'normalized.fct'),str(d/'messages.fct'),str(ref/'normalized.dag'),str(d/'actions.dag'),'--reference-execution',str(ref/'callbacks.txt'),'--candidate-execution',str(d/'callbacks.txt')]
  response=subprocess.run(cmd,text=True,capture_output=True);assert response.returncode==0,(d,response.stdout,response.stderr)
  comparison=json.loads(response.stdout)
  log=(d/'run.log').read_text();lead=int(re.search(r'(\d+) lead ties',log)[1])
  assert lead>0,(d,'same-lead comparator was not exercised')
  aux=[row.split()for row in (d/'horizons.txt').read_text().splitlines()]
  events=[row for row in aux if row[0]=='C']
  assert [tuple(row[:4])for row in events]==callbacks
  assert all(int(row[3])<int(row[4]) for row in events)
  boundary=[row for row in events if int(row[3])in [witness-1,witness,witness+1]]
  if mode=='one-slot':
   assert any(row[0]=='H' and int(row[1])==witness for row in aux)
   assert all(int(row[4])<=witness if int(row[3])==witness-1 else int(row[4])>witness for row in boundary)
  receipt['modes'][mode]=dict(matched=comparison['matched'],messages=comparison['messages']['reference_count'],lead_comparisons=lead,setup_order_checked=comparison['setup_order_checked'],boundary_callbacks=boundary)
 receipts[label]=receipt
(a.candidate/'acceptance.json').write_text(json.dumps(receipts,indent=2)+'\n')
print('Six real equal-lead/join/relative-root/exclusive-horizon comparisons passed')
