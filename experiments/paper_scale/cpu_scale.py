"""CPU-only scale references on a87, one isolated scratch directory per case."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import argparse,json,os,shutil,subprocess,time
def main():
    p=argparse.ArgumentParser()
    for name in ('root','tcp_ref','irn_ref'):p.add_argument(name,type=Path)
    p.add_argument('--modes',nargs='+',default=['heterogeneous','uniform','spread','incast'])
    p.add_argument('--workers',type=int,default=3)
    a=p.parse_args();r=a.root.resolve()
    def case(v):
        mode,h=v
        d=r/'cpu-scale'/mode/str(h);d.mkdir(parents=True,exist_ok=True)
        if (d/'receipt.json').exists():return
        src=r/'inputs'/str(h);tcp=mode in ('heterogeneous','uniform')
        if tcp:
            cmd=[a.tcp_ref.resolve(),'--topo=file','--hostq=red','--tcp=ns3::TcpDctcp','--flowEcmp=1','--stop=0.1',f'--flows={src}/tcp/flows.csv',f'--topofile={src}/{mode}.graph','--out=reference.fct']
        else:
            for n in ('config','ref.flows'):shutil.copyfile(src/mode/n,d/n)
            (d/'config').write_text((d/'config').read_text().replace('../fabric.txt',str(src/'fabric.txt')))
            cmd=[a.irn_ref.resolve(),'config']
        start=time.monotonic_ns()
        with (d/'process.log').open('w') as log:
            try:rc=subprocess.run(cmd,cwd=d,env={**os.environ,'IRN_BDP_BYTES':'156000'},stdout=log,stderr=subprocess.STDOUT,timeout=7200).returncode
            except subprocess.TimeoutExpired:rc=124
        dest=d/('receipt.json' if rc==0 else 'failure.json')
        dest.write_text(json.dumps(dict(command=list(map(str,cmd)),rc=rc,wall_ns=time.monotonic_ns()-start),indent=2))
    with ThreadPoolExecutor(a.workers) as exe:list(exe.map(case,((m,h) for h in (128,256,512,1024) for m in a.modes)))
if __name__=='__main__':main()
