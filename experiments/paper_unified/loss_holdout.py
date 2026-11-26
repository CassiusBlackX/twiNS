"""Replay the sealed additional IRN incast, without altering its protocol or input."""
import argparse,hashlib,json,os,re,subprocess
from pathlib import Path
p=argparse.ArgumentParser(description=__doc__);p.add_argument('backend',choices=['native','gpu'])
for name in ['binary','inputs','out']:p.add_argument(name,type=lambda s:Path(s).resolve())
p.add_argument('--libraries');p.add_argument('--native-order',action='store_true');a=p.parse_args();a.out.mkdir()
for line in (a.inputs/'sealed.sha256').read_text().splitlines():
 digest,name=line.split();assert hashlib.sha256((a.inputs/name).read_bytes()).hexdigest()==digest
if a.backend=='native':
 cfg=(a.inputs/'config').read_text()
 # The stock author's driver sets IRN BDP by matching a topology basename.
 # Give the exact frozen bytes their original alias so the BDP is 104000.
 topo=a.out/'leaf_spine_128_100G_OS2.txt'
 topo.write_bytes((a.inputs/'native.topology').read_bytes())
 cfg=re.sub(r'(?m)^TOPOLOGY_FILE .*',f'TOPOLOGY_FILE {topo}',cfg)
 flowfile=a.inputs/'ref.flows'
 if a.native_order:
  # The author streams the next input after scheduling the preceding one;
  # its flow table must be chronological, as the GPU loader already makes it.
  header,*entries=flowfile.read_text().splitlines()
  from decimal import Decimal
  entries.sort(key=lambda line:Decimal(line.split()[-1]))
  flowfile=a.out/'native.flows'
  flowfile.write_text(header+'\n'+'\n'.join(entries)+'\n')
 cfg=re.sub(r'(?m)^FLOW_FILE .*',f'FLOW_FILE {flowfile}',cfg)
 (a.out/'config').write_text(cfg)
 env={**os.environ,'LD_LIBRARY_PATH':a.libraries,'TWINS_IRN_LOW_NS':'100000','TWINS_TXTRACE':'wire.raw','TWINS_TXTRACE_MAX_NS':'2010000000'}
 command=[str(a.binary),'config']
else:
 env=os.environ.copy()
 command=[str(a.binary),str(a.inputs/'config'),f'--topo={a.inputs}/topology.graph',f'--flows={a.inputs}/flows.csv','IRN_RTO_LOW_NS=100000','IRN_RTO_HIGH_NS=320000','IRN_BDP=104000','--fp-bases=1000003,1000033','--out=flows.fct','--txtrace=wire.raw','--txtrace-cap=2000000','--until=2010000000']
with (a.out/'run.log').open('wb')as f:r=subprocess.run(command,cwd=a.out,env=env,stdout=f,stderr=f,timeout=150)
(a.out/'receipt.json').write_text(json.dumps(dict(backend=a.backend,rc=r.returncode,command=command),indent=2)+'\n')
