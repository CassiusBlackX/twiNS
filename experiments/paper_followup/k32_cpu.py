"""Repeat the five frozen 8192-host RDMA references with bounded shared admission."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT=Path.home()/'Videos/netsim'
DATA=ROOT/'rdma'
MODEL=ROOT/'hpcc'
CASES=[('a2a30',1),('a2a30',3),('a2a50s',1),('a2a50s',3),('incast16',1)]

def sha(path):
    with path.open('rb') as f:
        return hashlib.file_digest(f,'sha256').hexdigest()

def save(path,value):
    tmp=path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value,indent=2)+'\n')
    tmp.replace(path)

def measure(output):
    core=4
    rows=[]
    for repeat in range(3):
        for traffic,cc in CASES:
            name=f'k32_{traffic}_cc{cc}'
            container=f'twins-k32-{output.parent.name}-{name}-r{repeat}'
            work=output/f'{name}/trial-{repeat}'
            work.mkdir(parents=True)
            command=['docker','run','--rm','--name',container,'--network=none','--ulimit','core=0',
                '--label','twins.evidence.campaign=paper-followup-20261123',
                '--user',f'{os.getuid()}:{os.getgid()}','--cpuset-cpus',str(core),
                '--memory=70g','--memory-swap=70g','-v',f'{MODEL}:/hpcc:ro',
                '-v',f'{DATA}:/inputs:ro','-v',f'{work}:/result','-w','/result',
                '-e','LD_LIBRARY_PATH=/hpcc/simulation/build','localhost/hpcc-build',
                '/hpcc/simulation/build/scratch/rdma-ref','/inputs/hpcc.cfg',
                '--topo=/inputs/ft32.topo',f'--flows=/inputs/k32_{traffic}.csv',
                '--out=/result/output.fct',f'CC_MODE={cc}']
            record=dict(case=name,trial=repeat,command=command,core=core,cap_gib=70,
                        shared_reservation_gib=70,start_utc_ns=time.time_ns(),
                        input_sha256={p.name:sha(p) for p in
                        (DATA/'hpcc.cfg',DATA/'ft32.topo',DATA/f'k32_{traffic}.csv')},
                        binary_sha256=sha(MODEL/'simulation/build/scratch/rdma-ref'))
            save(work/'started.json',record)
            start=time.perf_counter_ns()
            with (work/'run.log').open('wb') as log:
                try:
                    rc=subprocess.run(command,stdout=log,stderr=subprocess.STDOUT,timeout=3600).returncode
                except subprocess.TimeoutExpired:
                    subprocess.run(['docker','stop','-t','5',container],
                                   stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                    rc=124
            record.update(rc=rc,wall_ns=time.perf_counter_ns()-start,end_utc_ns=time.time_ns())
            # Comparison is outside the process wall interval, preserving all FCT fields and duplicates.
            out=work/'output.fct'
            reference=DATA/(name+'.fct')
            if out.is_file():
                env={**os.environ,'LC_ALL':'C'}
                for src,dst in ((out,work/'sorted'),(reference,work/'reference-sorted')):
                    with dst.open('wb') as f:
                        subprocess.run(['sort','-S','128M',str(src)],stdout=f,env=env,check=True)
                record.update(output_sha256=sha(out),reference_sha256=sha(reference),
                              equal=sha(work/'sorted')==sha(work/'reference-sorted'))
                (work/'sorted').unlink()
                (work/'reference-sorted').unlink()
            record['accepted']=rc==0 and record.get('equal',False)
            save(work/'result.json',record)
            rows.append(record)
            print(name,repeat,rc,record.get('equal'),record['wall_ns']/1e9,flush=True)
    return rows

def main():
    output=Path(sys.argv[1]).resolve()
    output.mkdir(parents=True,exist_ok=False)
    rows=measure(output)
    save(output/'summary.json',dict(rows=rows,accepted=sum(r['accepted'] for r in rows)))
    if not all(r['accepted'] for r in rows):
        raise SystemExit(1)

if __name__=='__main__':
    main()
