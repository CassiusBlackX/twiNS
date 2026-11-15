#!/usr/bin/env python3
import csv,hashlib,json,re,subprocess,time
from pathlib import Path
SRC=Path(__file__).resolve().parents[2];DATA=Path('/home/cassius/Work/SpecDES/twiNS');OUT=DATA/'out/paper-evidence-20261116/local/residuals-timer';OUT.mkdir(parents=True,exist_ok=True)
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def rows(p):
 with p.open() as f:r=csv.reader(f);head=next(r);return head,[tuple(v) for v in r]
cases=[]
for tcp in ('newreno','cubic'):
 cases.append((f'k8_a2a50s_{tcp}_fin',['--k=8','--flows=data/flows/k8_a2a50s.csv','--tcp='+tcp,'--fin=1'],DATA/('data/ns3/ref/k8_a2a50s_Tcp'+('NewReno' if tcp=='newreno' else 'Cubic')+'_fin.csv')))
cases.append(('k16_a2a30_newreno_spray',['--k=16','--flows=data/flows/k16_a2a30.csv','--tcp=newreno','--spray=1'],DATA/'data/ns3/ref/k16_a2a30_TcpNewReno_spray.csv'))
for line in (DATA/'validation/grid/jobs_twins.txt').read_text().splitlines():
 v=line.split()
 if v and v[0] in ('g073','g075'):cases.append((v[0],v[1:],DATA/f'data/ns3/grid/{v[0]}.csv'))
opts=['--k=8']
cases.append(('k8_dm30_newreno',opts+['--flows=data/flows/k8_dm30.csv','--tcp=newreno'],DATA/'data/ns3/ref/k8_dm30_TcpNewReno.csv'))
cases=[x for x in cases if x[0].endswith('_fin') or x[0]=='g075']
caps=[(25,SRC/'build-timer/twins128')]
for runs,bin in caps:
 deadline=time.monotonic()+1800
 while not bin.exists():
  if time.monotonic()>deadline:raise RuntimeError('build not ready: '+str(bin))
  time.sleep(10)
 for n,args,ref in cases:
  d=OUT/f'{n}-runs{runs}';d.mkdir(exist_ok=True)
  if (d/'result.json').exists():continue
  cmd=[str(bin),*args,'--out='+str(d/'flows.csv')];start=time.perf_counter_ns();utc=time.time_ns()
  with (d/'process.log').open('w') as f:
   try:rc=subprocess.run(cmd,cwd=DATA,stdout=f,stderr=subprocess.STDOUT,timeout=3600).returncode
   except subprocess.TimeoutExpired:rc=124
  wall=time.perf_counter_ns()-start;log=(d/'process.log').read_text(errors='replace');valid={}
  try:
   head,a=rows(d/'flows.csv');rh,b=rows(ref)
   valid=dict(rows=len(a),reference_rows=len(b),compared_fields=head[:6],first_six_equal=([v[:6] for v in a]==[v[:6] for v in b]),all_fields_equal=(a==b),sha256=sha(d/'flows.csv'),reference_sha256=sha(ref))
  except Exception as e:valid=dict(error=str(e),first_six_equal=False)
  v=dict(case=n,runs=runs,frame_bytes=24+2*(2*runs+2),command=cmd,cwd=str(DATA),binary_sha256=sha(bin),reference=str(ref),start_utc_ns=utc,wall_ns=wall,rc=rc,timeout=(rc==124),flags=[x for x in log.splitlines() if 'warning:' in x or 'undecided-ties' in x],flagged=('were undecided' in log),overflow=('a capacity overflowed' in log),loop_s=re.findall(r'Wall time: ([\d.]+) s',log),validation=valid)
  (d/'result.json').write_text(json.dumps(v,indent=2)+'\n')
  with (OUT/'attempts.jsonl').open('a') as f:f.write(json.dumps(v)+'\n')
  print(json.dumps({k:v[k] for k in ('case','runs','wall_ns','rc','flagged')}),flush=True)
  # The full output is small for these fixed cases and is retained on the local execution host.
(OUT/'finished.json').write_text(json.dumps(dict(utc_ns=time.time_ns())))
