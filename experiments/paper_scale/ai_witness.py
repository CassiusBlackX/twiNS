"""Independent, small wire diagnostic for the paired two-stage workload."""
import argparse,json,os,shutil,subprocess,time
from pathlib import Path
p=argparse.ArgumentParser()
for name in ('inputs','out','exe','side'):p.add_argument(name)
a=p.parse_args()
inputs=Path(a.inputs).resolve()
out=Path(a.out).resolve();out.mkdir(parents=True,exist_ok=True)
for file in ('fabric.txt','simai.topo'):shutil.copyfile(inputs/file,out/file)
for case in ('large-buffer','small-buffer'):
    d=out/case;d.mkdir(exist_ok=True)
    for name in ('config','workload.txt','ref.flows'):shutil.copyfile(inputs/case/name,d/name)
    # Capture the physical NIC of every host so either direction of a QP is available.
    (d/'ports').write_text(''.join(f'{node} 1\n' for node in range(16)))
    env={**os.environ,'TWINS_TRACE_PORTS':str(d/'ports'),'AI_TOPO':'../simai.topo','AI_WORKLOAD':'workload.txt','IRN_BDP_BYTES':'104000'}
    cmd=[str(Path(a.exe).resolve()),'config']
    if a.side=='reference':env['TWINS_TXTRACE']='reference.tx'
    else:cmd+=['--topo=../simai.topo','--astra=workload.txt','--out=gpu.fct','--dag-out=gpu.dag','--txtrace=gpu.tx','--q3cap=65536','IRN_BDP=104000']
    with (d/(a.side+'.log')).open('w') as f:
        rc=subprocess.run(cmd,cwd=d,env=env,stdout=f,stderr=subprocess.STDOUT,timeout=900).returncode
    (d/(a.side+'.rc')).write_text(str(rc))
    if rc:raise SystemExit(rc)
