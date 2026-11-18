"""Compare each physical device's sends, and compute unions using a shared device clock."""
import argparse
from collections import Counter, defaultdict
from itertools import zip_longest
import json
from pathlib import Path


def wire(path, ports):
    rows={port:[] for port in ports}
    with path.open() as stream:
        for line in stream:
            row=line.split()
            if len(row)!=14:raise ValueError((path,row))
            rows[int(row[1]),int(row[2])].append(row)
    # GPU blocks publish records in parallel. Within a single link the serializer
    # has strictly positive transmission duration, so its time order is unique.
    for link in rows.values():
        link.sort(key=lambda x:int(x[0]))
        if any(a[0]==b[0] for a,b in zip(link,link[1:])):raise ValueError('two sends at the same time on one link')
    return rows


def profile(path):
    slots=defaultdict(list)
    active=multi=0
    switches=set()
    for line in path.read_text().splitlines():
        slot,node,sw,start,stop,s0,s1,ports=map(int,line.split())
        slots[slot].extend([(start,sw,1),(stop,sw,-1)])
        if s1>s0:
            if not start<=s0<s1<=stop:raise ValueError('section outside switch')
            slots[slot].extend([(s0,2,1),(s1,2,-1)])
        if sw and ports:
            active+=1
            multi+=ports>=2
            switches.add(node)
    ticks=Counter()
    for events in slots.values():
        last=min(x[0] for x in events)
        running=[0,0,0]
        for now,kind,delta in sorted(events):
            width=now-last
            if running[0] or running[1]:ticks['device_union']+=width
            if running[2]:
                ticks['serial_union']+=width
                if running[0]:ticks['also_endpoint']+=width
            running[kind]+=delta
            last=now
        if any(running):raise ValueError('open timing interval')
    return dict(sample_slots=len(slots),sample_active_switches=len(switches),active_switch_slots=active,multiple_active_ports=multi,
                multiple_fraction=multi/active if active else None,ticks=ticks,
                serial_fraction=ticks['serial_union']/ticks['device_union'] if ticks['device_union'] else None)


def main():
    p=argparse.ArgumentParser()
    for arg in ('gpu','reference','output'):p.add_argument(arg,type=Path)
    a=p.parse_args()
    ports=[tuple(map(int,x.split())) for x in (a.gpu/'ports').read_text().splitlines()]
    left=wire(a.reference/'reference.tx',ports);right=wire(a.gpu/'gpu.tx',ports)
    results={}
    for k in ports:
        x,y=left[k],right[k]
        first=next((dict(index=i,reference=r,gpu=g) for i,(r,g) in enumerate(zip_longest(x,y)) if r!=g),None)
        results[str(k)]=dict(reference=len(x),gpu=len(y),first_difference=first)
    out=dict(links=results,profile=profile(a.gpu/'device-intervals.txt'))
    a.output.write_text(json.dumps(out,indent=2))
if __name__=='__main__':main()
