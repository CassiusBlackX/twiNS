"""Capture full CUDA traces of the original three simulator binaries."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import crosspod
from crosspod_attribution import ALIGNED, CASES, prepare
from crosspod_dispersed import records
from crosspod_large import binaries
from pfc_sustained import save


ORDER = {'8-pod-pairs': ('twins','gedes','days'), '240-pod-pairs': ('days','gedes','twins')}


def engine_command(binary, engine, src, spec):
    slots = spec['cap_ns']//1000
    env = dict(os.environ)
    env.pop('GEDES_PAIRED_LEAF',None)
    if engine=='twins':
        cmd=[binary,'--k=16','--tcp=newreno','--queue=200','--ecnK=200',f'--max_slots={slots}',
             f'--flows={src}/twins.csv',f'--ecmp_from={src}/twins-hash.csv','--out=output.fct']
    elif engine=='gedes':
        cmd=[binary,'--ft_k=16',f'--flow_file={src}/gedes.csv',f'--max_slots={slots}','--output=output.fct']
        env.update(GEDES_ECMP_FILE=str(src/'gedes-hash.csv'), GEDES_PAIR_ROUTES='1',GEDES_INDEPENDENT_CONNECTIONS='1')
    else:
        cmd=[binary,src/'days.toml','--engine','cuda']
    return ['taskset','-c','0',*map(str,cmd)],env


def capture(out,tag,engine,binary,root,granularity='node'):
    source=root/'inputs'/tag
    spec=json.loads((source/'input.json').read_text())
    expected=json.loads((source/'expected.json').read_text())[engine]
    digest=crosspod.sha(binary)
    assert digest==expected['binary_sha256']
    cmd,env=engine_command(binary,engine,source,spec)
    out.mkdir(parents=True)
    prof=['nsys','profile','--trace=cuda','--sample=none','--cpuctxsw=none',f'--cuda-graph-trace={granularity}',
          '--output',str(out/'native-cuda'),*cmd]
    with (out/'profile.log').open('wb') as stream:
        subprocess.run(prof,cwd=out,env=env,stdout=stream,stderr=subprocess.STDOUT,check=True,timeout=550)
    output=out/'output.fct'
    raw=out/'native-cuda.nsys-rep'
    log=(out/'profile.log').read_text(errors='replace')
    result={'input':tag,'engine':engine,'binary_sha256':digest,'command':prof,'archive_sha256':crosspod.sha(raw)}
    if engine=='days':
        result['fingerprint']=re.findall(r'result_fnv1a64=([0-9a-f]+)', log)[-1]
        assert result['fingerprint']==expected['fingerprint']
        result['native_gpu']=next(line for line in log.splitlines() if line.startswith('record=days_device'))
        result['native_result']=next(line for line in log.splitlines() if line.startswith('record=days_result'))
    else:
        result['fct_sha256']=crosspod.sha(output)
        assert result['fct_sha256']==expected['fct_sha256']
        rows=records(output)
        field='completed_ns' if engine=='twins' else 'completed_timestamp'
        assert len(rows)==2048 and all(int(r[field])>=0 for r in rows)
        assert sum(int(r['flow_size']) for r in rows)==382730240
        result['completed_bytes']=382730240
        result['native_loop_ns']=round(float(re.findall(r'Simulation Duration: ([.0-9]+)s',log)[-1])*1e9)
        result['executed_slots_reported']=int(re.findall(r'Executed Slots: (\d+)',log)[-1])
    save(out/'capture.json',result)
    print('captured', tag, engine, raw.stat().st_size, flush=True)


def measure(out):
    engines,_=binaries(ALIGNED/'build')
    for tag in CASES:
        for name in ORDER[tag]:
            capture(out/'profiles'/tag/name,tag,name,engines[name],out)


def export(out,tag,engine):
    raw=out/'profiles'/tag/engine/'native-cuda.nsys-rep'
    file=out.parent/'sqlite'/tag/f'{engine}.sqlite'
    file.parent.mkdir(parents=True,exist_ok=True)
    log=out/'profiles'/tag/engine/'export.log'
    with log.open('wb') as stream:
        subprocess.run(['nsys','export','--type=sqlite','--output',str(file),str(raw)],stdout=stream,
                       stderr=subprocess.STDOUT,check=True,timeout=480)
    print('exported',tag,engine,file.stat().st_size,flush=True)


def main():
    out=Path(sys.argv[1]).resolve()
    if len(sys.argv)>2:
        return measure(out)
    out.mkdir()
    prepare(out)
    save(out/'tools.json',{k:subprocess.check_output(cmd,text=True).strip() for k,cmd in {
        'nsys':['nsys','--version'],'gpu':['nvidia-smi','--query-gpu=name,driver_version,memory.total','--format=csv,noheader']}.items()})
    subprocess.run(['flock',crosspod.LOCK,sys.executable,crosspod.OBSERVER,out/'resources',
                    sys.executable,__file__,out,'capture'],check=True)
    with ThreadPoolExecutor(max_workers=2) as jobs:
        work=[jobs.submit(export,out,tag,engine) for tag in CASES for engine in ORDER[tag]]
        for job in work:
            job.result()


if __name__=='__main__':
    main()
