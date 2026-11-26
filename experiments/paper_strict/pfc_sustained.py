"""Validate complete, frozen DCN+ PFC inputs and measure safe/serial in separate builds."""
import json
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from statistics import median

from crosspod import LOCK, OBSERVER
from gpu_counters import build
from pfc_fixed_pilot import defaultdict_data
from pfc_reference import HOME, MODEL, ORIGINAL, DATA, CANDIDATE, SIM
from exit_snapshot import sha

DESIGN=[('light',64,8),('intermittent',32,64),('higher-pause',24,32)]
EVIDENCE=HOME/'twins-paper-strict-20261123'
SIM_BIN=f'{SIM}/trial-build/scratch/ns3.36.1-AstraSimNetwork-optimized'

def save(path,data):
    tmp=path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data,indent=2)+'\n')
    tmp.replace(path)

def timed(command,work,name,seconds=1800):
    start=time.monotonic_ns()
    with (work/name).open('wb') as out:
        try:
            rc=subprocess.run(list(map(str,command)),cwd=work,stdout=out,stderr=subprocess.STDOUT,timeout=seconds).returncode
        except subprocess.TimeoutExpired:
            rc=124
        elapsed=time.monotonic_ns()-start
    return rc,elapsed

def freeze(out):
    dataset=ORIGINAL/'out/simai/wl/paper/c_alltoall_64m_128.txt'
    topology=ORIGINAL/'data/simai/dcn128.topo'
    config=CANDIDATE/'validation/simai/SimAI.conf'
    for label,pool,burst in DESIGN:
        work=out/label
        work.mkdir(parents=True)
        common=work/'input'
        common.mkdir()
        shutil.copy2(topology,common/'topo.log')
        wl=common/'workload.log'
        wl.write_text(dataset.read_text().replace('67108864',str(burst*1024**2)))
        cfg=common/'config.log'
        cfg.write_text(re.sub(r'^BUFFER_SIZE\s+\d+',f'BUFFER_SIZE {pool}',config.read_text(),flags=re.M))
        save(common/'provenance.json',dict(pool_mib=pool,burst_mib=burst,
             workload_sha256=sha(wl),reference_topology_sha256=sha(topology),config_sha256=sha(cfg)))

def reference(out,core=4,trace_wire=True,timeout=3600,docker_options=()):
    topo=next(DATA.glob('topo/DCN+SingleToR_128g*200Gbps*A100*'))
    extractor=Path(__file__).with_name('trace2dag.py')
    for label,pool,burst in DESIGN:
        work=out/label
        common=work/'input'
        ref=work/'reference'
        ref.mkdir()
        raw=out.parent.parent/'raw-pfc'/label
        (raw/'etc/simulation').mkdir(parents=True)
        (raw/'etc/results').mkdir()
        name=f'twins-pfc-confirm-{out.parent.name.lower()}-{pool}'
        cmd=['docker','run','--rm','--name',name,'--network=none','--ulimit','core=0',
             '--user',f'{os.getuid()}:{os.getgid()}','--cpuset-cpus',str(core),'--memory=20g','--memory-swap=20g',
             '-e','AS_SEND_LAT=3','-e','TWINS_TRACE=/raw/events.trace']
        if trace_wire:
            cmd+=['-e','TWINS_TXTRACE=/raw/wire.trace','-e','TWINS_TXUNTIL=400000']
        cmd+=[*docker_options,'-v',f'{MODEL}:/simai:ro','-v',f'{DATA}:/data:ro',
             '-v',f'{raw}:/raw','-v',f'{raw}/etc:/etc/astra-sim','-v',f'{common}:/case:ro',
             '-w','/raw','localhost/simai-build','/simai/'+SIM_BIN,
             '-t','1','-w','/case/workload.log','-n',f'/data/topo/{topo.name}','-c','/case/config.log']
        rc,wall=timed(cmd,ref,'process.log',timeout)
        if rc==124:
            subprocess.run(['docker','stop','-t','4',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        fct=raw/'etc/simulation/llama_hpn7_fct.txt'
        trace=raw/'events.trace'
        extract_rc=None
        if trace.is_file() and fct.is_file():
            extract_rc,_=timed([sys.executable,extractor,trace,fct,ref/'actions.dag',ref/'messages.fct'],ref,'extract.log')
        pfc=raw/'etc/simulation/llama_hpn7_pfc.txt'
        if pfc.is_file():
            shutil.copy2(pfc,ref/'pfc.log')
        row=dict(case=label,rc=rc,extract_rc=extract_rc,process_wall_ns=wall,pool_mib=pool,burst_mib=burst,
            source_binary_sha256=sha(MODEL/SIM_BIN),native_topology_sha256=sha(topo),
            commands=list(map(str,cmd)),wire_prefix_sha256=sha(raw/'wire.trace') if (raw/'wire.trace').is_file() else None)
        save(ref/'receipt.json',row)
        print('reference',label,rc,extract_rc,flush=True)

def normalize(p,dag=False):
    items=[]
    for line in p.read_text().splitlines():
        if dag:
            if line.startswith('#'): continue
            line=re.sub(r'^(on [ft][0-9]* m).*',r'\1',line)
        items.append(line.rstrip())
    return sorted(items)

def measure(out,instrumented):
    records={}
    clean=CANDIDATE/'build/twins-ai'
    for label,pool,burst in DESIGN:
        root=out/label
        inputs=root/'input'
        ref=root/'reference'
        proven=json.loads((ref/'receipt.json').read_text())
        reference_fct=normalize(ref/'messages.fct') if (ref/'messages.fct').is_file() else []
        reference_dag=normalize(ref/'actions.dag',True) if (ref/'actions.dag').is_file() else []
        rows=[]
        for epoch in ('diagnostic',0,1,2):
            order=('safe','serial') if epoch!=1 else ('serial','safe')
            for arm in order:
                work=root/f'{arm}-{epoch}'
                work.mkdir()
                binary=instrumented if epoch=='diagnostic' else clean
                cmd=['taskset','-c','0',binary,inputs/'config.log',f'--topo={inputs}/topo.log',
                     f'--astra={inputs}/workload.log','--out=messages.fct','--dag-out=actions.dag',
                     '--nvl-islands=0','--gwin=0','--fp-bases=1000003,1000033']
                if arm=='serial':
                    cmd.append('--serial')
                if epoch=='diagnostic':
                    cmd+=['--txtrace=raw-wire','--until=400000']
                rc,elapsed=timed(cmd,work,'process.log',1200)
                n=work/'messages.fct'
                a=work/'actions.dag'
                equal=dict(messages=bool(reference_fct) and n.is_file() and normalize(n)==reference_fct,
                           actions=bool(reference_dag) and a.is_file() and normalize(a,True)==reference_dag)
                text=(work/'process.log').read_text()
                streaks={int(i):int(n) for i,n in re.findall(r'pfc_pause_streak (\d+) (\d+) base_slots',text)}
                counts=defaultdict_data(work/'process.log')
                highest=max(((i,v) for i,v in counts.items() if v['busy']>=1000),
                            key=lambda pair:pair[1]['paused']/pair[1]['busy'],default=None)
                raw=work/'raw-wire'
                if raw.is_file() and highest:
                    # The raw, full prefix remains remote. Return only the hotspot and a digest.
                    node=int(highest[0])+128
                    with (work/'hotspot-wire.log').open('w') as dst:
                        for line in raw.open():
                            fields=line.split()
                            if len(fields) in (10,14) and fields[1].isdigit() and int(fields[1])==node:
                                dst.write(line)
                row=dict(case=label,arm=arm,sample=epoch,rc=rc,wall_ns=elapsed,binary_sha256=sha(binary),
                         reference_rc=proven['rc'],matched=equal,accepted=rc==0 and proven['rc']==0 and all(equal.values()),
                         hotspot=highest,pause_streak_slots=streaks,
                         wire_prefix_sha256=sha(raw) if raw.is_file() else None,command=list(map(str,cmd)))
                save(work/'receipt.json',row)
                rows.append(row)
                print('gpu',label,arm,epoch,row['accepted'],highest,flush=True)
        formal={arm:[x['wall_ns']/1e9 for x in rows if x['arm']==arm and x['sample']!='diagnostic' and x['accepted']]
                for arm in ('safe','serial')}
        records[label]=dict(rows=rows,formal_median_s={arm:median(v) if len(v)==3 else None for arm,v in formal.items()})
        save(out/'summary.json',records)

def main():
    out=Path(sys.argv[1]).resolve()
    if len(sys.argv)>2:
        return measure(out,Path(sys.argv[2]))
    out.mkdir()
    builds=out/'build-receipts'
    builds.mkdir()
    binary=build(builds,CANDIDATE,'CUDA')/'twins-ai'
    cases=out/'measurements'
    freeze(cases)
    reference(cases)
    return subprocess.run(['flock',LOCK,sys.executable,OBSERVER,out/'resources',
        sys.executable,__file__,cases,binary]).returncode

if __name__=='__main__':
    raise SystemExit(main())
