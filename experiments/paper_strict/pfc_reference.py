"""Run native sequential SimAI at the three generated DCN+ burst sizes."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

HOME=Path.home()/'Videos'
MODEL=HOME/'twins-simai-reference-20261122/20261122T094910Z/work/patch-uid64'
DATA=HOME/'netsim/aitime'
ORIGINAL=HOME/'twins-release-cuda-20261122/project/paper-inputs'
CANDIDATE=HOME/'twins-paper-followup-20261123/candidate'
BASE=HOME/'twins-paper-evidence-20261116'
SIM='astra-sim-alibabacloud/extern/network_backend/ns3-interface/simulation'
LOCK=HOME/'twins-isolated-20261118/out/exclusive-campaign.lock'

def checksum(path):
    with path.open('rb') as f:
        return hashlib.file_digest(f,'sha256').hexdigest()

def main():
    out=Path(sys.argv[1])
    out.mkdir(parents=True)
    if len(sys.argv)==2:
        # These untimed references run during GPU campaigns only after the 70 GiB CPU audit.
        audit=HOME/'twins-paper-strict-20261123/cpu_provenance'
        deadline=time.monotonic()+14000
        while time.monotonic()<deadline:
            dirs=sorted(audit.glob('20*'))
            if dirs and (dirs[-1]/'results/timing/timing-summary.json').exists():
                break
            time.sleep(30)
        return subprocess.run(['flock',str(LOCK),sys.executable,__file__,str(out/'measurements'),'run']).returncode
    source_workload=ORIGINAL/'out/simai/wl/paper/c_alltoall_64m_128.txt'
    topology=next(DATA.glob('topo/DCN+SingleToR_128g*200Gbps*A100*'))
    binary=MODEL/SIM/'trial-build/scratch/ns3.36.1-AstraSimNetwork-optimized'
    extractor=next(p for p in (Path(__file__).with_name('trace2dag.py'), DATA/'trace2dag.py', BASE/'workloads/trace2dag.py') if p.is_file())
    rows=[]
    for mb in (8,64,256):
        case=out/f'dcn-burst-{mb}m'
        case.mkdir()
        raw=out.parent.parent/'raw'/case.name
        (raw/'etc/simulation').mkdir(parents=True)
        (raw/'etc/results').mkdir()
        cfg=case/'config.log'
        cfg.write_bytes((CANDIDATE/'validation/simai/SimAI.conf').read_bytes())
        wl=case/'workload.log'
        wl.write_text(source_workload.read_text().replace('67108864',str(mb*1024**2)))
        name=f'twins-strict-pfc-ref-{out.parent.parent.name.lower()}-{mb}'
        command=['docker','run','--rm','--name',name,'--network=none','--ulimit','core=0',
            '--user',f'{os.getuid()}:{os.getgid()}','--cpuset-cpus','4','--memory=20g','--memory-swap=20g',
            '-e','AS_SEND_LAT=3','-e','TWINS_TRACE=/raw/events.trace',
            '-e','TWINS_TXTRACE=/raw/wire.trace','-e','TWINS_TXUNTIL=400000',
            '-v',f'{MODEL}:/simai:ro','-v',f'{DATA}:/data:ro','-v',f'{raw}:/raw',
            '-v',f'{raw}/etc:/etc/astra-sim','-v',f'{case}:/case:ro','-w','/raw',
            'localhost/simai-build',f'/simai/{SIM}/trial-build/scratch/ns3.36.1-AstraSimNetwork-optimized',
            '-t','1','-w','/case/workload.log','-n',f'/data/topo/{topology.name}','-c','/case/config.log']
        start=time.monotonic_ns()
        with (case/'process.log').open('wb') as stream:
            try:
                rc=subprocess.run(command,stdout=stream,stderr=subprocess.STDOUT,timeout=3600).returncode
            except subprocess.TimeoutExpired:
                subprocess.run(['docker','stop','-t','5',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                rc=124
            elapsed=time.monotonic_ns()-start
        fct=raw/'etc/simulation/llama_hpn7_fct.txt'
        events=raw/'events.trace'
        dag=case/'actions.dag'
        messages=case/'messages.fct'
        extract_status=None
        if fct.exists() and events.exists():
            with (case/'extract.log').open('wb') as log:
                extract_status=subprocess.run([sys.executable,extractor,events,fct,dag,messages],
                                              stdout=log,stderr=subprocess.STDOUT).returncode
        pfc=raw/'etc/simulation/llama_hpn7_pfc.txt'
        if pfc.is_file():
            shutil.copyfile(pfc,case/'pfc.log')
        row=dict(megabytes=mb,rc=rc,extract_rc=extract_status,elapsed_ns=elapsed,command=command,
                 sha256={str(p):checksum(p) for p in (topology,binary,wl,cfg,dag,messages) if p.is_file()},
                 wire_prefix_sha256=checksum(raw/'wire.trace') if (raw/'wire.trace').exists() else None,
                 pfc_records=sum(1 for _ in pfc.open()) if pfc.is_file() else None)
        (case/'receipt.json').write_text(json.dumps(row,indent=2)+'\n')
        rows.append(row)
        print(mb,rc,extract_status,row['pfc_records'],flush=True)
    (out/'summary.json').write_text(json.dumps(rows,indent=2)+'\n')
    return 0

if __name__=='__main__':
    raise SystemExit(main())
