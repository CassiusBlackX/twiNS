"""Final-candidate GPU timings, hot-path pairs, and the callback horizon control."""
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import statistics
import subprocess
import sys
import time
from release_status import status

HOME=Path.home()/'Videos'
CANDIDATE=HOME/'twins-paper-followup-20261123/candidate'
OLD=HOME/'twins-paper-evidence-20261116/candidate/build-default'
DATA=HOME/'twins-release-cuda-20261122/project/paper-inputs'
K32=HOME/'netsim/rdma'
OBSERVE=HOME/'twins-paper-followthrough-20261122/project/experiments/paper_review/observe_cuda.py'

def sha(path):
    with path.open('rb') as f:
        return hashlib.file_digest(f,'sha256').hexdigest()

def save(path,value):
    tmp=path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value,indent=2)+'\n')
    tmp.replace(path)

def invoke(command,work,env=None,cwd=None,timeout=1800):
    work.mkdir(parents=True)
    start=time.perf_counter_ns()
    with (work/'run.log').open('wb') as stream:
        try:
            rc=subprocess.run(['taskset','-c','0',*map(str,command)],cwd=cwd or work,env={**os.environ,**(env or {})},
                              stdout=stream,stderr=subprocess.STDOUT,timeout=timeout).returncode
        except subprocess.TimeoutExpired:
            rc=124
    elapsed=time.perf_counter_ns()-start
    return dict(command=list(map(str,command)),rc=rc,wall_ns=elapsed,
                parsed_status=status((work/'run.log').read_text(errors='replace')))

def signature(path,kind,work):
    # Timing has ended before any parsing, canonicalization, or sorting.
    normalized=work/'normalized'
    with path.open() as src,normalized.open('w') as out:
        if kind=='tcp':
            rows=csv.reader(src)
            next(rows)
            for row in rows:
                out.write(','.join(str(int(x)) for x in row[:6])+'\n')
        else:
            for line in src:
                if kind=='dag':
                    if line.startswith('#'):
                        continue
                    line=re.sub(r'^(on [ft][0-9]* m).*',r'\1',line.rstrip())
                else:
                    line=' '.join(str(int(x)) for x in line.split())
                out.write(line.rstrip()+'\n')
    if kind=='tcp':
        value=sha(normalized)
    else:
        with subprocess.Popen(['sort','-S','128M',str(normalized)],
                              env={**os.environ,'LC_ALL':'C'},stdout=subprocess.PIPE) as proc:
            value=hashlib.file_digest(proc.stdout,'sha256').hexdigest()
            if proc.wait():
                raise RuntimeError('sort failed')
    normalized.unlink()
    return value

def complete(record):
    return record['rc']==0 and not record['parsed_status']['flagged']

def k32(output):
    rows=[]
    for trial in range(3):
        cases=[('a2a30',1),('a2a30',3),('a2a50s',1),('a2a50s',3),('incast16',1)]
        if trial%2:
            cases.reverse()
        for traffic,cc in cases:
            name=f'k32_{traffic}_cc{cc}'
            work=output/'rdma-k32'/name/f'trial-{trial}'
            command=[CANDIDATE/'build/twins-rdma',K32/'hpcc.cfg',
                     f'--topo={K32}/ft32.topo',f'--flows={K32}/k32_{traffic}.csv',
                     '--out=output.fct',f'CC_MODE={cc}','--fp-bases=1000003,1000033']
            record=invoke(command,work)
            actual=work/'output.fct'
            reference=K32/(name+'.fct')
            record.update(case=name,trial=trial,binary_sha256=sha(command[0]),
                          input_sha256={p.name:sha(p) for p in (K32/'hpcc.cfg',K32/'ft32.topo',K32/f'k32_{traffic}.csv')})
            record['equal']=actual.is_file() and signature(actual,'rdma',work)==signature(reference,'rdma',work)
            record['accepted']=complete(record) and record['equal']
            save(work/'result.json',record)
            rows.append(record)
            print('k32',name,trial,record['accepted'],record['wall_ns']/1e9,flush=True)
    save(output/'rdma-k32/summary.json',dict(rows=rows,accepted=sum(x['accepted'] for x in rows)))

def ai_command(binary,name):
    hosts=1024 if name.endswith('1024') else 128
    return [binary,CANDIDATE/'validation/simai/SimAI.conf',f'--topo={DATA}/data/simai/hpn{hosts}.topo',
            f'--astra={DATA}/out/simai/wl/paper/{name.replace("_hpn","_")}.txt',
            '--out=messages.fct','--dag-out=actions.dag','--nvl-islands=0','--fp-bases=1000003,1000033']

def ai_validate(work,name):
    return {
        'messages':signature(work/'messages.fct','rdma',work)==signature(DATA/f'out/simai/paper/{name}.ref','rdma',work),
        'actions':signature(work/'actions.dag','dag',work)==signature(DATA/f'out/simai/paper/{name}.dag','dag',work),
    }

def ai(output):
    rows=[]
    name='c_allgather_512m_hpn1024'
    for trial in range(3):
        arms=[('safe',{}),('one-slot',{'TWINS_CL_ONE_SLOT':'1'})]
        if trial%2:
            arms.reverse()
        for arm,env in arms:
            work=output/'callback-horizon'/arm/f'trial-{trial}'
            record=invoke(ai_command(CANDIDATE/'build/twins-ai',name)+['--gwin=0'],work,env)
            record.update(case=name,trial=trial,arm=arm,environment=env,validation=ai_validate(work,name))
            log=(work/'run.log').read_text()
            metrics=re.findall(r'closed loop: (\d+) flows, (\d+) timers, (\d+) callbacks, (\d+) syncs,.*?GPU ([\d.]+) s, system layer ([\d.]+) s, sync ([\d.]+) s',log)
            record['coordination']=dict(zip(('flows','timers','callbacks','round_trips','gpu_s','system_s','sync_s'),metrics[-1])) if metrics else None
            spans=re.findall(r'closed loop horizon: (\d+) base slots \(safe maximum (\d+)\)',log)
            record['horizon_slots']=list(map(int,spans[-1])) if spans else None
            record['accepted']=complete(record) and all(record['validation'].values()) and record['coordination'] is not None
            save(work/'result.json',record)
            rows.append(record)
            print('horizon',arm,trial,record['accepted'],record['wall_ns']/1e9,flush=True)
    save(output/'callback-horizon/summary.json',dict(rows=rows,accepted=sum(x['accepted'] for x in rows)))
    pairs=[]
    for name in ('c_allgather_512m_hpn1024','gpt3_13b_probe_hpn128'):
        for trial in range(3):
            arms=[('timed-version',OLD/'twins-ai'),('final-candidate',CANDIDATE/'build/twins-ai')]
            if trial%2:
                arms.reverse()
            for label,binary in arms:
                work=output/'ai-hot-path'/name/label/f'trial-{trial}'
                record=invoke(ai_command(binary,name),work)
                record.update(case=name,trial=trial,arm=label,binary_sha256=sha(binary),validation=ai_validate(work,name))
                record['accepted']=complete(record) and all(record['validation'].values())
                save(work/'result.json',record)
                pairs.append(record)
                print('AI pair',name,label,trial,record['accepted'],record['wall_ns']/1e9,flush=True)
    save(output/'ai-hot-path/summary.json',dict(rows=pairs,accepted=sum(x['accepted'] for x in pairs)))

def tcp(output):
    frozen=HOME/'twins-isolated-20261118/out'
    manifest=json.loads((frozen/'manifest.json').read_text())
    rows=[]
    for job in manifest['cubic']:
        for protocol in ('dctcp','newreno','cubic'):
            for trial in range(3):
                signatures={}
                current=[]
                arms=[('timed-version',OLD/'twins'),('final-candidate',CANDIDATE/'build/twins')]
                if trial%2:
                    arms.reverse()
                for label,binary in arms:
                    work=output/'tcp-hot-path'/job['case']/protocol/label/f'trial-{trial}'
                    command=[binary,f'--k={job["k"]}',f'--flows={frozen}/inputs/{job["flows"]}',
                             f'--tcp={protocol}',f'--queue={job["queue"]}',f'--ecnK={job["ecnK"]}','--out=output.fct']
                    record=invoke(command,work)
                    attempts=[record]
                    if label=='timed-version' and record['parsed_status']['flagged'] and not record['rc']:
                        retry=work/'retry-128'
                        command[0]=OLD/'twins128'
                        record=invoke(command,retry)
                        attempts.append(record)
                        result_file=retry/'output.fct'
                    else:
                        result_file=work/'output.fct'
                    record=dict(record,wall_ns=sum(x['wall_ns'] for x in attempts),
                                attempts=attempts,case=job['case'],tcp=protocol,trial=trial,arm=label,
                                binary_sha256=sha(binary))
                    record['signature']=signature(result_file,'tcp',work) if result_file.exists() else None
                    record['accepted']=complete(record) and record['signature'] is not None
                    signatures[label]=record['signature']
                    current.append((work,record))
                equal=signatures['timed-version'] is not None and signatures['timed-version']==signatures['final-candidate']
                for work,record in current:
                    record['paired_output_equal']=equal
                    if protocol=='cubic':
                        record['independent_reference_equal']=record['signature']==signature(frozen/'inputs'/job['reference'],'tcp',work)
                    record['accepted'] &= equal and record.get('independent_reference_equal',True)
                    save(work/'result.json',record)
                    rows.append(record)
                print('TCP pair',job['case'],protocol,trial,equal,flush=True)
    save(output/'tcp-hot-path/summary.json',dict(rows=rows,accepted=sum(x['accepted'] for x in rows)))

def irn(output):
    base=HOME/'twins-paper-scale-20261119/inputs'
    old=HOME/'twins-paper-review-20261120/project/build/twins-rdma'
    rows=[]
    for traffic in ('spread','incast'):
        path=base/f'1024/{traffic}'
        for trial in range(3):
            signatures={}
            pending=[]
            for label,binary in [('timed-version',old),('final-candidate',CANDIDATE/'build/twins-rdma')]:
                work=output/'irn-hot-path'/traffic/label/f'trial-{trial}'
                record=invoke([binary,path/'config',f'--topo={base}/1024/uniform.graph',
                               f'--flows={path}/flows.csv','IRN_BDP=156000','--irn-parallel','--out=output.fct',
                               '--fp-bases=1000003,1000033'],work)
                signatures[label]=signature(work/'output.fct','rdma',work) if (work/'output.fct').exists() else None
                record.update(case=traffic,trial=trial,arm=label,output_signature=signatures[label],binary_sha256=sha(binary))
                pending.append((work,record))
            equal=signatures['timed-version'] is not None and signatures['timed-version']==signatures['final-candidate']
            for work,record in pending:
                record['accepted']=complete(record) and equal
                record['paired_output_equal']=equal
                save(work/'result.json',record); rows.append(record)
            print('IRN pair',traffic,trial,equal,flush=True)
    save(output/'irn-hot-path/summary.json',dict(rows=rows,accepted=sum(x['accepted'] for x in rows)))

def main():
    output=Path(sys.argv[1]).resolve()
    output.mkdir(parents=True,exist_ok=False)
    if len(sys.argv)>2 and sys.argv[2]=='measure':
        for function in (k32,ai,tcp,irn):
            function(output)
        return
    build=CANDIDATE/'build'
    with (output/'build.log').open('wb') as stream:
        subprocess.run(['cmake','-S',CANDIDATE,'-B',build,'-G','Ninja',
            '-DTWINS_GPU_BACKEND=CUDA','-DCMAKE_CUDA_ARCHITECTURES=120',
            '-DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc'],stdout=stream,stderr=subprocess.STDOUT,check=True)
        subprocess.run(['taskset','-c','12-15','cmake','--build',build,
            '--target','twins','twins-rdma','twins-ai','twins-island','twins-astra-stub','-j1'],
            stdout=stream,stderr=subprocess.STDOUT,check=True)
    inputs=[K32/'hpcc.cfg',K32/'ft32.topo',
            *[K32/f'k32_{name}.csv' for name in ('a2a30','a2a50s','incast16')],
            CANDIDATE/'validation/simai/SimAI.conf']
    for name in ('c_allgather_512m_hpn1024','gpt3_13b_probe_hpn128'):
        hosts=1024 if name.endswith('1024') else 128
        inputs.extend([DATA/f'data/simai/hpn{hosts}.topo',
                       DATA/f'out/simai/wl/paper/{name.replace("_hpn","_")}.txt',
                       DATA/f'out/simai/paper/{name}.ref',DATA/f'out/simai/paper/{name}.dag'])
    save(output/'provenance.json',dict(
        candidate=(CANDIDATE/'revision').read_text().strip(),
        binaries={p.name:sha(p) for p in build.glob('twins*') if p.is_file()},
        inputs={str(p):sha(p) for p in inputs},
        concurrency='CPU reference core 4, cap 70 GiB; GPU host core 0; native build on cores 12-15, j1.'))
    command=['flock',str(HOME/'twins-isolated-20261118/out/exclusive-campaign.lock'),
             'python3',str(OBSERVE),str(output/'resources'),sys.executable,str(Path(__file__).resolve()),
             str(output/'measurements'),'measure']
    rc=subprocess.run(command).returncode
    save(output/'suite-result.json',dict(rc=rc,candidate=(CANDIDATE/'revision').read_text().strip()))
    raise SystemExit(rc)

if __name__=='__main__':
    main()
