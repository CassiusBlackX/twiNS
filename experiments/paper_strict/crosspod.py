"""Compare three CUDA TCP simulators on decoded six-hop paths and fixed offered messages."""
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import select
import shutil
from statistics import median
import subprocess
import sys
import time

VIDEOS=Path.home()/'Videos'
ROOT=VIDEOS/'twins-paper-strict-20261123'
OLD=VIDEOS/'twins-paper-pair-20261122/project'
CANDIDATE=VIDEOS/'twins-paper-followup-20261123/candidate'
DAYS=VIDEOS/'days/target/release/days'
LOCK=VIDEOS/'twins-isolated-20261118/out/exclusive-campaign.lock'
OBSERVER=VIDEOS/'twins-paper-followthrough-20261122/project/experiments/paper_review/observe_cuda.py'

def replace(path,old,new):
    source=path.read_text()
    if source.count(old)!=1:
        raise ValueError(f'unexpected source for {path}: {old}')
    path.write_text(source.replace(old,new,1))

def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def invoke_bounded(command, cwd, env, stream, timeout):
    # A pidfd signals exit without the 50 ms polling backoff in wait(timeout=...).
    with subprocess.Popen(command,cwd=cwd,env=env,stdout=stream,stderr=subprocess.STDOUT) as proc:
        try:
            fd=os.pidfd_open(proc.pid)
        except ProcessLookupError:
            return proc.wait()
        try:
            poller=select.poll()
            poller.register(fd,select.POLLIN)
            if poller.poll(round(timeout*1000)):
                return proc.wait()
            proc.kill()
            proc.wait()
            return 124
        finally:
            os.close(fd)

def compile_variants(output):
    work=output/'builds'
    work.mkdir()
    twins=work/'twins'
    twins.mkdir()
    shutil.copy2(CANDIDATE/'CMakeLists.txt',twins)
    for name in ('src','validation'):
        shutil.copytree(CANDIDATE/name,twins/name)
    src=twins/'src/step.cuh'
    replace(src,
        'fr.up = (uint16_t)((to_sender ? F.hash_ack[f] : F.hash_data[f]) % (uint32_t)c.s->ecmp_n);',
        'fr.up = (uint16_t)(to_sender ? F.hash_ack[f] : F.hash_data[f]);')
    code=src.read_text()
    start=code.index('    const int k = s.k, hk = s.hk, E = k * hk;\n',code.index('int route_up('))
    end=code.index('\n}\n',start)
    routing=code[start:end]
    assert routing.count('return hk + up;')==2
    routing=routing.replace('return hk + up;','return hk + (up & 255);',1)
    routing=routing.replace('return hk + up;','return hk + (up >> 8);',1)
    src.write_text(code[:start]+routing+code[end:])

    gedes=work/'GeDES'
    shutil.copytree(OLD/'GeDES_code',gedes)
    ge=gedes/'code/c++/src/link_layer/switch.cu'
    replace(ge,'int half_ft_k, int rotating)', 'int half_ft_k, int rotating, bool aggregation)')
    replace(ge,'return g_audit_ecmp[src] % half_ft_k + half_ft_k;',
            'return ((g_audit_ecmp[src] >> (aggregation ? 8 : 0)) & 255) + half_ft_k;')
    code=ge.read_text()
    call='audit_up_port(frame, half_ft_k, (current_port_up_forward + dst_node_id) % half_ft_k + half_ft_k)'
    assert code.count(call)==2
    code=code.replace(call,call[:-1]+', true)',1)
    code=code.replace(call,call[:-1]+', false)',1)
    ge.write_text(code)
    (output/'adaptations.json').write_text(json.dumps({
        'twins':{'file':'src/step.cuh','before':sha(CANDIDATE/'src/step.cuh'),'after':sha(src)},
        'gedes':{'file':str(ge.relative_to(gedes)),'before':sha(OLD/'GeDES_code'/ge.relative_to(gedes)),
                 'after':sha(ge)},
        'scope':'Only the fat-tree route selection is adapted to distinct frozen edge/core port ordinals.',
    },indent=2)+'\n')
    for target,root,opts in (
        ('twins',twins,['-DTWINS_GPU_BACKEND=CUDA']),
        ('GeDES',gedes,['-DGEDES_GPU_BACKEND=CUDA','-DCMAKE_CUDA_FLAGS=-DENABLE_DCTCP=0']),
    ):
        with (output/(target+'-build.log')).open('wb') as log:
            subprocess.run(['cmake','-S',root,'-B',root/'cmake','-G','Ninja','-DCMAKE_CUDA_ARCHITECTURES=120',
                '-DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc',*opts],stdout=log,stderr=subprocess.STDOUT,check=True)
            subprocess.run(['cmake','--build',root/'cmake','--target',target,'-j2'],stdout=log,stderr=subprocess.STDOUT,check=True)

def route_hashes(case, out):
    ports={}
    with (case/'routes.csv').open() as stream:
        for item in csv.DictReader(stream):
            ports[int(item['src'])]=int(item['edge_uplink']) | (int(item['aggregation_uplink'])<<8)
    rows=list(csv.DictReader((case/'twins.csv').open()))
    with (out/'twins-hash.csv').open('w') as stream:
        writer=csv.writer(stream)
        writer.writerow(('src','dst','size','start','est','done','hash_data','hash_ack'))
        for item in rows:
            a,b=int(item['src']),int(item['dst'])
            writer.writerow((a,b,item['flow_size'],item['start_ns'],0,0,ports[a],ports[b]))
    with (out/'gedes-hash.csv').open('w') as stream:
        writer=csv.writer(stream)
        writer.writerow(('src','dst','size','start','est','done','hash_data'))
        for a,code in ports.items():
            writer.writerow((a,0,0,0,0,0,code))
    return ports

def run(out, engines, case, spec, name, trial, observe=False, environment=None, timeout=420):
    work=out/f'trial-{trial}'/name
    work.mkdir(parents=True)
    binary=engines[name]
    k=spec['k']
    env=os.environ.copy()
    env.pop('GEDES_PAIRED_LEAF',None)
    if environment:
        env.update(environment)
    if name=='twins':
        cmd=[binary,f'--k={k}','--tcp=newreno','--queue=200','--ecnK=200',
             f'--max_slots={spec.get("cap_ns",10_000_000)//1000}',f'--flows={case}/twins.csv',f'--ecmp_from={out}/twins-hash.csv','--out=fct.csv']
        if observe:
            cmd+=['--qtrace=queues.raw','--qtrace_until=200000000']
    elif name=='gedes':
        cmd=[binary,f'--ft_k={k}',f'--flow_file={case}/gedes.csv',f'--max_slots={spec.get("cap_ns",10_000_000)//1000}','--output=fct.csv']
        env['GEDES_ECMP_FILE']=str(out/'gedes-hash.csv')
    else:
        cmd=[binary,case/'days.toml','--engine','cuda']
    cmd=['taskset','-c','0',*map(str,cmd)]
    start=time.perf_counter_ns()
    with (work/'process.log').open('wb') as stream:
        rc=invoke_bounded(cmd,work,env,stream,timeout)
        elapsed=time.perf_counter_ns()-start
    log=(work/'process.log').read_text(errors='replace')
    row=dict(engine=name,trial=trial,rc=rc,wall_ns=elapsed,command=cmd,sha256=sha(binary),observation=observe)
    if name=='days':
        for field,prefix in [('native_result','record=days_result'),('device','record=days_device')]:
            text=next((x for x in log.splitlines() if x.startswith(prefix)),'')
            row[field]={key:int(val) for key,val in re.findall(r'\b(\w+)=(\d+)\b',text)}
        result=row['native_result']
        row['payload_packet_lower_bound']=result.get('received_packets',0)>=spec['flow_rows']*spec['segments_per_flow']
        row['terminal_state_drained']=result.get('pending_events')==0 and result.get('resident_packets')==0
        # Delivery count may include a retransmitted attempt. Exact flow
        # completion is checked from final sender ACK state in days_state.py.
        row['accepted']=rc==0 and row['payload_packet_lower_bound'] and row['terminal_state_drained']
    else:
        if (work/'fct.csv').exists():
            fcts=list(csv.DictReader((work/'fct.csv').open()))
            field='completed_ns' if name=='twins' else 'completed_timestamp'
            ends=[int(x[field]) for x in fcts if int(x[field])>=0]
            row.update(completed=len(ends),flow_rows=len(fcts),latest_completion_ns=max(ends,default=None))
        loop=re.findall(r'Simulation Duration: ([\d.]+)s',log)
        slots=re.findall(r'Executed Slots: (\d+)',log)
        row.update(event_loop_s=float(loop[-1]) if loop else None,steps=int(slots[-1]) if slots else None)
        counters=re.findall(r'drops (\d+) marks (\d+) retx (\d+) rto (\d+)',log)
        if counters:
            row['tcp_counters']={field:int(value) for field,value in zip(('drops','marks','retx','rto'),counters[-1])}
        row['accepted']=rc==0 and row.get('completed')==spec['flow_rows']
    if observe and (work/'queues.raw').exists():
        trace=(work/'queues.raw')
        counts={}
        for line in trace.open():
            _,node,port,src,dst,*_=line.split()
            key=','.join((node,port,src,dst))
            counts[key]=counts.get(key,0)+1
        row['observed_switch_port_source_target_counts']=counts
    (work/'receipt.json').write_text(json.dumps(row,indent=2)+'\n')
    print(case.name,name,trial,row['accepted'],elapsed/1e9,flush=True)
    return row

def measure(output, build, inputs=None):
    if inputs is None:
        inputs=sorted((ROOT/'crosspod_inputs').glob('20*'))[-1]/'results'
    binaries={'twins':build/'twins/cmake/twins','gedes':build/'GeDES/cmake/GeDES','days':DAYS}
    trials=[]
    for case in sorted(inputs.glob('k*-*')):
        spec=json.loads((case/'input.json').read_text())
        out=output/case.name
        out.mkdir(parents=True)
        for p in case.iterdir():
            if p.is_file():
                shutil.copy2(p,out/p.name)
        route_hashes(case,out)
        # Freeze this matrix before the first formal sample. The pilot diagnoses viability.
        pilot=[run(out,binaries,case,spec,name,'pilot',name=='twins') for name in binaries]
        for i,order in enumerate((('twins','days','gedes'),('days','gedes','twins'),('gedes','twins','days'))):
            for name in order:
                trials.append(dict(input=case.name,**run(out,binaries,case,spec,name,i)))
        (out/'pilot.json').write_text(json.dumps(pilot,indent=2)+'\n')
    (output/'summary.json').write_text(json.dumps({
        'trials':trials,
        'accepted':sum(x['accepted'] for x in trials),
        'native_timing_scope':'Days prints separate lowering and device/engine timing; TwiNS and GeDES retain native loop and exit conditions.',
    },indent=2)+'\n')

def main():
    out=Path(sys.argv[1]).resolve()
    if len(sys.argv)>2:
        return measure(out,Path(sys.argv[2]),Path(sys.argv[3]) if len(sys.argv)>3 else None)
    out.mkdir(parents=True,exist_ok=False)
    compile_variants(out)
    cmd=['flock',str(LOCK),sys.executable,str(OBSERVER),str(out/'resources'),
         sys.executable,__file__,str(out/'measurements'),str(out/'builds')]
    return subprocess.run(cmd).returncode

if __name__=='__main__':
    raise SystemExit(main())
