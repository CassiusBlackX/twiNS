"""Reproduce matched MTP-OFF anchors and isolate sequential SimAI patch effects."""
import concurrent.futures
import hashlib
from itertools import zip_longest
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

SIM = Path('astra-sim-alibabacloud/extern/network_backend/ns3-interface/simulation')
CONFIG = '/simai/astra-sim-alibabacloud/inputs/config/SimAI.conf'
PREVIOUS = Path.home()/'Videos/twins-isolated-20261118'
ORACLE = Path.home()/'Videos/netsim/aitime'
IMAGE = 'localhost/simai-build'
BINARY = f'/simai/{SIM}/trial-build/scratch/ns3.36.1-AstraSimNetwork-optimized'


def h(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def save(path,value):
    path.write_text(json.dumps(value,indent=2)+'\n')


def write_once(path,before,after):
    text=path.read_text()
    if text.count(before)!=1:
        raise ValueError('source does not match frozen replacement: '+str(path))
    path.write_text(text.replace(before,after,1))


def clone(source,dest):
    ignore=shutil.ignore_patterns('.git','cmake-*','build','build-*','trial-build','docs','aicb','vidur-alibabacloud')
    shutil.copytree(source,dest,ignore=ignore,symlinks=True)


def toggle_variants(source):
    sim=source/SIM
    write_once(sim/'scratch/common.h',
        'return a->GetId () < b->GetId ();',
        'static const bool oldRoute = getenv("TWINS_ABLATE_ROUTE_ADDR");\n'
        '    return oldRoute ? a < b : a->GetId () < b->GetId ();')
    write_once(sim/'scratch/common.h',
        'sw->m_mmu->ConfigEcnStream(j, (uint64_t)i * SwitchMmu::pCnt + j);',
        'if (!getenv("TWINS_ABLATE_ECN_STREAM")) sw->m_mmu->ConfigEcnStream(j, (uint64_t)i * SwitchMmu::pCnt + j);')
    point=sim/'src/point-to-point/model'
    write_once(point/'switch-node.cc',
        'PppHeader::GetStaticSize() + 20 + 8 + 10',
        'PppHeader::GetStaticSize() + 20 + 8 + (oldIntHeader ? 6 : 10)')
    write_once(point/'switch-node.cc',
        'IntHeader *ih = (IntHeader*)&buf',
        'static const bool oldIntHeader = getenv("TWINS_ABLATE_INT_OFFSET");\n\t\t\tIntHeader *ih = (IntHeader*)&buf')
    mmu=point/'switch-mmu.cc'
    write_once(mmu,'namespace ns3 {','''namespace ns3 {
    struct TwinsEcnCount {
        unsigned long long draws = 0, marks = 0;
        ~TwinsEcnCount() {
            fprintf(stderr, "twins ecn draws %llu probabilistic marks %llu\\n", draws, marks);
        }
    };
    static TwinsEcnCount twinsEcnCount;
''')
    write_once(mmu,
        'if ((ecn_rng[ifindex] ? ecn_rng[ifindex]->RandU01() : UniformVariable(0, 1).GetValue()) < p)',
        '''const bool mark = (ecn_rng[ifindex] ? ecn_rng[ifindex]->RandU01() : UniformVariable(0, 1).GetValue()) < p;
            ++twinsEcnCount.draws;
            twinsEcnCount.marks += mark;
            if (mark)''')
    device=point/'qbb-net-device.cc'
    after = ('static const bool oldResume = getenv("TWINS_ABLATE_PFC_RESUME");\n'
             '\t\tif(oldResume){\n'
             '\t\t\tPtr<RdmaQueuePair> qp = m_rdmaEQ->GetQp(qIndex);\n'
             '\t\t\tif(qp->nvls_enable == 1 && m_node->GetNodeType() == 2) SwitchAsHostSend();\n'
             '\t\t\telse DequeueAndTransmit();\n'
             '\t\t\treturn;\n\t\t}\n')
    before='if(m_node->GetNodeType() == 2 && m_rdmaEQ->GetQp(qIndex)->nvls_enable == 1) SwitchAsHostSend();'
    write_once(device,before,after+before)


def docker(source,cap,cpus):
    return ['docker','run','--rm','--network=none','--ulimit','core=0',
            '--label','twins.evidence.campaign=simai-reference-20261122',
            '--user',f'{os.getuid()}:{os.getgid()}','--cpuset-cpus',str(cpus),
            f'--memory={cap}g',f'--memory-swap={cap}g','-v',f'{source}:/simai']


def build(source,out):
    out.mkdir(parents=True)
    base=docker(source,22,'8-13')+[IMAGE]
    config=['cmake','-S',f'/simai/{SIM}','-B',f'/simai/{SIM}/trial-cmake','-G','Ninja',
            '-DCMAKE_BUILD_TYPE=release','-DNS3_MTP=OFF','-DNS3_NATIVE_OPTIMIZATIONS=ON',
            '-DNS3_WARNINGS_AS_ERRORS=OFF','-DCMAKE_CXX_FLAGS=-include cstdint',
            f'-DNS3_OUTPUT_DIRECTORY=/simai/{SIM}/trial-build']
    compile=['cmake','--build',f'/simai/{SIM}/trial-cmake','--target','scratch_AstraSimNetwork','-j6']
    save(out/'build-commands.json',[base+config,base+compile])
    with (out/'build.log').open('wb') as log:
        for cmd in (config,compile):
            subprocess.run(base+cmd,stdout=log,stderr=subprocess.STDOUT,check=True,timeout=7200)
    binary=source/BINARY.removeprefix('/simai/')
    lib=source/SIM/'trial-build/lib/libns3.36.1-core-optimized.so'
    symbols=subprocess.check_output(['nm','-D','-C',lib],text=True)
    save(out/'build-result.json',dict(binary_sha256=h(binary),core_sha256=h(lib),
        event_id_symbol=[s for s in symbols.splitlines() if 'EventId::EventId(ns3::Ptr<ns3::EventImpl>' in s]))


def execute(source,results,work,name,workload,variant,core,cap):
    raw=work/name
    result=results/name
    raw.mkdir(parents=True)
    result.mkdir(parents=True)
    etc=raw/'etc'
    (etc/'simulation').mkdir(parents=True)
    (etc/'results').mkdir()
    options=['-e','AS_SEND_LAT=3']
    if variant:
        options+=['-e',variant+'=1']
    workload_path=f'/work/wl/paper/{workload}_128.txt'
    topology='/work/topo/AlibabaHPN_128g_8gps_DualToR_SinglePlane_200Gbps_H100'
    command=docker(source,cap,core)+options+['-v',f'{ORACLE}:/work:ro',
        '-v',f'{etc}:/etc/astra-sim','-v',f'{raw}:/run','-w','/run',IMAGE,
        'timeout','7200',BINARY,'-t','1','-w',workload_path,'-n',topology,'-c',CONFIG]
    start=time.monotonic_ns()
    with (result/'run.log').open('wb') as log:
        rc=subprocess.run(command,stdout=log,stderr=subprocess.STDOUT).returncode
    elapsed=time.monotonic_ns()-start
    fct=etc/'simulation/llama_hpn7_fct.txt'
    summary=[]
    for csv in raw.glob('*EndToEnd*.csv'):
        shutil.copyfile(csv,result/csv.name)
        summary += re.findall(r'total time,([\d.]+)',csv.read_text())
    ret=dict(command=command,rc=rc,wall_ns=elapsed,complete=rc==0 and bool(summary) and fct.is_file(),
             simulated_iteration_us=list(map(float,summary)),
             fct_sha256=h(fct) if fct.is_file() else None,
             workload_sha256=h(ORACLE/workload_path.removeprefix('/work/')),
             topology_sha256=h(ORACLE/topology.removeprefix('/work/')))
    count=re.findall(r'twins ecn draws (\d+) probabilistic marks (\d+)',(result/'run.log').read_text())
    if count:
        ret.update(ecn_draws=int(count[-1][0]),ecn_probabilistic_marks=int(count[-1][1]))
    save(result/'result.json',ret)
    return ret,fct


def compare(a,b):
    if not a.is_file() or not b.is_file():
        return {'available':False}
    with a.open('rb') as left,b.open('rb') as right:
        count=equal=0
        for x,y in zip_longest(left,right):
            count+=1
            equal+=x==y
    return dict(available=True,row_positions=count,equal=equal,byte_equal=h(a)==h(b))


def main():
    root=Path(sys.argv[1]).resolve()
    trace_script=Path(sys.argv[2]).resolve()
    out=root/'results'
    out.mkdir(parents=True,exist_ok=False)
    raw=root/'raw'
    source=PREVIOUS/'simai-mtp'
    off=root/'same-source-off'
    clone(source,off)
    pinned=json.loads((PREVIOUS/'out/receipts/mtp-source.json').read_text())['source_hashes']
    mismatches=[rel for rel,expected in pinned.items() if not (off/SIM/rel).is_file() or h(off/SIM/rel)!=expected]
    # CMake's generated source lock file changes between configurations and is not model code.
    model_mismatches=[rel for rel in mismatches if not Path(rel).name.startswith('.lock-ns3')]
    save(out/'same-source-proof.json',dict(pinned_files=len(pinned),mismatches=mismatches,
                                         model_mismatches=model_mismatches))
    if model_mismatches:
        raise RuntimeError('the copied MTP model does not match its frozen build receipt')
    build(off,out/'build-off')
    runs=[('gpt3_13b',4,8),('llama_65b',8,16),('gpt3_175b',12,18)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        futures={name:pool.submit(execute,off,out,raw,name+'-off',name+'_full','',core,cap)
                 for name,core,cap in runs}
        anchors={}
        for name,future in futures.items():
            result,fct=future.result()
            reference=PREVIOUS/'out/reference-cache'/(name+'_full_hpn128.fct')
            anchors[name]={'run':result,'vs_historical_uid64_off':compare(fct,reference)}
    save(out/'matched-off-summary.json',anchors)
    matrix={}
    for width in (64,32):
        copied=root/f'patch-uid{width}'
        clone(source,copied)
        src=trace_script.read_text()
        if width==32:
            src=src.split('# 64-bit event uids (TWINS_UID64)',1)[0]
        generated=out/f'apply-uid{width}.py'
        generated.write_text(src)
        subprocess.run([sys.executable,generated,copied],check=True)
        toggle_variants(copied)
        build(copied,out/f'build-uid{width}')
        envs={'all':'','pointer-order':'TWINS_ABLATE_ROUTE_ADDR','original-pfc':'TWINS_ABLATE_PFC_RESUME',
              'old-int-offset':'TWINS_ABLATE_INT_OFFSET','original-ecn':'TWINS_ABLATE_ECN_STREAM'}
        if width==32:
            envs={'all':''}
        entries=list(envs.items())
        for offset in range(0,len(entries),3):
            wave=entries[offset:offset+3]
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(wave)) as pool:
                futures={name:pool.submit(execute,copied,out,raw,f'uid{width}-{name}',
                                         'gpt3_13b_probe',var,4+4*idx,8)
                         for idx,(name,var) in enumerate(wave)}
                for name,result in futures.items():
                    matrix[(width,name)]=result.result()
    base=matrix[(64,'all')][1]
    save(out/'patch-effects.json',{
        f'uid{width}-{name}':dict(run=result,vs_all_patches=compare(path,base))
        for (width,name),(result,path) in matrix.items()
    })


if __name__=='__main__':
    main()
