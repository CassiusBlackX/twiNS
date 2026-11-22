"""Build an observation-only GeDES variant, preserving all transport decisions."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from collections import Counter

def replace(path,before,after):
    text=path.read_text()
    if text.count(before)!=1:
        raise ValueError('frozen source no longer matches: '+str(path))
    path.write_text(text.replace(before,after,1))

def main():
    output=Path(sys.argv[1]).resolve()
    output.mkdir(parents=True,exist_ok=False)
    baseline=Path.home()/'Videos/twins-paper-pair-20261122/project'
    source=output.parent/'source'
    shutil.copytree(baseline/'GeDES_code',source)
    transport=source/'code/c++/src/transport/tcp_controller.cu'
    replace(transport,
        '\n                fill_tcp_packets(con, packet, con->mss, NULL, params->timeslot_end_time);',
        '\n                printf("PAIR_DATA %d %d %llu\\n", tid, i, (unsigned long long)con->una);\n'
        '                fill_tcp_packets(con, packet, con->mss, NULL, params->timeslot_end_time);')
    replace(transport,
        'int ack_response_num = send_ack_packets(params, non_ack_num, cons, con_num, alloc_packets);',
        'int ack_response_num = send_ack_packets(params, non_ack_num, cons, con_num, alloc_packets);\n'
        '        if (ack_response_num) printf("PAIR_ACK %d %d\\n", tid, ack_response_num);')
    switch=source/'code/c++/src/link_layer/switch.cu'
    replace(switch,
        '\n        params->drop_frame_num[tid] = drop_frame_num;',
        '\n        params->drop_frame_num[tid] = drop_frame_num;\n'
        '        if (drop_frame_num) printf("PAIR_DROP %d %d\\n", tid, drop_frame_num);')
    replace(source/'main.cc','gedesConfigureRuntime();',
        'gedesConfigureRuntime();\n    cudaDeviceSetLimit(cudaLimitPrintfFifoSize, 8 * 1024 * 1024);')
    save=lambda p,v:p.write_text(json.dumps(v,indent=2)+'\n')
    save(output/'changed-sources.json',{
        str(p.relative_to(source)):hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (transport,switch,source/'main.cc')
    })
    build=output.parent/'build'
    with (output/'build.log').open('wb') as log:
        subprocess.run(['cmake','-S',source,'-B',build,'-G','Ninja','-DGEDES_GPU_BACKEND=CUDA',
            '-DCMAKE_CUDA_ARCHITECTURES=120','-DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc',
            '-DCMAKE_CUDA_FLAGS=-DENABLE_DCTCP=0'],stdout=log,stderr=subprocess.STDOUT,check=True)
        subprocess.run(['cmake','--build',build,'--target','GeDES','-j2'],stdout=log,
                       stderr=subprocess.STDOUT,check=True)
    reports={}
    for k in (8,16):
        case=output/f'k{k}'
        case.mkdir()
        command=[str(build/'GeDES'),f'--ft_k={k}',
                 f'--flow_file={baseline}/common-inputs/k{k}/gedes.csv',
                 '--max_slots=10000','--output=fct.csv']
        logpath=case/'observations.log'
        with logpath.open('wb') as log:
            rc=subprocess.run(command,cwd=case,env={**os.environ,'GEDES_PAIRED_LEAF':'1'},
                              stdout=log,stderr=subprocess.STDOUT,timeout=180).returncode
        packets=Counter()
        ack=drop=0
        with logpath.open() as log:
            for line in log:
                row=line.split()
                if not row:
                    continue
                if row[0]=='PAIR_DATA':
                    packets[tuple(row[1:])]+=1
                elif row[0]=='PAIR_ACK':
                    ack+=int(row[2])
                elif row[0]=='PAIR_DROP':
                    drop+=int(row[2])
        reference=baseline/'runs/20261122T094810Z/measurements'/f'k{k}/trial-0/gedes/fct.csv'
        identical=(case/'fct.csv').read_bytes()==reference.read_bytes()
        reports[f'k{k}']=dict(rc=rc,data_packets=sum(packets.values()),unique_data_segments=len(packets),
                             retransmitted_packets=sum(v-1 for v in packets.values()),
                             ack_packets=ack,drop_packets=drop,
                             unchanged_fct=identical,command=command)
        if not rc and identical and packets and ack:
            logpath.unlink()
        else:
            raise RuntimeError('diagnostic run must reproduce the frozen FCT and retain packet observations')
    save(output/'summary.json',reports)

if __name__=='__main__':
    main()
