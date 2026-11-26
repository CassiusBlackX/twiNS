"""Run a small real SimAI workload with unchanged entry-logged native callbacks."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import re
import subprocess
p=argparse.ArgumentParser(description=__doc__)
for key in ('binary','inputs','out'):
 p.add_argument(key,type=lambda x:Path(x).resolve())
a=p.parse_args();a.out.mkdir()
def run(inp):
 out=a.out/inp.name;out.mkdir()
 config=(inp/'config.log').read_text()
 config=re.sub(r'/etc/[^\s]+/',str(out)+'/',config)
 (out/'config').write_text(config)
 with (out/'run.log').open('wb') as log:
  env={**os.environ,'AS_SEND_LAT':'3','TWINS_TRACE':str(out/'events.trace')}
  r=subprocess.run([a.binary,'-t','1','-w',inp/'simai-workload','-n',inp/'topo.log','-c',out/'config'],cwd=out,env=env,stdout=log,stderr=log,timeout=45)
 (out/'rc').write_text(str(r.returncode)+'\n')
 if (out/'events.trace').exists():
  (out/'callbacks.txt').write_text(''.join('C '+l[2:] for l in (out/'events.trace').read_text().splitlines(True) if l.startswith('B ')))
 print(inp.name,r.returncode,flush=True)
with ThreadPoolExecutor(max_workers=5) as pool:list(pool.map(run,sorted(a.inputs.iterdir())))
