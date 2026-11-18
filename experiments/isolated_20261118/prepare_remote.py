#!/usr/bin/env python3
"""Private builds: TCP log-only instrumentation and MTP from the exact sequential source.
No existing source/build is modified. Run before any formal measurement setting.
"""
import difflib,json,os,shlex,shutil,subprocess as sp,time
from pathlib import Path
from common import *
def run(name,cmd,cwd=None,timeout=3600):
 p=E/'receipts'/name;p.mkdir(exist_ok=True);t=time.time_ns()
 with (p/'process.log').open('w') as f:r=sp.run(cmd,cwd=cwd or ROOT,stdout=f,stderr=sp.STDOUT,timeout=timeout)
 save(p/'result.json',dict(command=cmd,cwd=str(cwd),rc=r.returncode,wall_ns=time.time_ns()-t))
 if r.returncode:raise RuntimeError(name+' failed')
def tcp():
 U=HOME/'Videos/netsim/unison';p=ROOT/'tcp-reference';p.mkdir(exist_ok=True);src=U/'scratch/twins-ref.cc';old=src.read_text();new=old.replace('#include <chrono>','#include <chrono>\n#include <iomanip>');new=new.replace('    Simulator::Destroy();','    auto final_ps = Simulator::Now().GetPicoSeconds();\n    Simulator::Destroy();');new=new.replace('    std::cout << "flows "','    std::cout << std::setprecision(12) << "evidence final_simulated_ps " << final_ps << " event_loop_ns " << (uint64_t)(wall * 1e9) << std::endl;\n    std::cout << "flows "')
 assert old!=new and new.count('evidence final_simulated_ps')==1
 source=p/'twins-evidence-ref.cc';source.write_text(new);(p/'logging-only.patch').write_text(''.join(difflib.unified_diff(old.splitlines(True),new.splitlines(True),fromfile=str(src),tofile=source.name)))
 entry=next(x for x in read(U/'cmake-cache/compile_commands.json') if x['file']==str(src));cmd=shlex.split(entry['command']);cmd[cmd.index('-o')+1]=str(p/'reference.o');cmd[-1]=str(source)
 run('tcp-compile',cmd,cwd=entry['directory'])
 link=sp.check_output(['ninja','-C',str(U/'cmake-cache'),'-t','commands',str(U/'build/scratch/ns3.46.1-twins-ref-optimized')],text=True).splitlines()[-1]
 parts=shlex.split(link);parts=[x for x in parts if x not in (':','&&')];parts=[str(p/'reference.o') if x=='scratch/CMakeFiles/scratch_twins-ref.dir/twins-ref.cc.o' else x for x in parts];parts[parts.index('-o')+1]=str(p/'ns3-unison-ref')
 run('tcp-link',parts,cwd=U/'cmake-cache');save(E/'receipts/tcp-source.json',dict(original_source_sha256=sha(src),logging_source_sha256=sha(source),binary_sha256=sha(p/'ns3-unison-ref'),scope='same Unison libraries and reference model; only print precision and post-Run clock added',libraries={str(q):sha(q) for q in (U/'build/lib').glob('*.so')}))
def mtp():
 src=HOME/'Videos/netsim/simai-t64';dest=ROOT/'simai-mtp';dest.mkdir(exist_ok=True)
 run('mtp-source-copy',['rsync','-a','--exclude=.git','--exclude=cmake-cache','--exclude=build','--exclude=docs','--exclude=vidur-alibabacloud','--exclude=aicb',str(src)+'/',str(dest)+'/'])
 sim='astra-sim-alibabacloud/extern/network_backend/ns3-interface/simulation';d=dest/sim
 assert (d/'src/applications/astra-sim/system/Sys.cc').exists()
 source_files=[q for q in d.rglob('*') if q.is_file() and not set(q.parts)&{'build','cmake-cache','cmake-mtp','build-mtp'}]
 hashes={str(q.relative_to(d)):sha(q) for q in source_files};different=[rel for rel,h in hashes.items() if not (src/sim/rel).is_file() or sha(src/sim/rel)!=h];assert not different
 cfg=['cmake','-S','/simai/'+sim,'-B','/simai/'+sim+'/cmake-mtp','-G','Ninja','-DCMAKE_BUILD_TYPE=release','-DNS3_MTP=ON','-DNS3_NATIVE_OPTIMIZATIONS=ON','-DNS3_WARNINGS_AS_ERRORS=OFF','-DCMAKE_CXX_FLAGS=-include cstdint','-DNS3_OUTPUT_DIRECTORY=/simai/'+sim+'/build-mtp']
 base=['docker','run','--rm','--label','twins.evidence.campaign=20261118','--user',f'{os.getuid()}:{os.getgid()}','--memory=16g','--memory-swap=16g','-v',str(dest)+':/simai','localhost/simai-build']
 run('mtp-configure',base+cfg)
 run('mtp-build',base+['cmake','--build','/simai/'+sim+'/cmake-mtp','--target','scratch_AstraSimNetwork','-j6'],timeout=7200)
 bin=d/'build-mtp/scratch/ns3.36.1-AstraSimNetwork-optimized';assert bin.exists()
 save(E/'receipts/mtp-source.json',dict(source_hashes=hashes,mtp_and_sequential_source_identical=True,model_patch_difference=[],compile_difference='NS3_MTP=ON versus OFF; same native/O3/cstdint settings; warning policy not model behavior',binary_sha256=sha(bin),original_sequential_binary_sha256=sha(src/c.SIMBIN.removeprefix('/simai/')),source_base=str(src),scope='Same patched source including UID64, NCCL/mock, ordering/stream changes; MTP changes scheduler semantics. -t1 is MTP, original OFF build remains the sequential control.'))
if __name__=='__main__':
 if not (E/'receipts/tcp-source.json').exists():tcp()
 if not (E/'receipts/mtp-source.json').exists():mtp()
 save(E/'receipts/prepared.json',dict(utc_ns=time.time_ns(),status='ready'))
