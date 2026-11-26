"""Check unchanged output and retained window batching for equal aligned deadlines."""
import argparse,json,re,subprocess,time
from pathlib import Path
p=argparse.ArgumentParser(description=__doc__)
for k in ['old','new','inputs','out']:p.add_argument(k,type=lambda x:Path(x).resolve())
a=p.parse_args();a.out.mkdir();records=[];gold=None
for trial in range(3):
 for label,binary in ([('old',a.old),('new',a.new)] if trial%2==0 else [('new',a.new),('old',a.old)]):
  d=a.out/f'{trial}-{label}';d.mkdir()
  (d/'worlds').write_text(''.join(f'{n} {a.inputs}/fattree8.txt {a.inputs}/k8_a2a30.csv CC_MODE=1 SIMULATOR_STOP_TIME=0.02\n' for n in ['a','b']))
  cmd=[str(binary),str(a.inputs/'hpcc.cfg'),'--batch=worlds','--gwin=2','--delay=2000ns','--fp-bases=1000003,1000033']
  start=time.monotonic_ns()
  with (d/'run.log').open('wb') as log:r=subprocess.run(cmd,cwd=d,stdout=log,stderr=log,timeout=60)
  wall=time.monotonic_ns()-start
  values={n:sorted((d/f'{n}.fct').read_text().splitlines()) for n in ['a','b']}
  if gold is None:gold=values
  assert r.returncode==0 and values==gold and values['a']==values['b']
  log=(d/'run.log').read_text();flags=re.findall(r'undecided ties (\d+), on lost tails (\d+) \| overflows: tx (\d+) queue (\d+) ackq (\d+) qps (\d+) due (\d+)',log)
  assert flags and not any(map(int,flags[-1]))
  records.append(dict(trial=trial,version=label,command=cmd,wall_ns=wall,loop_s=re.findall(r'simulated [\d.]+ s in ([\d.]+) s wall',log),flows=len(values['a']),matched=True))
(a.out/'summary.json').write_text(json.dumps(records,indent=2)+'\n')
