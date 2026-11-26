"""Compare complete queue histories at the common last-flow picosecond."""

from collections import Counter, defaultdict, deque
import csv
import gzip
import hashlib
from itertools import zip_longest
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from cubic_qtrace import sha, save


FIELDS=('src','dst','flow_size','start_ns','established_ns','completed_ns')
HISTORICAL_CASE='k8_a2a30 cubic'


def csv_rows(path):
    with path.open() as stream:
        return [{k:int(v) for k,v in row.items()} for row in csv.DictReader(stream)]


def as_set(rows,fields):
    return Counter(tuple(row[x] for x in fields) for row in rows)


def fct(left,right,frozen,flows):
    offered=csv_rows(flows)
    ref,sim,old=map(csv_rows,(left,right,frozen))
    identity=FIELDS[:4]
    assert len(ref)==len(sim)==len(old)==len(offered)==273
    assert as_set(ref,identity)==as_set(sim,identity)==as_set(old,identity)==as_set(offered,identity)
    historical=as_set(old,tuple(old[0]))==as_set(ref,tuple(old[0]))
    six=as_set(ref,FIELDS)==as_set(sim,FIELDS)==as_set(old,FIELDS)
    pico=as_set(ref,(*FIELDS,'completed_ps'))==as_set(sim,(*FIELDS,'completed_ps'))
    assert all(r['completed_ps']>0 and r['completed_ps']//1000==r['completed_ns'] for r in ref+sim)
    ends={k:max(x['completed_ps'] for x in rows) for k,rows in (('ns3',ref),('twins',sim))}
    return {'historical_reference_full_record_equal':historical,'six_fields_equal':six,'all_completion_ps_equal':pico,
            'last_ps':ends,'terminal_flows':{k:[x for x in rows if x['completed_ps']==ends[k]]
                for k,rows in (('ns3',ref),('twins',sim))},'flows':len(ref)}


def prepare(raw, scratch, cutoff):
    clipped,sorted_=scratch/'window.raw',scratch/'window.sorted'
    inside=outside=at_boundary=0
    kinds=Counter()
    outside_bounds=[None,None]
    with gzip.open(raw,'rb') as stream,clipped.open('wb') as output:
        for n,row in enumerate(stream,1):
            fields=row.split()
            if fields[0]==b'Q': fields.pop(0)
            if len(fields)!=8 or fields[5] not in (b'D',b'A',b'S',b'SA'):
                raise ValueError(f'Unexpected trace line {n} in {raw}: {row!r}')
            for idx in (0,1,2,3,4,6,7):
                if not fields[idx].isdigit():
                    raise ValueError(f'Unexpected integer at line {n} in {raw}: {row!r}')
            stamp=int(fields[0])
            if stamp<=cutoff:
                inside+=1
                at_boundary+=stamp==cutoff
                output.write(b' '.join(fields)+b'\n')
            else:
                outside+=1
                kinds[fields[5].decode()]+=1
                if outside_bounds[0] is None:
                    outside_bounds=[stamp,stamp]
                else:
                    outside_bounds=[min(outside_bounds[0],stamp),max(outside_bounds[1],stamp)]
    subprocess.run(['sort','-s','--parallel=2','-S','256M','-T',scratch,'-k1,1n','-k2,2n','-k3,3n',clipped,'-o',sorted_],
                   env={**os.environ,'LC_ALL':'C'},check=True,timeout=180)
    return sorted_,{'trace_sha256':sha(raw),'full_rows':inside+outside,'window_rows':inside,'at_boundary_rows':at_boundary,
                    'window_sha256':sha(sorted_), 'after_rows':outside,'after_by_kind':dict(kinds),'after_bounds_ps':outside_bounds}


def compare(left,right):
    stats=[defaultdict(lambda:[0,hashlib.sha256()]),defaultdict(lambda:[0,hashlib.sha256()])]
    first=None
    preceding=deque(maxlen=6)
    with left.open('rb') as a,right.open('rb') as b:
        for i,(p,q) in enumerate(zip_longest(a,b),1):
            for side,row in enumerate((p,q)):
                if row is not None:
                    _,node,dev,*_=row.split()
                    state=stats[side][int(node),int(dev)]
                    state[0]+=1
                    state[1].update(row)
            if first is None:
                entry={'row_1based':i,'ns3':p.decode().rstrip() if p else None,'twins':q.decode().rstrip() if q else None}
                if p!=q:
                    first={'first':entry,'preceding':list(preceding),'following':[]}
                else:
                    preceding.append(entry)
            elif len(first['following'])<6:
                first['following'].append({'row_1based':i,'ns3':p.decode().rstrip() if p else None,'twins':q.decode().rstrip() if q else None})
    records=[]
    for node,port in sorted(stats[0].keys()|stats[1].keys()):
        a,b=(s[node,port] for s in stats)
        records.append({'node':node,'device':port,'ns3_count':a[0],'twins_count':b[0],
                        'ns3_sha256':a[1].hexdigest(),'twins_sha256':b[1].hexdigest(),
                        'equal':a[0]==b[0] and a[1].digest()==b[1].digest()})
    return first,records


def historical(path,new_rows):
    lines=path.read_text().splitlines()
    assert len(lines)==12
    prior=[]
    for l in lines:
        m=re.fullmatch(r'== (.*?): (\d+) ns-3 enqueues, (\d+) TwiNS: (.*)',l)
        assert m
        if m[1]!=HISTORICAL_CASE:
            assert m[4]=='identical over the common prefix'
            prior.append({'label':m[1],'common_prefix_rows':min(int(m[2]),int(m[3]))})
    assert len(prior)==11
    prior_total=sum(r['common_prefix_rows'] for r in prior)
    return {'old_summary_sha256':sha(path),'archived_other_cases':prior,'archived_other_rows':prior_total,
            'rechecked_case_rows':new_rows,'total_rows':prior_total+new_rows}


def main():
    out=Path(sys.argv[1]).resolve()
    repo=Path(sys.argv[2]).resolve()
    ns,tw=(out/name for name in ('ns3','hip'))
    prior=repo/'data/ns3/ref/k8_a2a30_TcpCubic.csv'
    flows=repo/'data/flows/k8_a2a30.csv'
    c=fct(ns/'completion.fct',tw/'completion.fct',prior,flows)
    for arm in (ns,tw):
        receipt=json.loads((arm/'run-provenance.json').read_text())
        assert receipt['flow_sha256']==sha(flows)
        assert receipt['queue_trace_sha256']==sha(arm/'full.qtrace.gz')
    if len(set(c['last_ps'].values()))!=1:
        save(out/'queue-audit.json',{'fct':c,'accepted':False})
        raise SystemExit('The two native completion boundaries disagree')
    cutoff=c['last_ps']['ns3']
    with tempfile.TemporaryDirectory(dir=out) as temp:
        stages=[]
        for arm in (ns,tw):
            part=Path(temp)/arm.name
            part.mkdir()
            stages.append(prepare(arm/'full.qtrace.gz',part,cutoff))
        (a,r),(b,t)=stages
        mismatch,queues=compare(a,b)
    accepted=(mismatch is None and all(q['equal'] for q in queues) and
              c['historical_reference_full_record_equal'] and c['six_fields_equal'] and c['all_completion_ps_equal'])
    stats={'fct':c, 'inclusive_cutoff_ps':cutoff,'ns3':r,'twins':t,'queues':queues,
           'first_difference':mismatch,'accepted':accepted}
    if accepted:
        stats['twelve_case_summary']=historical(repo/'out/audit/qdiff_k8.txt',r['window_rows'])
    save(out/'queue-audit.json',stats)
    print(accepted,cutoff,r,t,stats.get('twelve_case_summary',{}),flush=True)
    if not accepted:
        raise SystemExit(1)


if __name__=='__main__':
    main()
