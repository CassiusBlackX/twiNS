"""Compare the four sealed IRN holdouts under both batch line orders."""
import argparse,json,subprocess
from collections import defaultdict
from pathlib import Path
p=argparse.ArgumentParser(description=__doc__)
for name in ['binary','holdout','config','topology','single','out']:p.add_argument(name,type=lambda s:Path(s).resolve())
a=p.parse_args();a.out.mkdir();cases=[r for r in json.loads((a.holdout/'scenarios.json').read_text())if r['engine']=='irn'];record={}
def port(path):
 d=defaultdict(list)
 for line in path.read_text().splitlines():
  v=line.split();d[tuple(v[1:3])].append(v)
 return dict(d)
for order in ['forward','reverse']:
 d=a.out/order;d.mkdir();seq=cases if order=='forward' else cases[::-1]
 (d/'worlds').write_text(''.join(f"{c['case']} {a.topology} {a.holdout/c['case']/'flows.csv'} SIMULATOR_STOP_TIME={c['cutoff_ns']/1e9:.9f} IRN_RTO_LOW_NS={c['rto_ns']} IRN_RTO_HIGH_NS=320000 IRN_BDP=104000\n"for c in seq))
 cmd=[str(a.binary),str(a.config),'--batch=worlds','--drain','--fp-bases=1000003,1000033','--txtrace=wire.raw','--txtrace-cap=100000']
 with (d/'run.log').open('wb')as log:r=subprocess.run(cmd,cwd=d,stdout=log,stderr=log,timeout=90)
 assert r.returncode==2
 split=defaultdict(list)
 for line in (d/'wire.raw').read_text().splitlines():
  name,*v=line.split();split[name].append(v)
 for c in cases:
  name=c['case'];single=a.single/name
  assert (d/f'{name}.fct').read_text().splitlines()==(single/'flows.fct').read_text().splitlines(),(order,name)
  by_port=defaultdict(list)
  for v in split[name]:by_port[tuple(v[1:3])].append(v)
  assert dict(by_port)==port(single/'wire.raw'),(order,name)
 record[order]=dict(rc=r.returncode,matched=[c['case']for c in cases],command=cmd)
(a.out/'acceptance.json').write_text(json.dumps(record,indent=2)+'\n')
