"""Verify returned raw outputs for queue recovery, buffer edges and IRN stream safety."""
from collections import defaultdict, Counter
import hashlib
import json
from pathlib import Path
import re

root=Path(__file__).resolve().parents[2]
r=root/'out/unified-review'
result={}
def rows(path):return sorted(tuple(map(int,x.split())) for x in path.read_text().splitlines())
def port(path):
 out=defaultdict(list)
 for line in path.read_bytes().splitlines():
  v=line.split();out[tuple(v[1:3])].append(tuple(v))
 return dict(out)
def clean_rdma(log):
 flags=re.findall(r'undecided ties (\d+), on lost tails (\d+) \| overflows: tx (\d+) queue (\d+) ackq (\d+) qps (\d+) due (\d+)',log)
 return bool(flags) and not any(map(int,flags[-1]))
# A simulator capacity failure must never stand in for a model drop.
ref=root/'out/full-review-20261126/native/strong'
want=rows(ref/'output.fct');trace=port(ref/'selected-wire.log');assert len(want)==320 and sum(map(len,trace.values()))==31396
rec=[]
for a in json.loads((r/'queue-cuda/summary.json').read_text()):
 d=r/'queue-cuda'/f"{a['trial']}-{a['case']}"
 if a['case']=='fixed':
  assert a['rc']==3 and a['failures'];continue
 assert a['rc']==0 and rows(d/'output.fct')==want and port(d/'wire.raw')==trace and clean_rdma((d/'run.log').read_text()),d
 rec.append(dict(case=a['case'],trial=a['trial'],flows=len(want),sends=31396,matched=True))
result['strong_queue_cuda']=rec
base=rows(r/'queue-hip/0-preset/output.fct');wire=port(r/'queue-hip/0-preset/wire.raw')
d=r/'queue-hip-adaptive/0-auto'
assert rows(d/'output.fct')==base and port(d/'wire.raw')==wire and clean_rdma((d/'run.log').read_text())
result['adaptive_hip']=dict(matched=True,receipt=json.loads((r/'queue-hip-adaptive/summary.json').read_text()))
# Admission and pause probes start empty and build state with legal packets.
for kind,refdir,archdirs,threshold,modes in [
 ('admission','native-admission',{'hip':'admission-hip','cuda':'admission-cuda'},320,['default','serial']),
 ('pause','native-pause-v3',{'hip':'pause-hip-v3','cuda':'pause-cuda'},284,['normal','serial'])]:
 items=[]
 for arch,folder in archdirs.items():
  for n in [threshold-1,threshold,threshold+1]:
   gold=r/refdir/str(n);refmode='default' if kind=='admission' else 'normal'
   for mode in modes:
    d=r/folder/str(n)
    assert rows(d/f'{mode}.fct')==rows(gold/f'{refmode}.fct') and port(d/f'{mode}.wire')==port(gold/f'{refmode}.wire'),d
    assert int((d/f'{mode}.rc').read_text()) in (0,2)
    log=(d/f'{mode}.log').read_text();assert clean_rdma(log)
    items.append(dict(backend=arch,mode=mode,boundary=n,wire_rows=sum(map(len,port(d/f'{mode}.wire').values())),matched=True,model_counts=re.findall(r'^drops .*',log,re.M)[-1:]))
 result[kind]=items
# Both cases have real losses; ECN case additionally consumes one switch RNG stream.
manifest=json.loads((root/'out/boundary-review/stage/irn-matrix.json').read_text())
items=[]
for arch in ['hip','cuda']:
 folder=r/f'irn-stream-{arch}'
 if not folder.exists():continue
 for kind in ['plain','ecn']:
  gold=next(x['gold'] for x in manifest if x['case']==f'103/{kind}/spread/128')
  serial=folder/kind/'serial';checked=folder/kind/'checked'
  plainrows=rows(serial/'flows.fct')
  digest=hashlib.sha256(''.join(' '.join(map(str,x))+'\n' for x in plainrows).encode()).hexdigest()
  assert digest==gold and rows(checked/'flows.fct')==plainrows
  assert port(serial/'wire.raw')==port(checked/'wire.raw')
  for d in [serial,checked]:
   assert int((d/'rc').read_text())==0 and clean_rdma((d/'run.log').read_text())
  log=(checked/'run.log').read_text();counts=re.search(r'drops (\d+) marks (\d+)',log)
  assert int(counts[1])>0
  if kind=='ecn':assert int(counts[2])>0
  items.append(dict(backend=arch,case=kind,flows=len(plainrows),matched=True,drops=int(counts[1]),marks=int(counts[2]),fct_sha256=digest,slots=re.findall(r'IRN active switch slots: .*',log)))
result['irn_stream']=items
# One preexisting incomplete world is carried through exactly one affected-world retry.
for arch in ['hip','cuda']:
 d=r/f'mixed-single-retry-{arch}'
 if not d.exists():continue
 for order in ['forward','reverse']:
  log=(d/order/'run.log').read_text()
  assert 'RDMA frame retry: 1 of 3 worlds' in log
  for name in ['healthy','stranded','loaded']:
   assert rows(d/name/'output.fct')==rows(d/order/f'{name}.fct')
 result[f'mixed_single_retry_{arch}']=True
(r/'supplement-acceptance.json').write_text(json.dumps(result,indent=2)+'\n')
print('Verified',', '.join(result))
