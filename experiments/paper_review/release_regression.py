"""Check paper scenarios under one candidate; store hash receipts, not bulk traces."""
import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def save(path,value):
    temporary=path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value,indent=2)+'\n')
    temporary.replace(path)


def signature(path,kind,temp):
    if kind=='tcp':
        dig=hashlib.sha256()
        with path.open() as stream:
            rows=csv.reader(stream)
            next(rows)
            for row in rows:
                dig.update((','.join(str(int(x)) for x in row[:6])+'\n').encode())
        return dig.hexdigest()
    src=temp/'normalized'
    with path.open() as a, src.open('w') as b:
        for line in a:
            if kind=='dag':
                if line.startswith('#'):
                    continue
                line=re.sub(r'^(on [ft][0-9]* m).*',r'\1',line.rstrip())
            else:
                line=' '.join(str(int(x)) for x in line.split())
            b.write(line.rstrip()+'\n')
    env={**os.environ,'LC_ALL':'C'}
    with subprocess.Popen(['sort','-T',str(temp),'-S','256M',str(src)],stdout=subprocess.PIPE,env=env) as sort:
        result=hashlib.file_digest(sort.stdout,'sha256').hexdigest()
        if sort.wait()!=0:
            raise RuntimeError('normalization sort failed')
    src.unlink()
    return result


def invoke(command,work,cwd):
    start=time.monotonic_ns()
    with (work/'run.log').open('wb') as stream:
        try:
            rc=subprocess.run(command,cwd=cwd,stdout=stream,stderr=subprocess.STDOUT,timeout=7200).returncode
        except subprocess.TimeoutExpired:
            rc=124
    return dict(command=command,rc=rc,wall_ns=time.monotonic_ns()-start)


def version_flags(log):
    # Keep warnings from earlier rollback passes in the log. A last pass only
    # is eligible for comparison on the as-executed final output.
    last=re.split(r'^.*running again with[^\n]*\n',log,flags=re.M)[-1]
    return bool(re.search(r'were undecided|warning:.*(?:ties the chains|not exact)|error:|'
                          r'undecided.ties [1-9]|overflows:.*(?:tx|queue|ackq|qps|due) [1-9]',last))


def network(args):
    source=args.data
    inventory=json.loads(args.inventory.read_text())
    seen=set()
    for row in inventory:
        kind=row['kind']
        if kind not in ('tcp','rdma') or not row['reference_sha256'] or row['case'].startswith('dons_'):
            continue
        key=row['execution_config_sha256']
        if key in seen:
            continue
        seen.add(key)
        work=args.out/'network'/key
        work.mkdir(parents=True,exist_ok=True)
        if (work/'result.json').exists():
            continue
        dst=work/'output.fct'
        ref=Path(row['reference'])
        command=[str(args.build/('twins' if kind=='tcp' else 'twins-rdma'))]
        if kind=='rdma':
            command.append(str(args.source/'validation/hpcc/hpcc.cfg'))
        command+=row['args']+[f'--out={dst}']
        result=dict(case=row['case'],suite=row['suite'],kind=kind,reference_sha256=row['reference_sha256'])
        try:
            if sha(ref)!=row['reference_sha256']:
                raise ValueError('frozen reference digest mismatch')
            result.update(invoke(command,work,source))
            result['flagged']=version_flags((work/'run.log').read_text(errors='replace'))
            if dst.is_file():
                actual=signature(dst,kind,work)
                expected=signature(ref,kind,work)
                result.update(actual=actual,expected=expected,equal=actual==expected)
                if result['equal']:
                    dst.unlink()
            result['accepted']=result['rc']==0 and result.get('equal',False) and not result['flagged']
        except (OSError,ValueError,RuntimeError) as failure:
            result['error']=str(failure)
            result['accepted']=False
        save(work/'result.json',result)
        print('network',row['suite'],row['case'],result['accepted'],flush=True)


def ai_cases(source):
    jobs=source/'validation/simai/jobs_paper.txt'
    wl=source/'out/simai/wl'
    for line in jobs.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        name,topo,*rest=line.split()
        base=source/'out/simai/paper'/name
        yield ('paper',name,topo,wl/'paper'/(name.replace('_hpn','_')+'.txt'),base,rest,None)
    loads={
        'mar':(128,'microAR_tp128'), 'moe':(16,'moe_16'),
        'ar64':(128,'paper/c_allreduce_64m_128'),
        'llp':(128,'paper/llama_65b_probe_128'),
        'a2a':(128,'paper/c_alltoall_64m_128'),
    }
    whatif=source/'out/simai/whatif'
    files=[p for p in sorted(whatif.glob('*.ref')) if p.with_suffix('.dag').is_file()]
    assert len(files)==47, len(files)
    for ref in files:
        load,setting=ref.stem.split('_',1)
        hosts,workload=loads[load]
        if setting in ('hpn','dcn'):
            topo=f'{setting}{hosts}.topo'
        elif setting.startswith('dcnb'):
            topo=f'dcn{hosts}.topo'
        elif setting=='g200':
            topo=f'sx{hosts}_200.topo'
        else:
            topo=f'sx{hosts}.topo'
        extra=(['CC_MODE=3'] if setting=='cc3' else
               ['BUFFER_SIZE='+setting[1:]] if setting.startswith('b') else
               ['BUFFER_SIZE='+setting[4:]] if setting.startswith('dcnb') else [])
        cache=whatif/(load+'.flows.dag')
        yield ('whatif',ref.stem,topo,wl/(workload+'.txt'),ref.with_suffix(''),extra,cache if cache.is_file() else None)


def closed(args):
    for suite,name,topo,workload,reference,extra,cache in ai_cases(args.data):
        work=args.out/suite/name
        work.mkdir(parents=True,exist_ok=True)
        if (work/'result.json').exists():
            continue
        fct,dag=work/'messages',work/'actions.dag'
        config=args.source/'validation/simai/SimAI.conf'
        topo_path=args.data/'data/simai'/topo
        binary=args.build/'twins-ai'
        cmd=[str(binary),str(config),f'--topo={topo_path}',f'--astra={workload}',
             f'--out={fct}',f'--dag-out={dag}',f'--nvl-islands={args.workers}',
             '--fp-bases=1000003,1000033',*extra]
        if cache:
            cmd.append(f'--flows-cache={cache}')
        result=dict(case=name,suite=suite,workers=args.workers)
        try:
            result.update(input_sha256={str(p):sha(p) for p in (config,topo_path,workload)})
            result.update(invoke(cmd,work,work))
            result['flagged']=version_flags((work/'run.log').read_text(errors='replace'))
            for key,out,kind in [('fct',fct,'uid'),('dag',dag,'dag')]:
                ref=reference.with_suffix('.ref' if key=='fct' else '.dag')
                if out.is_file():
                    got=signature(out,kind,work)
                    expected=signature(ref,kind,work)
                    result[key]=dict(equal=got==expected,signature=got,reference=expected,reference_sha256=sha(ref))
                    if got==expected:
                        out.unlink()
            result['accepted']=result['rc']==0 and not result['flagged'] and all(result.get(k,{}).get('equal') for k in ('fct','dag'))
        except (OSError,ValueError,RuntimeError) as failure:
            result['error']=str(failure)
            result['accepted']=False
        save(work/'result.json',result)
        print(suite,name,result['accepted'],flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    for name in ('source','data','inventory','build','out'):
        parser.add_argument(name,type=lambda value:Path(value).resolve())
    parser.add_argument('--workers',type=int,default=4)
    parser.add_argument('--revision', help='Pinned source commit for a tree exported with git archive')
    args=parser.parse_args()
    args.out.mkdir(parents=True,exist_ok=True)
    binaries={name:sha(args.build/name) for name in ('twins','twins-rdma','twins-ai')}
    revision=args.revision or subprocess.check_output(
        ['git','rev-parse','HEAD'],cwd=args.source,text=True).strip()
    save(args.out/'run-provenance.json',dict(binaries=binaries,inventory_sha256=sha(args.inventory),
                                           source_revision=revision))
    network(args)
    closed(args)
    summary={}
    for suite,glob in [('network','network/*/result.json'),('paper','paper/*/result.json'),('whatif','whatif/*/result.json')]:
        rows=[json.loads(p.read_text()) for p in args.out.glob(glob)]
        summary[suite]=dict(tested=len(rows),accepted=sum(row['accepted'] for row in rows))
    save(args.out/'finished.json',summary)
