"""Re-run the frozen k8 CUBIC A2A30 input with exact completion timestamps."""

import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile


REFERENCE_TREE = Path.home()/'Videos/netsim/unison'
TRACE_LIMIT_PS = 1_000_000_000_000


def replace(path, old, new):
    s=path.read_text()
    if s.count(old)!=1:
        raise ValueError(f'Unexpected source anchor in {path}: {old!r}')
    path.write_text(s.replace(old,new,1))


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def save(path,data):
    path.write_text(json.dumps(data,indent=2)+'\n')


def report(out,binary,flow,commands,origin,modified):
    save(out/'run-provenance.json', {'binary':str(binary),'binary_sha256':sha(binary),'flow_sha256':sha(flow),
        'trace_limit_ps':TRACE_LIMIT_PS,'commands':[[str(v) for v in cmd] for cmd in commands],
        'source_original_sha256':sha(origin),'source_modified_sha256':sha(modified),
        'queue_trace_sha256':sha(out/'full.qtrace.gz'),'fct_sha256':sha(out/'completion.fct')})


def compress(raw, out, separate_ns3_log=False):
    with raw.open('rb') as src,(out/'full.qtrace.gz').open('wb') as target:
        with gzip.GzipFile(filename='',mode='wb',fileobj=target,mtime=0) as dst:
            if not separate_ns3_log:
                shutil.copyfileobj(src,dst)
                return
            with (out/'process.log').open('wb') as log:
                for line in src:
                    (dst if line.startswith(b'Q ') else log).write(line)


def reference(out,flow):
    out.mkdir(parents=True)
    native=REFERENCE_TREE/'scratch/twins-ref.cc'
    source=out/'twins-ref-audit.cc'
    shutil.copy2(native,source)
    replace(source,'    int64_t done_ns = -1;','    int64_t done_ns = -1;\n    int64_t done_ps = -1;')
    replace(source,'''        while (m_done < m_written && now.GetValue() >= g_flows[m_msgs[m_done]].end + 1)
            g_flows[m_msgs[m_done++]].done_ns = Simulator::Now().GetNanoSeconds();''','''        while (m_done < m_written && now.GetValue() >= g_flows[m_msgs[m_done]].end + 1)
        {
            auto &record = g_flows[m_msgs[m_done++]];
            record.done_ns = Simulator::Now().GetNanoSeconds();
            record.done_ps = Simulator::Now().GetPicoSeconds();
        }''')
    replace(source,'completed_ns,hash_data,hash_ack,retx\\n','completed_ns,hash_data,hash_ack,retx,completed_ps\\n')
    replace(source,'<< f.hash_data << "," << f.hash_ack << "," << f.retx << "\\n";','<< f.hash_data << "," << f.hash_ack << "," << f.retx << "," << f.done_ps << "\\n";')
    # Preserve ns-3's output and queue trace interfaces. The added field only observes the callback clock.
    cdir=REFERENCE_TREE/'cmake-cache'
    rules=subprocess.check_output(['ninja','-C',cdir,'-t','commands'],text=True).splitlines()
    compile_=next(shlex.split(s) for s in rules if s.endswith(f' -c {native}'))
    link=next(shlex.split(s) for s in rules if s.startswith(': && /usr/bin/c++ ') and '/ns3.46.1-twins-ref-optimized ' in s)
    assert link[0:2]==[':','&&'] and link[-2:]==['&&',':']
    link=link[2:-2]
    obj=compile_[compile_.index('-o')+1]
    relocated=out/'twins-ref-audit.o'
    binary=out/'twins-ref-audit'
    compile_=[str(source) if s==str(native) else str(relocated) if s==obj else str(relocated)+'.d' if s==obj+'.d' else s for s in compile_]
    link=[str(relocated) if s==obj else str(binary) if s.endswith('/ns3.46.1-twins-ref-optimized') else s for s in link]
    commands=[['taskset','-c','4',*item] for item in (compile_,link)]
    with (out/'build.log').open('wb') as stream:
        for cmd in commands:
            subprocess.run(cmd,cwd=cdir,stdout=stream,stderr=subprocess.STDOUT,timeout=180,check=True)
    cmd=['taskset','-c','4',binary,'--k=8',f'--flows={flow}','--tcp=ns3::TcpCubic','--queue=200',
         '--ecnK=30','--flowEcmp=1','--hostq=red','--threads=0','--fin=0','--stop=1','--out=completion.fct']
    env={k:v for k,v in os.environ.items() if not k.startswith(('TRACE_','NS3_TIE_'))}
    env['TRACE_ALLQ']=str(TRACE_LIMIT_PS)
    with tempfile.TemporaryDirectory(dir=out) as tmp:
        events=Path(tmp)/'stdout'
        with events.open('wb') as stream:
            subprocess.run(cmd,cwd=out,env=env,stdout=stream,stderr=subprocess.STDOUT,timeout=240,check=True)
        compress(events,out,separate_ns3_log=True)
    report(out,binary,flow,[*commands,cmd],native,source)
    libs=sorted((REFERENCE_TREE/'build/lib').glob('libns3.46.1-*-optimized.so'))
    save(out/'reference-library-sha256.json',{p.name:sha(p) for p in libs})
    print('ns-3 archived',out,flush=True)


def hip(out,flow,origin):
    out.mkdir(parents=True)
    tree=out/'source'
    tree.mkdir()
    shutil.copytree(origin/'src',tree/'src')
    shutil.copy2(origin/'CMakeLists.txt',tree/'CMakeLists.txt')
    native=origin/'src/main.cu'
    driver=tree/'src/main.cu'
    replace(driver,'start_ns,established_ns,completed_ns\\n','start_ns,established_ns,completed_ns,completed_ps\\n')
    replace(driver,'<< (done[j] >= 0 ? done[j] / 1000 : -1) << "\\n";',
            '<< (done[j] >= 0 ? done[j] / 1000 : -1) << "," << done[j] << "\\n";')
    replace(driver,'        n = std::min(n, s.qtrace_n);', '''        printf("qtrace uncapped %llu capacity %llu\\n", n, (unsigned long long)s.qtrace_n);
        n = std::min(n, s.qtrace_n);''')
    bdir=out/'compiled'
    commands=[['cmake','-S',tree,'-B',bdir,'-G','Ninja','-DTWINS_GPU_BACKEND=HIP','-DCMAKE_HIP_ARCHITECTURES=gfx1200',
               '-DCMAKE_HIP_COMPILER=/opt/rocm/llvm/bin/clang++'],['cmake','--build',bdir,'--target','twins128','-j2']]
    with (out/'build.log').open('wb') as stream:
        for cmd in commands:
            subprocess.run(cmd,stdout=stream,stderr=subprocess.STDOUT,check=True,timeout=450)
    binary=bdir/'twins128'
    env={k:v for k,v in os.environ.items() if not k.startswith(('TWINS_', 'NS_TRACE'))}
    with tempfile.TemporaryDirectory(dir=out) as tmp:
        events=Path(tmp)/'queues'
        cmd=['flock','/tmp/specdes-pfc-and-replay-hip.lock',binary,'--k=8','--tcp=cubic','--queue=200','--ecnK=30',
             f'--flows={flow}','--out=completion.fct',f'--qtrace={events}',f'--qtrace_until={TRACE_LIMIT_PS}']
        with (out/'process.log').open('wb') as stream:
            subprocess.run(cmd,cwd=out,env=env,stdout=stream,stderr=subprocess.STDOUT,timeout=360,check=True)
        text=(out/'process.log').read_text()
        match=re.findall(r'qtrace uncapped (\d+) capacity (\d+)',text)
        assert len(match)==1 and int(match[0][0])<int(match[0][1])
        compress(events,out)
    report(out,binary,flow,[*commands,cmd],native,driver)
    print('HIP archived',out,flush=True)


def main():
    mode=sys.argv[1]
    out=Path(sys.argv[2]).resolve()
    flow=Path(sys.argv[3]).resolve()
    if mode=='reference':
        reference(out,flow)
    elif mode=='hip':
        hip(out,flow,Path(sys.argv[4]).resolve())
    else:
        raise ValueError(mode)


if __name__=='__main__':
    main()
