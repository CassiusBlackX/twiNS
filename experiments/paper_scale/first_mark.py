"""Find the earliest observed changed ECN decision on the same packet at the same queue/time."""
import csv,gzip,itertools,json,sys
from collections import Counter,defaultdict
from pathlib import Path
r=Path(sys.argv[1]); dirs=[r/'trace'/str(i) for i in (0,1)]
def groups(path):
    def records():
        with gzip.open(path,'rt') as f:
            for l in f:
                if l.startswith('Q '):
                    xs=l.split()
                    if len(xs)!=11 or xs[-2]!='ecn':raise ValueError(xs)
                    yield int(xs[1]),tuple(xs[2:8]),int(xs[8]),int(xs[10])
    for ts,it in itertools.groupby(records(),lambda x:x[0]):yield ts,list(it)
def main():
    it=[iter(groups(d/'events.gz')) for d in dirs];cur=[next(i,None) for i in it];first_changed=None;first_mark=None
    while all(cur):
        ts=min(v[0] for v in cur)
        buckets=[v[1] if v[0]==ts else [] for v in cur]
        if not first_changed and Counter(x[1] for x in buckets[0])!=Counter(x[1] for x in buckets[1]):
            first_changed=dict(time_ps=ts,original=buckets[0][:20],perturbed=buckets[1][:20])
        marks=[]
        for rows in buckets:
            m=defaultdict(list)
            for _,key,qlen,ecn in rows:m[key].append((qlen,ecn))
            marks.append(m)
        for key in sorted(marks[0].keys() & marks[1].keys()):
            l,h=marks[0][key],marks[1][key]
            if len(l)==len(h)==1 and (l[0][1]==3)!=(h[0][1]==3):
                first_mark=dict(time_ps=ts,device_and_packet=key,original=l,perturbed=h,simultaneous_queue_events=[[x for x in b if x[1][:2]==key[:2]] for b in buckets]);break
        if first_mark:break
        for j in range(2):
            if cur[j][0]==ts:cur[j]=next(it[j],None)
    report=dict(first_changed_enqueue_multiset=first_changed,first_same_packet_CE_difference=first_mark)
    for i,d in enumerate(dirs):
        with (d/'flows.csv').open() as f:
            rows=list(csv.DictReader(f))
        short=sorted(int(x['completed_ns'])-int(x['start_ns']) for x in rows if int(x['flow_size'])<100000)
        report['p99_'+str(i)]=short[int(.99*(len(short)-1)+.5)]
    (r/'trace/decision.json').write_text(json.dumps(report,indent=2))
main()
