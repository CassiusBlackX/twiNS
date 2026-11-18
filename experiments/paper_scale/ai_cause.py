"""Find a selective recovery whose receive-completion causes a later-stage send."""
import argparse
from collections import Counter,defaultdict
from pathlib import Path
import json
from ai_compare import messages

def phase(folder,side):
    dag=folder/('reference.dag' if side=='reference' else 'gpu.dag')
    fct=messages(folder/('messages.fct' if side=='reference' else 'gpu.fct'))
    parent={}
    identity={}
    per_pair=Counter()
    qp={}
    for line in dag.read_text().splitlines():
        x=line.split()
        if not x:continue
        if x[0]=='flow':
            i,s,d,size=map(int,x[1:]);port=10000+per_pair[s,d];per_pair[s,d]+=1
            qp[s,d,port]=i
            identity[i]=(s,d,size,port)
        if x[0]=='on':parent[x[1]]=x[2]
    seen=defaultdict(set);retx={};sack={}
    with (folder/(side+'.tx')).open() as f:
        for line in f:
            ts,node,dev,kind,src,dst,sport,seq,size,ecn,dport,nack,length,flag=line.split()
            src,dst,sport,seq=map(int,(src,dst,sport,seq))
            if kind=='D' and int(node)==src:
                i=qp[src,dst,sport]
                if seq in seen[i]:retx.setdefault(i,(int(ts),seq))
                seen[i].add(seq)
            elif kind=='N' and int(length)>0:
                i=qp[dst,src,sport];sack.setdefault(i,(int(ts),int(nack)))
    edges={}
    for i in identity:
        cause=parent['f'+str(i)]
        path=[]
        while cause.startswith('t'):
            path.append(cause);cause=parent[cause]
        if cause.startswith('r'):
            prev=int(cause[1:]);edges[(identity[prev],identity[i])]=(prev,i,path)
    return fct,edges,retx,sack

def main():
    p=argparse.ArgumentParser()
    for name in ('reference','gpu','output'):p.add_argument(name,type=Path)
    a=p.parse_args()
    runs={(c,s):phase((a.reference if s=='reference' else a.gpu)/c,s) for c in ('large-buffer','small-buffer') for s in ('reference','gpu')}
    choices=runs['small-buffer','reference'][1];result={}
    for key in choices:
        if all(key in runs['small-buffer',s][1] and (i:=runs['small-buffer',s][1][key][0]) in runs['small-buffer',s][2] and i in runs['small-buffer',s][3] for s in ('reference','gpu')):
            result={'identity_parent_then_child':key,'runs':{}}
            for (c,s),(times,edges,retx,sack) in runs.items():
                if key in edges:
                    i,j,path=edges[key]
                    result['runs'][c+'-'+s]=dict(parent_id=i,child_id=j,parent_start_ns=times[i][0],parent_done_ns=sum(times[i]),child_start_ns=times[j][0],timer_chain=path,first_selective_feedback=sack.get(i),first_retransmission=retx.get(i))
                else:
                    other=next(((par,ch,p) for (x,y),(par,ch,p) in edges.items() if y==key[1]),None)
                    result['runs'][c+'-'+s]=dict(same_cause=False,child_start_ns=times[other[1]][0] if other else None,other_parent=other[0] if other else None)
            break
    a.output.write_text(json.dumps(result or {'witness':None,'reason':'no shared recovered predecessor'},indent=2))
if __name__=='__main__':main()
