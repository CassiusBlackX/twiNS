"""Co-run bounded 16k validation after the first incast hits the queue cap."""

from concurrent.futures import ThreadPoolExecutor
import csv
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
from time import monotonic,sleep

from crosspod import LOCK,OBSERVER,ROOT,sha
from pfc_sustained import save
import rdma_scale_compact_reference as ref
import rdma_scale_gpu as cuda


OLD_REF=ROOT/'rdma_scale_compact_reference/20261125T160521Z'
OLD_GPU=ROOT/'rdma_scale_gpu/20261125T155841Z'
EARLIER_FINISH=ROOT/'rdma_scale_finish/20261125T161822Z'
INPUT=ROOT/'rdma_scale_reference/20261125T155657Z/results/inputs/k40'
TRACES_NS=10_000_000


def stop_parent(root,module,descendants=False):
    args=[str(root/f'{module}.py'),str(root/'results')]
    found=[]
    for p in Path('/proc').glob('[0-9]*/cmdline'):
        try: values=p.read_bytes().decode(errors='replace').strip('\0').split('\0')
        except OSError: continue
        if len(values)==3 and values[1:]==args:
            found.append(int(p.parent.name))
    for pid in found:
        try: os.kill(pid,signal.SIGSTOP)
        except ProcessLookupError: continue
        if descendants:
            pending=[pid]
            for p in pending:
                try:
                    children=Path(f'/proc/{p}/task/{p}/children').read_text().split()
                    pending.extend(int(x) for x in children)
                except FileNotFoundError: pass
            for child in reversed(pending[1:]):
                try: os.kill(child,signal.SIGTERM)
                except ProcessLookupError: pass
        os.kill(pid,signal.SIGTERM)
        os.kill(pid,signal.SIGCONT)
    return found


def prepare(case):
    shutil.copytree(INPUT,case)
    old=json.loads((case/'input.json').read_text())
    config=case/'config.log'
    text,n=re.subn(r'(?m)^SIMULATOR_STOP_TIME\s+\S+','SIMULATOR_STOP_TIME 0.02',config.read_text())
    assert n==1
    config.write_text(text)
    inc=case/'incast.csv'
    with inc.open() as file:
        rows=list(csv.DictReader(file))
    assert len(rows)==320
    with inc.open('w') as file:
        w=csv.writer(file,lineterminator='\n')
        w.writerow(('src','dst','flow_size','start_ns'))
        for i,line in enumerate(rows):
            w.writerow((line['src'],line['dst'],48000,100000+300000*(i//16)))
    old.update(config_sha256=sha(config),stop_time_ns=20_000_000,tx_until_ns=TRACES_NS,
               adjusted_incast={'bytes_per_flow':48000, 'wave_spacing_ns':300000, 'flows_per_wave':16})
    old['traffic']['incast']['sha256']=sha(inc)
    save(case/'input.json',old)


def references(out):
    ref.BUILD=OLD_REF/'results/build'
    ref.TRACES_UNTIL=TRACES_NS
    # The k8 ordered-packet and k32 incast controls have already passed. Avoid serializing
    # the much smaller k40 inputs behind the 14.6 GB k32 throughput validation.
    control=OLD_REF/'results/checking-k32/incast/receipt.json'
    assert json.loads(control.read_text())['stock_fct_equal']
    for kind in ('incast','distributed'):
        ref.run(out/'reference',out/'input',kind,core=6,memory='36g')
    check=OLD_REF/'results/checking-k32/distributed/receipt.json'
    deadline=monotonic()+240
    while not check.is_file() and monotonic()<deadline:
        sleep(3)
    k32_alltoall_ready=check.is_file()
    matched=json.loads(check.read_text())['stock_fct_equal'] if k32_alltoall_ready else None
    stopped=stop_parent(OLD_REF,'rdma_scale_compact_reference')
    # Only retire the superseded unpaced cases, never the two frozen k32 checks.
    listing=subprocess.check_output(['docker','ps','--filter','name=twins-compact-hpcc','--format','{{.Names}}'],text=True)
    for name in listing.splitlines():
        mounts=subprocess.check_output(['docker','inspect','--format','{{json .Mounts}}',name],text=True)
        if str(OLD_REF/'results/reference-k40') in mounts or (not k32_alltoall_ready and
                 str(OLD_REF/'results/checking-k32/distributed') in mounts):
            subprocess.run(['docker','stop','-t','4',name],check=True,timeout=20)
    save(out/'reference-handoff.json',{'stopped_original_route_trials':stopped,'k32_distribution_matched':matched})


def measure(out):
    binary=OLD_GPU/'results/build/compiled/twins-rdma'
    for kind in ('incast','distributed'):
        for cap,stage in ((2048,'clean'),(1024,'clean'),(2048,'wire'),(1024,'wire')):
            target=out/'measurements'/kind/f'q{cap}-{stage}'
            subprocess.run(['flock',LOCK,sys.executable,OBSERVER,out/'resources'/kind/f'q{cap}-{stage}',
                            sys.executable,__file__,target,out/'input',binary,kind,str(cap),stage],check=True)


def main():
    out=Path(sys.argv[1]).resolve()
    if len(sys.argv)>2:
        case,binary=map(Path,sys.argv[2:4]);kind,cap,stage=sys.argv[4:7]
        cuda.TRACES_UNTIL=TRACES_NS
        return cuda.main_run(out,case,binary,kind,int(cap),stage=='wire')
    out.mkdir()
    prepare(out/'input')
    if out.parent!=EARLIER_FINISH:
        stop_parent(EARLIER_FINISH,'rdma_scale_finish',descendants=True)
    prior=OLD_GPU/'results/measurements/k40/incast/q2048-clean/receipt.json'
    assert json.loads(prior.read_text())['counts']['queue']>0
    retire=stop_parent(OLD_GPU,'rdma_scale_gpu',descendants=True)
    save(out/'gpu-handoff.json',{'stopped_unpaced_gpu_queue':retire,'old_receipt':str(prior),'old_receipt_sha256':sha(prior)})
    with ThreadPoolExecutor(max_workers=2) as work:
        cpu=work.submit(references,out)
        measure(out)
        cpu.result()


if __name__=='__main__':
    main()
