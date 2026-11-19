"""Generate immutable paired scale inputs from a common physical link list."""
import argparse
import csv
import hashlib
import json
import random
import shutil
from pathlib import Path

def topology(hosts):
    pods = hosts // 64
    leaves = pods * 8
    switches = leaves * 2 + 64
    edges = [(h, hosts + h // 8) for h in range(hosts)]
    for p in range(pods):
        for i in range(8):
            for j in range(8):
                edges.append((hosts + 8*p+i, hosts + leaves+8*p+j))
                edges.append((hosts + leaves+8*p+i, hosts + 2*leaves+8*i+j))
    return switches, edges

def generate(root, hosts, template, *, irn_seed=101):
    d = root / str(hosts)
    d.mkdir()
    switches, edges = topology(hosts)
    def node(n): return f'h{n}' if n < hosts else f's{n-hosts}'
    graphs = {}
    for mode in ('uniform', 'heterogeneous'):
        rows = [f'hosts {hosts}', f'switches {switches}']
        for a,b in edges:
            access = a < hosts
            rate,delay = (25,500) if mode=='heterogeneous' and access else (100,2000 if mode=='heterogeneous' else 1000)
            rows.append(f'link {node(a)} {node(b)} {rate*10**9} {delay*1000}')
        (d/(mode+'.graph')).write_text('\n'.join(rows)+'\n')
    (d/'fabric.txt').write_text(f'{hosts+switches} {switches} {len(edges)}\n'+' '.join(map(str,range(hosts,hosts+switches)))+'\n'+''.join(f'{a} {b} 100Gbps 1000ns 0\n' for a,b in edges))
    for layout in ('tcp','tcp-holdout','spread','incast'):
        dst=d/layout;dst.mkdir()
        seed = 102 if layout == 'tcp-holdout' else 101 if layout == 'tcp' else irn_seed
        rng=random.Random(seed)
        rows=[]
        for t in range(10):
            shift=rng.randrange(1,hosts//8)*8
            for h in range(hosts):
                target=(h+shift)%hosts
                if layout=='incast':target=(target//8)*8+t%8
                tcp=layout.startswith('tcp')
                rows.append((h,target,32768 if tcp else 150000,(1000 if tcp else 2000000000)+t*200000))
        with (dst/'flows.csv').open('w',newline='') as f:
            w=csv.writer(f);w.writerow(('src','dst','flow_size','start_ns'));w.writerows(rows)
        if not layout.startswith('tcp'):
            (dst/'ref.flows').write_text(str(len(rows))+'\n'+''.join(f'{a} {b} 3 {sz} {ts//10**9}.{ts%10**9:09d}\n' for a,b,sz,ts in rows))
            config=template.read_text()
            config=config.replace('../leaf_spine_128_100G_OS2.txt','../fabric.txt').replace('FLOWGEN_STOP_TIME 2.002','FLOWGEN_STOP_TIME 2.1')
            (dst/'config').write_text(config)
    return {'hosts':hosts,'switches':switches,'links':len(edges),'flows':10*hosts,'sample_tx_ports':[[0,1],[hosts,9],[hosts+hosts//8,9],[hosts+switches-1,1]]}

def main():
    p=argparse.ArgumentParser();p.add_argument('out',type=Path);p.add_argument('template',type=Path);p.add_argument('traces',type=Path);a=p.parse_args()
    a.out.mkdir(parents=True)
    sizes=[generate(a.out,h,a.template) for h in (128,256,512,1024)]
    e=a.out/'ecn';e.mkdir()
    for i in range(1,21):shutil.copyfile(a.traces/f'ws30_s{i}.csv',e/f'{i}.csv')
    hashes={str(f.relative_to(a.out)):hashlib.sha256(f.read_bytes()).hexdigest() for f in sorted(a.out.rglob('*')) if f.is_file()}
    (a.out/'manifest.json').write_text(json.dumps(dict(sizes=sizes,sha256=hashes,tie_seeds=list(range(6)),K=[10,20,30,40],diagnostic_trajectory=1,tcp_retry_fraction=.2,irn_threshold=.5),indent=2))
if __name__=='__main__':main()
