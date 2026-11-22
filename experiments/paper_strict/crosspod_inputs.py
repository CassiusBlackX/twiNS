"""Freeze pod-paired workload candidates and decode the actual Days routing image."""
import csv
import hashlib
import json
from pathlib import Path
import subprocess
import sys

CONFIG = '''seed = 51002
duration = 0.01
threading = "single"
log_path = "logs"
[topology]
category = "FatTree"
[topology.fat_tree]
k = {k}
hosts_per_edge = {half}
[switch]
port_rate = 100000000000
capacity = 200
weights = [1]
discipline = "FIFO"
drop = "TailDrop"
[link]
propagation_ns = 1000
[routing]
policy = "ShortestPath"
'''

def freeze(root, reader, k, label, rounds, segments, pacing_ns=0, cap_ns=10_000_000):
    case = root / f'k{k}-{label}'
    case.mkdir(parents=True)
    half = k//2
    edge = k*half
    hosts = edge*half
    def day_host(node):
        return node//half+(node%half)*edge
    records=[]
    config=[CONFIG.format(k=k,half=half).replace('duration = 0.01', f'duration = {cap_ns/1e9}')]
    for source in range(hosts//2):
        target = source ^ (hosts//2)
        for repeat in range(rounds):
            # The first arm staggers racks. The second aligns paired racks in bursts.
            if pacing_ns:
                start = 1000 + (repeat*(hosts//2) + source)*pacing_ns
            else:
                start = 1000 + repeat * 75000 + (300*(source//half) if label=='permutation' else 200*((source//half)%2))
            size = 1460*segments
            records.append((source,target,size,start,source))
            config.append(f'''
[[flow]]
flow_type = "TCP"
graph = [[{day_host(source)}, {day_host(target)}]]
[flow.traffic]
initial_delay = {start/1e9:.9f}
size = {size}
arr_dist = {{type = "Uniform", low = 1.0, high = 1.0}}
pkt_size_dist = {{type = "DiscreteUniform", low = 1460, high = 1460}}
[flow.traffic.tcp]
cc_algorithm = "TCPReno"
''')
    with (case/'twins.csv').open('w') as stream:
        writer=csv.writer(stream)
        writer.writerow(('src','dst','flow_size','start_ns','conn'))
        writer.writerows(records)
    with (case/'gedes.csv').open('w') as stream:
        writer=csv.writer(stream)
        writer.writerow(('node','flow_size','start_ns'))
        writer.writerows((src,size,start) for src,_,size,start,_ in records)
    toml=case/'days.toml'
    toml.write_text(''.join(config))
    raw=case/'days-route-ids.csv'
    with raw.open('wb') as stream:
        subprocess.run([reader,toml],stdout=stream,check=True)
    graph=set()
    def connect(a,b):
        graph.update(((a,b),(b,a)))
    for h in range(hosts):
        connect(h,hosts+h%edge)
    for i in range(edge):
        for j in range(half):
            connect(hosts+i,hosts+edge+(i//half)*half+j)
    for i in range(edge):
        for j in range(half):
            connect(hosts+edge+i,hosts+2*edge+(i%half)*half+j)
    links=sorted(graph)
    paths={}
    for row in csv.DictReader(raw.open()):
        a,b=int(row['source']),int(row['target'])
        for src,dst,kind in ((a,b,'forward'),(b,a,'reverse')):
            path=tuple(links[int(index)] for index in row[kind].split())
            assert len(path)==6 and path[0][0]==src and path[-1][1]==dst
            assert all(x[1]==y[0] for x,y in zip(path,path[1:]))
            paths.setdefault((src,dst),set()).add(path)
    assert len(paths)==hosts and all(len(value)==1 for value in paths.values())
    port={}
    for (src,dst),values in paths.items():
        route=next(iter(values))
        ordinal=(route[1][1]-hosts-edge)%half
        core=(route[2][1]-hosts-2*edge)
        assert core//half==ordinal
        port[src]=(ordinal,core%half)
    # A two-column per-source map supplies the upstream ports for the edge and aggregation layers.
    # This describes the observed Days path, without replacing its routing policy.
    with (case/'routes.csv').open('w') as stream:
        writer=csv.writer(stream)
        writer.writerow(('src','edge_uplink','aggregation_uplink'))
        writer.writerows((node,*port[day_host(node)]) for node in range(hosts))
    (case/'input.json').write_text(json.dumps(dict(
        k=k, hosts=hosts, label=label, rounds=rounds, segments_per_flow=segments,
        cap_ns=cap_ns,pacing_ns=pacing_ns,
        flow_rows=len(records),payload_bytes=sum(r[2] for r in records),path_hops=6,
        unique_directional_paths=len(paths),
        upstream_port_pairs=sorted(set(port.values())),
        input_sha256={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in case.iterdir()},
    ),indent=2)+'\n')

if __name__=='__main__':
    output=Path(sys.argv[1]).resolve()
    output.mkdir(parents=True,exist_ok=True)
    videos=Path.home()/'Videos'
    rust=videos/'twins-days-probe-20261122/toolchain/bin/rustc'
    deps=videos/'days/target/release/deps'
    reader=output/'days-routes'
    source=Path(__file__).with_name('days_pair_routes.rs')
    command=[rust,'--edition=2021','-O',source,'-o',reader,'-L',f'dependency={deps}',
             '--extern',f'days={deps}/libdays-27a621a024f738d1.rlib']
    with (output/'route-build.log').open('wb') as log:
        subprocess.run(command,stdout=log,stderr=subprocess.STDOUT,check=True)
    for k in (8,16):
        if len(sys.argv)>2:
            freeze(output,reader,k,'paced',4,128,20000,100_000_000)
        else:
            freeze(output,reader,k,'permutation',1,128)
            freeze(output,reader,k,'overlap',4,128)
