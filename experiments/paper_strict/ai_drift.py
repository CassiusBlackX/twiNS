"""Cross original and final AI builds with the network window and launch path."""
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from statistics import median

from crosspod import VIDEOS, LOCK, OBSERVER
from pfc_sustained import normalize, save
from exit_snapshot import sha

DATA=VIDEOS/'twins-release-cuda-20261122/project/paper-inputs'
NEW=VIDEOS/'twins-paper-followup-20261123/candidate'
OLD=VIDEOS/'twins-paper-evidence-20261116/candidate'
CASES=(('gpt3_13b_probe_hpn128',128),('c_allgather_512m_hpn1024',1024))
MODES={'default':(), 'no-network-window':('--gwin=0',), 'no-persistent-launch':('--persist=0',)}

def audit(out):
    static=out/'static'
    static.mkdir()
    with (static/'nvcc.log').open('wb') as log:
        subprocess.run(['/usr/local/cuda/bin/nvcc','--version'],stdout=log,stderr=subprocess.STDOUT,check=True)
    data={}
    for label,root,build in [('old',OLD,OLD/'build-default'),('new',NEW,NEW/'build')]:
        d=static/label
        d.mkdir()
        binary=build/'twins-ai'
        with (d/'device-usage.log').open('wb') as log:
            subprocess.run(['/usr/local/cuda/bin/cuobjdump','--dump-resource-usage',binary],
                           stdout=log,stderr=subprocess.STDOUT,check=True,timeout=120)
        with (d/'effective-commands.log').open('w') as log:
            if (build/'build.ninja').is_file():
                subprocess.run(['ninja','-C',build,'-t','commands','twins-ai'],
                               stdout=log,stderr=subprocess.STDOUT,check=True,timeout=120)
            else:
                cmake=build/'CMakeCache.txt'
                if cmake.is_file():
                    log.write('\n'.join(x for x in cmake.read_text().splitlines() if x.startswith(
                        ('CMAKE_GENERATOR:', 'CMAKE_CUDA_', 'CMAKE_CXX_', 'TWINS_'))) + '\n')
                for name in ('flags.make','link.txt'):
                    p=build/'CMakeFiles/twins-ai.dir'/name
                    if p.is_file():
                        log.write(p.read_text()+'\n')
        data[label]=dict(sha256=sha(binary),source={p:sha(root/p) for p in
            ('src/astra/closed.h','src/rdma/rmain.cu','src/rdma/rstep.cuh','src/rdma/rdma.h') if (root/p).is_file()})
    save(static/'provenance.json',data)

def run_one(out,binary,name,hosts,opts):
    out.mkdir(parents=True)
    suffix=name.replace('_hpn','_')
    command=['taskset','-c','0',str(binary),str(NEW/'validation/simai/SimAI.conf'),
        f'--topo={DATA}/data/simai/hpn{hosts}.topo',f'--astra={DATA}/out/simai/wl/paper/{suffix}.txt',
        '--out=messages.fct','--dag-out=actions.dag','--nvl-islands=0','--fp-bases=1000003,1000033',*opts]
    log=out/'process.log'
    start=time.monotonic_ns()
    with log.open('wb') as stream:
        try:
            rc=subprocess.run(command,cwd=out,stdout=stream,stderr=subprocess.STDOUT,timeout=1200).returncode
        except subprocess.TimeoutExpired:
            rc=124
    elapsed=time.monotonic_ns()-start
    ref=DATA/f'out/simai/paper/{name}'
    fct=out/'messages.fct'
    act=out/'actions.dag'
    matched=dict(fct=fct.is_file() and normalize(fct)==normalize(ref.with_suffix('.ref')),
                dag=act.is_file() and normalize(act,True)==normalize(ref.with_suffix('.dag'),True))
    text=log.read_text()
    measures=re.findall(r'closed loop: (\d+) flows, (\d+) timers, (\d+) callbacks, (\d+) syncs,.*?GPU ([\d.]+) s, system layer ([\d.]+) s, sync ([\d.]+) s',text)
    path=[line for line in text.splitlines() if line.startswith(('persistent:','global windows:','closed loop horizon:','flows done '))]
    metrics=dict(zip(('flows','timers','callbacks','rounds','gpu_s','system_s','sync_s'),measures[-1])) if measures else None
    row=dict(rc=rc,wall_ns=elapsed,matched=matched,accepted=rc==0 and all(matched.values()) and bool(metrics),
             launch_report=path,metrics=metrics,command=command,binary_sha256=sha(binary),
             time_reference_sha256=sha(ref.with_suffix('.ref')),
             action_reference_sha256=sha(ref.with_suffix('.dag')))
    save(out/'receipt.json',row)
    return row

def measure(out):
    out.mkdir()
    settings={}
    for case,hosts in CASES:
        for mode,opts in MODES.items():
            group=[]
            for round in range(3):
                arms=[('old',OLD/'build-default/twins-ai'),('new',NEW/'build/twins-ai')]
                if round%2: arms.reverse()
                for arm,binary in arms:
                    folder=out/case/mode/arm/f'trial-{round}'
                    row=run_one(folder,binary,case,hosts,opts)
                    group.append(dict(arm=arm,trial=round,**row))
                    print(case,mode,arm,round,row['accepted'],row['wall_ns']/1e9,flush=True)
            arms={}
            for arm in ('old','new'):
                valid=[x for x in group if x['arm']==arm and x['accepted']]
                arms[arm]=dict(valid=len(valid),wall_median_s=median(x['wall_ns'] for x in valid)/1e9 if len(valid)==3 else None,
                               gpu_median_s=median(float(x['metrics']['gpu_s']) for x in valid) if len(valid)==3 else None)
            settings[case+'/'+mode]=dict(measurements=group,arms=arms)
            save(out/'summary.json',settings)

def main():
    out=Path(sys.argv[1]).resolve()
    if len(sys.argv)>2:
        measure(out)
        return 0
    out.mkdir()
    audit(out)
    return subprocess.run(['flock',LOCK,sys.executable,OBSERVER,out/'resources',
                           sys.executable,__file__,out/'measurements','run']).returncode

if __name__=='__main__':
    raise SystemExit(main())
