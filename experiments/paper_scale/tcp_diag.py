"""GPU memory and history diagnostics, independent of the formal timing repetitions."""
import argparse
from pathlib import Path
import json
import subprocess
import shutil
import threading
import time
from scale import run
from reference_hashes import signature

p=argparse.ArgumentParser()
for arg in ('inputs','out','release','profile','reference'):p.add_argument(arg,type=Path)
p.add_argument('--hosts',type=int,default=1024)
p.add_argument('--passes-only',action='store_true')
a=p.parse_args()
inputs=a.inputs.resolve()/str(a.hosts)
a.out=a.out.resolve()
a.out.mkdir(parents=True,exist_ok=True)
def gpu_memory(stop,rows):
    vram=None if shutil.which('nvidia-smi') else next(iter(sorted(Path('/sys/class/drm').glob('card*/device/mem_info_vram_used'))),None)
    while not stop.is_set():
        if vram:rows.append((time.monotonic_ns(),int(vram.read_text())))
        else:
            mem=subprocess.check_output(['nvidia-smi','--query-gpu=memory.used','--format=csv,noheader,nounits'],text=True).splitlines()
            rows.append((time.monotonic_ns(),int(mem[0])*1024**2))
        stop.wait(.1)
def case(name,exe,mode,flow,extra,truth):
    d=a.out/name;d.mkdir()
    samples=[];stop=threading.Event()
    thread=threading.Thread(target=gpu_memory,args=(stop,samples));thread.start()
    try:
        rec=run([exe,'--topo=graph',f'--topofile={inputs}/{mode}.graph','--tcp=dctcp','--ecnK=30',f'--flows={inputs}/{flow}/flows.csv','--out=gpu.fct',*extra],d,'gpu',{},1800)
    finally:stop.set();thread.join()
    (d/'memory.json').write_text(json.dumps(dict(unit='device-wide bytes, sampled',samples=samples,peak=max((v for _,v in samples),default=None))))
    if not samples:raise RuntimeError('no GPU memory samples: '+name)
    if rec['rc'] and not (name.endswith('narrow-histogram') or '-pass-' in name):raise RuntimeError(name)
    return truth is not None and signature(d/'gpu.fct')==truth
release=a.release.resolve();profile=a.profile.resolve()
ref=a.reference.resolve()
hashes=json.loads(ref.read_text()) if ref.is_file() else {}
for mode in ('uniform','heterogeneous'):
    rel=f'{mode}/{a.hosts}/reference.fct'
    truth=hashes.get('cpu-scale/'+rel) if hashes else signature(ref/rel)
    for label,exe in [('3','twins-links'),('7','twins128-links'),('15','twins256-links'),('31','twins512-links')]:
        exact=case(mode+'-pass-'+label,release/exe,mode,'tcp',['--single-pass'],truth)
        (a.out/(mode+'-pass-'+label)/'equal.json').write_text(json.dumps(dict(six_fields_equal=exact)))
        if label=='31' and not exact:raise ValueError('wide release replay')
    if a.passes_only:continue
    for start in (64,512):
        name=f'{mode}-start-{start}'
        if not case(name,release/'twins',mode,'tcp',[f'--start-history={start}'],truth):raise ValueError(name)
    case(mode+'-narrow-histogram',profile/'twins-links',mode,'tcp',['--single-pass'],None)
    name=mode+'-histogram'
    if not case(name,profile/'twins512-links',mode,'tcp',['--single-pass'],truth):raise ValueError(name)
if a.passes_only:raise SystemExit(0)
holdout=a.out/'holdout-start-64'
case(holdout.name,release/'twins','heterogeneous','tcp-holdout',['--start-history=64'],None)
if not case('holdout-start-512',release/'twins','heterogeneous','tcp-holdout',['--start-history=512'],signature(holdout/'gpu.fct')):raise ValueError('holdout')
