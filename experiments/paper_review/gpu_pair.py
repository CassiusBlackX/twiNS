"""Stage and measure a common, single-path Reno-family control at two host scales."""
import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import re
from statistics import median
import subprocess
import time


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def freeze(out, route_reader):
    for k in (8,16):
        case=out/f'k{k}'
        case.mkdir(parents=True)
        half=k//2
        switches=k*half
        hosts=switches*half
        config=f'''seed = 51001
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
        def day_host(n):
            return n//half+(n%half)*switches
        with (case/'twins.csv').open('w') as t, (case/'gedes.csv').open('w') as g:
            twin,gedes=csv.writer(t),csv.writer(g)
            twin.writerow(['src','dst','flow_size','start_ns'])
            gedes.writerow(['node','flow_size','start_ns'])
            for src in range(hosts):
                dst=src^1
                assert src//half==dst//half
                start=1000+2000*(src//half)
                twin.writerow((src,dst,46720,start))
                gedes.writerow((src,46720,start))
                config+=f'''
[[flow]]
flow_type = "TCP"
graph = [[{day_host(src)}, {day_host(dst)}]]
[flow.traffic]
initial_delay = {start/1e9:.9f}
size = 46720
arr_dist = {{type = "Uniform", low = 1.0, high = 1.0}}
pkt_size_dist = {{type = "DiscreteUniform", low = 1460, high = 1460}}
[flow.traffic.tcp]
cc_algorithm = "TCPReno"
'''
        paths=case/'days-routes.csv'
        toml=case/'days.toml'
        toml.write_text(config)
        with paths.open('wb') as stream:
            subprocess.run([route_reader,toml],stdout=stream,check=True)
        topology_ordered=set()
        for h in range(hosts):
            switch=hosts+h%switches
            topology_ordered.update(((h,switch),(switch,h)))
        for edge in range(switches):
            for group in range(half):
                pair=(hosts+edge, hosts+switches+(edge//half)*half+group)
                topology_ordered.update((pair,pair[::-1]))
        for agg in range(switches):
            for ordinal in range(half):
                pair=(hosts+switches+agg, hosts+2*switches+(agg%half)*half+ordinal)
                topology_ordered.update((pair,pair[::-1]))
        links=sorted(topology_ordered)
        routes=list(csv.DictReader(paths.open()))
        assert len(routes)==hosts
        for row in routes:
            source,target=int(row['source']),int(row['target'])
            assert source%switches==target%switches
            middle=hosts+source%switches
            for direction,a,b in [('forward',source,target),('reverse',target,source)]:
                physical=[links[int(v)] for v in row[direction].split()]
                assert physical == [(a,middle),(middle,b)],(row,direction,physical)
        (case/'route-check.json').write_text(json.dumps({
            'hosts':hosts,'bidirectional_routes_verified':2*hosts,'hops':2,
            'rate_bps':100_000_000_000,'propagation_ns':1000,
            'scope':'Within-leaf exchanges have a unique two-link physical route. Days host IDs were remapped to the other engines’ wiring.',
        },indent=2)+'\n')


def run(args):
    args.out.mkdir(parents=True,exist_ok=False)
    data=[]
    binary={'twins':args.twins,'gedes':args.gedes,'days':args.days}
    for k in (8,16):
        case=args.inputs/f'k{k}'
        count=k**3//4
        for iteration,order in enumerate((('twins','gedes','days'),('gedes','days','twins'),('days','twins','gedes'))):
            for engine in order:
                dest=args.out/f'k{k}'/f'trial-{iteration}'/engine
                dest.mkdir(parents=True)
                command={
                    'twins':[str(args.twins),f'--k={k}','--tcp=newreno','--queue=200','--ecnK=200',
                             '--max_slots=10000',f'--flows={case}/twins.csv','--out=fct.csv'],
                    'gedes':[str(args.gedes),f'--ft_k={k}',f'--flow_file={case}/gedes.csv',
                             '--max_slots=10000','--output=fct.csv'],
                    'days':[str(args.days),str(case/'days.toml'),'--engine','cuda'],
                }[engine]
                env={**os.environ,'GEDES_PAIRED_LEAF':'1'}
                start=time.monotonic_ns()
                with (dest/'process.log').open('w') as stream:
                    try:
                        rc=subprocess.run(command,cwd=dest,env=env,stdout=stream,stderr=subprocess.STDOUT,timeout=240).returncode
                    except subprocess.TimeoutExpired:
                        rc=124
                ns=time.monotonic_ns()-start
                log=(dest/'process.log').read_text()
                row=dict(engine=engine,k=k,hosts=count,trial=iteration,command=command,rc=rc,wall_ns=ns,
                         binary_sha256=digest(binary[engine]),accepted=False)
                if engine=='days':
                    result=next((line for line in log.splitlines() if line.startswith('record=days_result')), '')
                    metrics={key:int(val) for key,val in re.findall(r'\b(\w+)=(\d+)\b',result)}
                    device=next((line for line in log.splitlines() if line.startswith('record=days_device')), '')
                    row.update(metrics=metrics,device_metrics={key:int(val) for key,val in re.findall(r'\b(\w+)=(\d+)\b',device)})
                    row['accepted']=rc==0 and metrics.get('pending_events')==0 and metrics.get('received_packets')==32*count and metrics.get('dropped_packets')==0
                elif (dest/'fct.csv').exists():
                    actual=list(csv.DictReader((dest/'fct.csv').open()))
                    key='completed_ns' if engine=='twins' else 'completed_timestamp'
                    finished=[int(x[key]) for x in actual if int(x[key])>=0]
                    loop=re.search(r'Simulation Duration: ([\d.]+)s',log)
                    slots=re.search(r'Executed Slots: (\d+)',log)
                    row.update(completed=len(finished),max_completed_ns=max(finished,default=None),
                               event_loop_seconds=float(loop[1]) if loop else None,
                               steps=int(slots[1]) if slots else None)
                    row['accepted']=rc==0 and len(finished)==len(actual)==count and max(finished)<10_000_000
                (dest/'receipt.json').write_text(json.dumps(row,indent=2)+'\n')
                data.append(row)
    medians={}
    for k in (8,16):
        for engine in binary:
            trials=[r for r in data if (r['k'],r['engine'])==(k,engine)]
            if all(r['accepted'] for r in trials):
                medians[f'k{k}/{engine}']=median(r['wall_ns'] for r in trials)
    (args.out/'summary.json').write_text(json.dumps(dict(rows=data,median_ns=medians,
        scope='Reno-family, 10ms cap, one 32-segment flow per host; physical links and offered traffic matched; native packet and stop semantics retained.'),indent=2)+'\n')
    raise SystemExit(0 if all(row['accepted'] for row in data) else 1)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    commands=parser.add_subparsers(dest='phase',required=True)
    f=commands.add_parser('freeze')
    f.add_argument('out',type=lambda p:Path(p).resolve())
    f.add_argument('route_reader',type=lambda p:Path(p).resolve())
    b=commands.add_parser('run')
    for arg in ('inputs','out','twins','gedes','days'):
        b.add_argument(arg,type=lambda p:Path(p).resolve())
    options=parser.parse_args()
    if options.phase=='freeze':
        freeze(options.out,options.route_reader)
    else:
        run(options)
