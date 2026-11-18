"""Build the evidence summary from closed inputs and receipts. Run on the CPU analysis host."""
import argparse
from collections import Counter
import csv,json,re,sys
from pathlib import Path
from statistics import median
from ai_compare import compare

def flowids(path):
    with path.open() as stream:
        rows=list(csv.DictReader(stream))
    send,recv=Counter(),Counter()
    ids={}
    for i,r in sorted(enumerate(rows),key=lambda x:int(x[1]['start_ns'])):
        a,b,size,t=[int(r[k]) for k in ('src','dst','flow_size','start_ns')]
        ids[a,b,10000+send[a],100+recv[b],size,t]=i
        send[a]+=1;recv[b]+=1
    return ids,len(recv)

def irn_case(flow,cpu,gpu):
    mapping,receivers=flowids(flow)
    truth={}
    with (cpu/'reference.fct').open() as f:
        for line in f:
            x=list(map(int,line.split()));i=mapping[tuple(x[:6])]
            if i in truth:raise ValueError('duplicate CPU flow')
            truth[i]=[x[5],x[6]]
    actual={}
    for line in (gpu/'gpu.fct').read_text().splitlines():
        i,t,d=map(int,line.split())
        if i in actual:raise ValueError('duplicate GPU flow')
        actual[i]=[t,d]
    log=(gpu/'gpu.log').read_text()
    terminal=re.search(r'undecided ties (\d+), on lost tails (\d+) \| overflows: tx (\d+) queue (\d+) ackq (\d+) qps (\d+) due (\d+)',log)
    metrics=re.search(r'drops (\d+) marks (\d+) pauses (\d+) resumes (\d+) nacks (\d+)',log)
    recovery=re.search(r'IRN selective feedback (\d+), retransmitted packets (\d+), timeouts (\d+)',log)
    n=sum(r==actual.get(i) for i,r in truth.items())
    first=next((dict(id=i,reference=truth.get(i),gpu=actual.get(i)) for i in sorted(set(truth)|set(actual)) if truth.get(i)!=actual.get(i)),None)
    return dict(input_flows=len(mapping),active_receivers=receivers,reference_completed=len(truth),gpu_completed=len(actual),matched=n,first_difference=first,
                accepted=len(mapping)==len(truth)==len(actual)==n and all(int(k)==0 for k in terminal.groups()),flags=list(map(int,terminal.groups())),
                gpu_drops=int(metrics[1]),gpu_marks=int(metrics[2]),gpu_sacks=int(recovery[1]),gpu_retransmitted=int(recovery[2]),gpu_timeouts=int(recovery[3]))

def main():
    p=argparse.ArgumentParser();p.add_argument('root',type=Path);a=p.parse_args();r=a.root
    out={'original_irn':{},'dcqcn_irn':{},'ai':{},'ecn':[],'tcp':{}}
    for group,base,cpu,gpu in [('original_irn',r/'inputs',r/'a87/cpu-scale',r/'gpu-only'),('dcqcn_irn',r/'dcqcn/inputs',r/'dcqcn/cpu-scale',r/'dcqcn/rx')]:
        for h in [128,256,512,1024]:
            for name in ['spread','incast']:
                d=cpu/name/str(h)
                if (d/'receipt.json').is_file():out[group][f'{name}-{h}']=irn_case(base/str(h)/name/'flows.csv',d,gpu/name/str(h))
    for case in ['clean','loss','large-buffer','small-buffer']:
        out['ai'][case]=compare(r/('ai-cap' if case.endswith('buffer') else 'ai')/case,r/'a87/ai-v5'/case)
    for entry in json.loads((r/'a87/receipts/result.json').read_text()):
        delta=[row['delta_ns'] for row in entry['pairs']]
        out['ecn'].append(dict(order=entry['order'],crossings=entry['crossings'],ranking=Counter(row['rank_status'] for row in entry['pairs']),delta_ns=sorted(delta),median_ns=median(delta)))
    for d in (r/'tcp-history').iterdir():
        if not (d/'gpu.json').exists():continue
        log=(d/'gpu.log').read_text()
        out['tcp'][d.name]=dict(wall_ns=json.loads((d/'gpu.json').read_text())['wall_ns'],peak_sampled_bytes=json.loads((d/'memory.json').read_text())['peak'],passes=[l for l in log.splitlines() if l.startswith(('TCP pass','TCP compared runs','TCP arrival ring'))])
    (r/'summary.json').write_text(json.dumps(out,indent=2))
if __name__=='__main__':main()
