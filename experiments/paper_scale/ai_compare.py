"""Message and normalized action comparison using the existing SimAI evidence convention."""
from collections import Counter
import json
from pathlib import Path
import re

def messages(path):
    table={}
    for line in path.read_text().splitlines():
        i,start,duration=map(int,line.split())
        if i in table:raise ValueError('duplicate flow ID')
        table[i]=(start,duration)
    return table

def actions(path):
    return Counter(re.sub(r'^(on [ft][0-9]* m).*',r'\1',line) for line in path.read_text().splitlines() if line and not line.startswith('#'))

def compare(gpu,ref):
    a=messages(ref/'messages.fct');b=messages(gpu/'gpu.fct');ids=sorted(set(a)|set(b))
    ra=actions(ref/'reference.dag');ga=actions(gpu/'gpu.dag')
    first=next((dict(id=i,reference=a.get(i),gpu=b.get(i)) for i in ids if a.get(i)!=b.get(i)),None)
    reference=re.search(r'live counters drops=(\d+) sacks=(\d+) retransmitted=(\d+) timeouts=(\d+)',(ref/'reference.log').read_text())
    log=(gpu/'gpu.log').read_text()
    marks=re.search(r'drops (\d+) marks (\d+)',log);irn=re.search(r'IRN selective feedback (\d+), retransmitted packets (\d+), timeouts (\d+)',log)
    if not all((reference,marks,irn)):raise ValueError('missing protocol counters')
    processed=re.search(r'IRN selective feedback processed by live QPs (\d+)',log)
    counts=list(map(int,reference.groups()))
    gpu_counts=[int(marks[1]),int(processed[1]) if processed else int(irn[1]),int(irn[2]),int(irn[3])]
    clean='undecided ties 0, on lost tails 0 | overflows: tx 0 queue 0 ackq 0 qps 0 due 0' in log
    ok=(int((ref/'reference.rc').read_text())==int((gpu/'gpu.rc').read_text())==0 and a==b and ra==ga and counts==gpu_counts and clean)
    return dict(accepted=ok,reference_messages=len(a),gpu_messages=len(b),matching_messages=sum(a.get(i)==b.get(i) for i in ids),
                reference_actions=ra.total(),gpu_actions=ga.total(),matching_actions=(ra&ga).total(),first_message_difference_by_id=first,
                first_action_only_reference=next(iter((ra-ga).items()),None),first_action_only_gpu=next(iter((ga-ra).items()),None),
                counters=['drops','selective feedback processed by live QPs','retransmitted packets','timeouts'],reference_counters=counts,gpu_counters=gpu_counts,gpu_feedback_emitted=int(irn[1]),gpu_feedback_has_live_qp_counter=bool(processed),terminal_clean=clean)
