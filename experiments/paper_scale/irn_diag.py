"""Sampling coordinates are frozen in the campaign manifest (device indices start at one)."""
import argparse,json,os,subprocess,time
from pathlib import Path
p=argparse.ArgumentParser()
for n in ('root','exe','kind'):p.add_argument(n)
p.add_argument('--out',type=Path)
p.add_argument('--hosts',type=int)
p.add_argument('--mode')
a=p.parse_args();root=Path(a.root).resolve();exe=Path(a.exe).resolve()
manifest=json.loads((root/'inputs/manifest.json').read_text())
for h,m in [(256,'incast'),(1024,'spread'),(1024,'incast')]:
    if (a.hosts and a.hosts!=h) or (a.mode and a.mode!=m):continue
    s=next(x for x in manifest['sizes'] if x['hosts']==h);src=root/'inputs'/str(h)
    name=f'irn-{m}-{h}';d=(a.out.resolve() if a.out else root/'trace')/name;d.mkdir(parents=True,exist_ok=True)
    ports=d/'ports';ports.write_text(''.join(f'{n} {dev}\n' for n,dev in s['sample_tx_ports']))
    env={**os.environ,'TWINS_TRACE_PORTS':str(ports),'IRN_BDP_BYTES':'156000'}
    if a.kind=='cpu':
        (d/'config').write_text((src/m/'config').read_text().replace('../fabric.txt',str(src/'fabric.txt')).replace('FLOW_FILE ref.flows','FLOW_FILE '+str(src/m/'ref.flows')))
        env['TWINS_TXTRACE']='reference.tx';cmd=[exe,'config']
    else:
        env['TWINS_SAMPLE_FIRST_SLOT']='2000000';cmd=[exe,src/m/'config',f'--topo={src}/uniform.graph',f'--flows={src}/{m}/flows.csv','IRN_BDP=156000','--out=gpu.fct','--txtrace=gpu.tx']
    with (d/(a.kind+'.log')).open('w') as f:
        start=time.monotonic_ns()
        rc=subprocess.run(cmd,cwd=d,env=env,stdout=f,stderr=subprocess.STDOUT,timeout=7200).returncode
    (d/'receipt.json').write_text(json.dumps(dict(rc=rc,wall_ns=time.monotonic_ns()-start,command=list(map(str,cmd)),ports=s['sample_tx_ports'])))

    if rc:raise SystemExit(rc)
