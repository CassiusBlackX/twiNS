"""Replay the A5 inventory using the exact bda9422 candidate on HIP or CUDA."""
import argparse
import fcntl
import json
from pathlib import Path
import subprocess
import sys

REVISION='bda9422cff61b74ac8491f50662dbf3f6712729c'

def run(out,source,data,inventory,build,workers,backend):
    out.mkdir(parents=True,exist_ok=True)
    try:
        git_revision=subprocess.check_output(['git','-C',source,'rev-parse','HEAD'],text=True,stderr=subprocess.DEVNULL).strip()
    except subprocess.CalledProcessError:
        git_revision=(source/'revision').read_text().strip()
    if git_revision != REVISION:
        raise ValueError(f'unexpected revision {git_revision}')
    sys.path.insert(0,str(source/'experiments/paper_review'))
    import release_regression as replay
    args=argparse.Namespace(source=source,data=data,inventory=inventory,build=build,out=out,workers=workers)
    replay.save(out/'run-provenance.json',dict(backend=backend,source_revision=git_revision,
        binaries={p.name:replay.sha(p) for p in build.glob('twins*') if p.is_file()},
        inventory_sha256=replay.sha(inventory)))
    replay.network(args)
    replay.closed(args)
    summary={}
    for suite in ('network','paper','whatif'):
        rows=[json.loads(p.read_text()) for p in (out/suite).glob('*/result.json')]
        summary[suite]=dict(tested=len(rows),accepted=sum(row['accepted'] for row in rows),
                            flagged=sum(row.get('flagged',False) for row in rows))
    expected={'network':985,'paper':42,'whatif':47}
    if any(summary[s]['tested']!=n for s,n in expected.items()):
        raise RuntimeError(f'incomplete replay inventory: {summary}')
    replay.save(out/'finished.json',summary)

def local(out,workspace):
    source=workspace/'twins-paper-release-20261122'
    data=workspace/'twiNS'
    out.mkdir(parents=True,exist_ok=True)
    build=out/'build'
    with (out/'build.log').open('wb') as stream:
        subprocess.run(['cmake','-S',source,'-B',build,'-G','Ninja','-DTWINS_GPU_BACKEND=HIP',
            '-DCMAKE_HIP_ARCHITECTURES=gfx1200','-DCMAKE_HIP_COMPILER=/opt/rocm/llvm/bin/clang++',
            '-DCMAKE_EXPORT_COMPILE_COMMANDS=ON'],stdout=stream,stderr=subprocess.STDOUT,check=True)
        subprocess.run(['cmake','--build',build,'--target','twins','twins-rdma','twins-ai','twins-island',
            'twins-astra-stub','-j2'],stdout=stream,stderr=subprocess.STDOUT,check=True)
    inventory=data/'out/paper-evidence-20261116/manifests/scenario-inventory.json'
    with Path('/tmp/specdes-pfc-and-replay-hip.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        run(out/'measurements',source,data,inventory,build,4,'HIP')

def main():
    out=Path(sys.argv[1]).resolve()
    if len(sys.argv)==3 and sys.argv[2]!='cuda':
        return local(out,Path(sys.argv[2]).resolve())
    videos=Path.home()/'Videos'
    source=videos/'twins-paper-followup-20261123/candidate'
    inputs=videos/'twins-release-cuda-20261122/project'
    if len(sys.argv)==3:
        return run(out,source,inputs/'paper-inputs',inputs/'inventory.json',source/'build',16,'CUDA')
    out.mkdir(parents=True,exist_ok=True)
    observe=videos/'twins-paper-followthrough-20261122/project/experiments/paper_review/observe_cuda.py'
    lock=videos/'twins-isolated-20261118/out/exclusive-campaign.lock'
    return subprocess.run(['flock',str(lock),sys.executable,str(observe),str(out/'resources'),
        sys.executable,__file__,str(out/'measurements'),'cuda']).returncode

if __name__=='__main__':
    raise SystemExit(main())
