"""Generate two FIFO-UID lineages, then time the production comparator's fixed-input bench."""
import heapq
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile

CAP=64
RUNS=65536
MODULUS=(1<<61)-1
BASES=(1000003,1000033)

def uid_order(depth):
    """Assign UID at schedule time and dispatch by (time, UID)."""
    events=[]
    uid=0
    def schedule(when,branch,remaining):
        nonlocal uid
        heapq.heappush(events,(when,uid,branch,remaining))
        uid+=1
    schedule(0,0,depth)
    schedule(0,1,depth)
    finished=[]
    while events:
        when,_,branch,left=heapq.heappop(events)
        if not left:
            finished.append(branch)
        else:
            schedule(when+1000+(left%2),branch,left-1)
    if sorted(finished)!=[0,1]:
        raise ValueError('UID fixture did not finish both lineages')
    # With a common root these are two consecutive sibling schedules in its handler.
    # With distinct roots they are two timer roots with the same timestamp.
    return -1 if finished==[0,1] else 1

def generate(path,depth,shared):
    if not 1<=depth<RUNS:
        raise ValueError('fixture exceeds the fixed corpus layout')
    leads=[1000+(j%2)+1 for j in range(1,depth+1)]
    a=[(lead<<5 | 31,1) for lead in leads]
    b=list(a)
    if shared:
        value,n=b[-1]
        b[-1]=(value-1,n)
    fp=[]
    for base in BASES:
        h=0
        for lead in reversed(leads):
            h=(h*base+lead)%MODULUS
        fp.append(h)
    verdict=uid_order(depth)
    # The input comprises complete synthetic chains, and the full lead sequence is fingerprinted.
    prefix=struct.pack('<IIIIIiiii4xQQQQ',depth,depth,1,1,1 if shared else 2,0,1,verdict,0,*fp,*fp)
    blocks=[b''.join(struct.pack('<II',*r) for r in pair) + bytes((RUNS-depth)*8) for pair in (a,b)]
    valid=prefix+b''.join(blocks)
    with path.open('wb') as stream:
        stream.write(valid)
        stream.write(bytes(len(valid))*(CAP-1))
    return verdict

def main():
    output=Path(sys.argv[1]).resolve()
    source=Path(sys.argv[2]).resolve()
    output.mkdir(parents=True,exist_ok=False)
    with (output/'build.log').open('wb') as log:
        subprocess.run(['cmake','-S',source,'-B',output/'build','-G','Ninja',
                        '-DTWINS_GPU_BACKEND=HIP','-DCMAKE_HIP_ARCHITECTURES=gfx1200',
                        '-DCMAKE_HIP_COMPILER=/opt/rocm/llvm/bin/clang++','-DTWINS_BUILD_EVIDENCE_TOOLS=ON'],
                       stdout=log,stderr=subprocess.STDOUT,check=True)
        subprocess.run(['cmake','--build',output/'build','--target','twins-lineage-bench','-j2'],
                       stdout=log,stderr=subprocess.STDOUT,check=True)
    results=[]
    for depth in (64,1024,8192,32768):
        for shared in (True,False):
            name=f'{"same" if shared else "different"}-root-{depth}'
            with tempfile.TemporaryDirectory(dir=output) as tmp:
                corpus=Path(tmp)/'sample.corpus'
                verdict=generate(corpus,depth,shared)
                result=subprocess.run([output/'build/twins-lineage-bench',corpus],capture_output=True,text=True,timeout=180)
            (output/f'{name}.log').write_text(result.stdout+result.stderr)
            rows=[]
            for line in result.stdout.splitlines():
                if line.startswith('{'):
                    rows.append(json.loads(line))
            results.append(dict(depth=depth,same_root=shared,reference_fifo_uid_verdict=verdict,rc=result.returncode,bench=rows))
            (output/'summary.json').write_text(json.dumps(results,indent=2)+'\n')

if __name__=='__main__':
    main()
