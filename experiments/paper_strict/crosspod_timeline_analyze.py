"""Account for CUDA device activity and intersect it with the native host synchronization."""

from collections import Counter, defaultdict
import json
from pathlib import Path
import re
import sqlite3
import sys


SYNC = re.compile(r'^(?:cu|cuda)(?:Ctx|Device|Thread|Stream|Event)Synchronize')


def merge(intervals):
    joined=[]
    for begin,end in sorted(intervals):
        if begin>=end:
            continue
        if not joined or begin>joined[-1][1]:
            joined.append([begin,end])
        elif end>joined[-1][1]:
            joined[-1][1]=end
    return joined


def length(intervals):
    return sum(b-a for a,b in intervals)


def clip(intervals, lo, hi):
    return ((max(lo,a),min(hi,b)) for a,b in intervals if a<hi and b>lo)


def intersection(a,b):
    """Return interval length common to two already merged sequences."""
    i=j=total=0
    while i<len(a) and j<len(b):
        lo=max(a[i][0],b[j][0]); hi=min(a[i][1],b[j][1]); total+=max(0,hi-lo)
        if a[i][1]<=b[j][1]:
            i+=1
        else:
            j+=1
    return total


def available(db, table):
    return db.execute('SELECT 1 FROM sqlite_master WHERE type=\'table\' AND name=?',(table,)).fetchone() is not None


def query(db, table, sql):
    return db.execute(sql) if available(db,table) else ()


def stage(engine,name):
    n=name.lower()
    if engine=='twins':
        return 'slot kernel' if 'ns_slot' in n else 'other'
    if engine=='gedes':
        for key, patterns in (
            ('TCP',('receive_tcp','send_tcp','tcp_ack','tcp_receive','tcp_send')),
            ('encapsulation/IPv4',('encapsul','decapsul','ipv4','route_packets','route_ip','frame_to')),
            ('pool/cache',('allocate','alloc_','_alloc','recycle','reclaim','cache','buffer_pool','free_packet','memory_pool')),
            ('link/switch',('p2p_','forward','switch','distribute','dequeue_frame')),
            ('device scans/reductions',('devicescan','devicereduce')),
            ('slot/completion',('timeslot','time_slot','advance_time','completed','finish','timer','time_update')),
        ):
            if any(x in n for x in patterns):
                return key
        return 'other'
    if n in ('days_round','days_round_parallel'):
        return 'round execution'
    if n.startswith('days_exchange'):
        return 'exchange'
    if n.startswith('days_horizon'):
        return 'horizon'
    if n.startswith('days_round_'):
        return 'round setup/control'
    return 'other'


def unique_time(groups):
    """Separate stage-exclusive kernel occupancy from concurrent stages."""
    change=defaultdict(list)
    for name,intervals in groups.items():
        for a,b in intervals:
            change[a].append((name,True))
            change[b].append((name,False))
    prev=0
    active=set()
    exclusive=Counter()
    overlap=0
    for t,updates in sorted(change.items()):
        if len(active)==1:
            exclusive[next(iter(active))]+=t-prev
        elif len(active)>1:
            overlap+=t-prev
        for name,enter in updates:
            (active.add if enter else active.remove)(name)
        prev=t
    assert not active
    return dict(exclusive),overlap


def api_group(rows,lo,hi):
    by_name=defaultdict(list)
    for a,b,name,*_ in rows:
        if SYNC.match(name) and a<hi and b>lo:
            by_name[name].append((max(lo,a),min(hi,b)))
    report={k:{'count':len(v),'sum_ns':length(v),'first_ns':v[0][0],'last_ns':v[-1][1]}
            for k,v in by_name.items()}
    return report,merge(t for v in by_name.values() for t in v)


def counts_for_phase(kernels,copy,sets,lo,hi):
    k=merge(clip(kernels,lo,hi)); m=merge(clip(copy,lo,hi)); z=merge(clip(sets,lo,hi))
    memory=merge(m+z); active=merge(k+memory)
    return {'start_ns':lo, 'end_ns':hi, 'wall_ns':hi-lo, 'kernel_busy_ns':length(k),
            'copy_busy_ns':length(m), 'memset_busy_ns':length(z), 'memory_only_ns':length(active)-length(k),
            'kernel_memory_overlap_ns':intersection(k,memory), 'busy_ns':length(active), 'idle_ns':hi-lo-length(active)},active


def analyze(db_path, meta):
    engine=meta['engine']
    with sqlite3.connect(db_path) as db:
        api=list(db.execute('''SELECT r.start,r.end,s.value,r.correlationId,r.globalTid FROM CUPTI_ACTIVITY_KIND_RUNTIME r
                           JOIN StringIds s ON r.nameId=s.id ORDER BY r.start'''))
        assert api
        graph_prefix='cuGraphLaunch' if engine=='days' else 'cudaGraphLaunch'
        launches=[row for row in api if row[2].startswith(graph_prefix)]
        launch_ids={r[3] for r in launches}
        assert len(launches)==len(launch_ids)>0,(engine,len(launches))
        if engine=='gedes':
            assert len(launches)==meta['executed_slots_reported']
        matching_sync=[r for r in api if SYNC.match(r[2]) and r[4]==launches[-1][4] and r[0]>=launches[-1][0]]
        stop=next(r for r in matching_sync if r[2].startswith(('cudaStreamSynchronize','cuStreamSynchronize',
            'cudaDeviceSynchronize','cuCtxSynchronize','cuEventSynchronize')))
        lo,hi=launches[0][0],stop[1]

        replay_span={}
        def track(c,a,b):
            if c not in launch_ids:
                return
            extent=replay_span.get(c)
            if extent is None:
                replay_span[c]=[a,b]
            else:
                extent[0]=min(extent[0],a)
                extent[1]=max(extent[1],b)

        all_k,all_copy,all_set=[],[],[]
        kernels=defaultdict(list)
        stages=defaultdict(list)
        memory_bytes=Counter()
        memory_calls=Counter()
        for a,b,name,c in db.execute('''SELECT k.start,k.end,s.value,k.correlationId FROM CUPTI_ACTIVITY_KIND_KERNEL k
                                    JOIN StringIds s ON k.shortName=s.id ORDER BY k.start'''):
            all_k.append((a,b))
            track(c,a,b)
            if lo<=a and b<=hi:
                kernels[name].append((a,b))
                stages[stage(engine,name)].append((a,b))
        for a,b,c,n,d in query(db,'CUPTI_ACTIVITY_KIND_MEMCPY','''SELECT m.start,m.end,m.correlationId,m.bytes,
                    coalesce(en.label,cast(m.copyKind AS text)) FROM CUPTI_ACTIVITY_KIND_MEMCPY m
                    LEFT JOIN ENUM_CUDA_MEMCPY_OPER en ON en.id=m.copyKind ORDER BY m.start'''):
            all_copy.append((a,b))
            track(c,a,b)
            if lo<=a and b<=hi:
                memory_bytes[d]+=n
                memory_calls[d]+=1
        for a,b in query(db,'CUPTI_ACTIVITY_KIND_MEMSET','SELECT start,end FROM CUPTI_ACTIVITY_KIND_MEMSET ORDER BY start'):
            all_set.append((a,b))
    assert all_k and len(replay_span)==len(launch_ids),(engine,len(replay_span),len(launch_ids))
    first=min(api[0][0],all_k[0][0])
    last=max(max(r[1] for r in api),max(b for a,b in all_k),max((b for a,b in all_copy+all_set),default=0))
    phases={}
    for name,start,end in (('cuda_initialization',first,lo),('advance',lo,hi),('cuda_output',hi,last)):
        p,gpu=counts_for_phase(all_k,all_copy,all_set,start,end)
        by_name,wait=api_group(api,start,end)
        p.update(sync_by_api=by_name,sync_union_ns=length(wait),sync_coincident_gpu_busy_ns=intersection(wait,gpu),
                 sync_coincident_gpu_idle_ns=length(wait)-intersection(wait,gpu))
        phases[name]=p
    stage_merged={k:merge(v) for k,v in stages.items()}
    x,over=unique_time(stage_merged)
    assert sum(x.values())+over==phases['advance']['kernel_busy_ns']
    ordered=[replay_span[row[3]] for row in launches]
    assert all(a>=lo and b<=hi for a,b in ordered)
    gaps=[(prev[1],nxt[0]) for prev,nxt in zip(ordered,ordered[1:]) if nxt[0]>prev[1]]
    replay_union=merge(ordered)
    advance=phases['advance']
    device_activity=merge(list(clip(all_k,lo,hi))+list(clip(all_copy,lo,hi))+list(clip(all_set,lo,hi)))
    graph_span=length(replay_union)
    by_api=defaultdict(list)
    for begin,end,name,*_ in api:
        if begin<hi and end>lo:
            by_api[name].append((max(lo,begin),min(hi,end)))
    api_in_loop={name:{'count':len(v),'sum_ns':length(v),'union_ns':length(w:=merge(v)),
                       'coincident_gpu_busy_ns':intersection(w,device_activity)} for name,v in by_api.items()}
    result={'engine':engine,'boundaries':{'first_replay_api':launches[0], 'last_replay_api':launches[-1],
                                          'closing_sync_api':stop,'source':'CUDA APIs in the frozen program'},
          'phases':phases,'graph_replays':len(launches),'graph_gpu_spans_sum_ns':length(ordered),
          'graph_gpu_spans_union_ns':graph_span,'between_replays_ns':length(gaps),
          'between_replays_idle_ns':length(gaps)-intersection(merge(gaps),device_activity),
          'idle_inside_graph_spans_ns':graph_span-intersection(replay_union,device_activity),
          'kernel_stages':{k:{'count':len(v),'sum_ns':length(v),'busy_union_ns':length(stage_merged[k]),
                              'exclusive_ns':x.get(k,0)} for k,v in stages.items()},
          'kernel_stage_overlap_ns':over,'kernel_names':{k:{'count':len(v),'sum_ns':length(v),'busy_union_ns':length(merge(v)),
                                                        'stage':stage(engine,k)} for k,v in kernels.items()},
          'copy_counts_by_kind':dict(memory_calls),'copy_bytes_by_kind':dict(memory_bytes),
          'host_api_in_advance':api_in_loop}
    assert advance['busy_ns']+advance['idle_ns']==advance['wall_ns'] and len(kernels)>0
    return result


def analyze_envelopes(db_path, meta):
    """Get slot-to-slot gaps from graph launch-range records without node tracing."""
    assert meta['engine']=='gedes'
    with sqlite3.connect(db_path) as db:
        api=list(db.execute('''SELECT r.start,r.end,s.value,r.correlationId,r.globalTid
              FROM CUPTI_ACTIVITY_KIND_RUNTIME r JOIN StringIds s ON r.nameId=s.id ORDER BY r.start'''))
        launches=[r for r in api if r[2].startswith('cudaGraphLaunch')]
        assert len(launches)==meta['executed_slots_reported']
        specs=[]
        for name, in db.execute("SELECT name FROM sqlite_master WHERE name LIKE '%GRAPH%' AND type='table'"):
            columns={r[1] for r in db.execute(f'PRAGMA table_info("{name}")')}
            if {'start','end','correlationId','deviceId'}<=columns:
                specs.append(name)
        assert len(specs)==1,specs
        spans=defaultdict(list)
        for a,b,c in db.execute(f'SELECT start,end,correlationId FROM "{specs[0]}"'):
            spans[c].append((a,b))
        assert all(len(spans[r[3]])==1 for r in launches),(len(spans),len(launches))
        ordered=[spans[r[3]][0] for r in launches]
        sync=next(r for r in api if r[0]>=launches[-1][0] and r[2].startswith('cudaStreamSynchronize'))
        lo,hi=launches[0][0],sync[1]
        # These are graph envelopes, so gaps exclude nothing within an envelope.
        outside=[]
        for table in ('CUPTI_ACTIVITY_KIND_MEMCPY','CUPTI_ACTIVITY_KIND_MEMSET','CUPTI_ACTIVITY_KIND_KERNEL'):
            for a,b in query(db,table,f'SELECT start,end FROM {table}'):
                if a<hi and b>lo:
                    outside.append((max(lo,a),min(hi,b)))
    assert all(a<b for a,b in ordered) and ordered[0][0]>=lo and ordered[-1][1]<=hi
    gaps=merge((previous[1],following[0]) for previous,following in zip(ordered,ordered[1:]))
    return {'graph_trace_table':specs[0],'slots':len(ordered),'advancing_wall_ns':hi-lo,
            'graph_span_union_ns':length(merge(ordered)),'between_graph_gap_ns':length(gaps),
            'outside_work_in_gap_ns':intersection(gaps,merge(outside)),
            'native_loop_ns':meta['native_loop_ns'],'first_api_ns':lo,'last_sync_end_ns':hi}


def main():
    root=Path(sys.argv[1]).resolve()
    tag,engine=sys.argv[2:4]
    work=root/'results/profiles'/tag/engine
    meta=json.loads((work/'capture.json').read_text())
    db=root/'sqlite'/tag/f'{engine}.sqlite'
    result=analyze_envelopes(db,meta) if len(sys.argv)>4 and sys.argv[4]=='envelopes' else analyze(db,meta)
    print(json.dumps(result,indent=2))


if __name__=='__main__':
    main()
