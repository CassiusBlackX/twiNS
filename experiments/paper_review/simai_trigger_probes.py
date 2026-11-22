"""Execute boundary probes against the already built, frozen SimAI libraries."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

SIM = Path('astra-sim-alibabacloud/extern/network_backend/ns3-interface/simulation')
PREVIOUS = Path.home()/'Videos/twins-simai-reference-20261122/20261122T094910Z/work'

def main():
    output=Path(sys.argv[1]).resolve()
    output.mkdir(parents=True,exist_ok=False)
    probe=Path(__file__).with_name('simai_trigger_probe.cc')
    records=[]
    for width in (64,32):
        source=PREVIOUS/f'patch-uid{width}'
        includes=f'/simai/{SIM}/trial-build/include'
        libs=f'/simai/{SIM}/trial-build/lib'
        headers=output/f'headers-uid{width}'/'ns3'
        headers.mkdir(parents=True)
        header=(source/SIM/'src/core/model/default-simulator-impl.h').read_text()
        if header.count('private:') != 1:
            raise ValueError('UID probe header no longer matches the frozen layout')
        (headers/'default-simulator-impl.h').write_text(header.replace('private:','public:',1))
        base=['docker','run','--rm','--network=none','--ulimit','core=0',
              '--user',f'{os.getuid()}:{os.getgid()}','--cpuset-cpus','4-5',
              '--memory=4g','--memory-swap=4g',
              '-v',f'{source}:/simai:ro','-v',f'{probe.parent}:/probe:ro',
              '-v',f'{output}:/output','localhost/simai-build']
        binary=f'/output/probe-uid{width}'
        command=base+['g++','-std=c++17','-O2','-include','cstdint',
                      f'-I/output/headers-uid{width}','-I'+includes,'-iquote',includes+'/ns3',
                      '/probe/simai_trigger_probe.cc','-o',binary,
                      '-L'+libs,'-Wl,-rpath,'+libs,
                      *['-lns3.36.1-'+name+'-optimized' for name in
                        ('point-to-point','applications','internet','network','core')],
                      '-pthread']
        with (output/f'build-uid{width}.log').open('wb') as stream:
            subprocess.run(command,stdout=stream,stderr=subprocess.STDOUT,check=True,timeout=180)
        for mode,env in [('uid',''),('pfc',''),('int','')]+(
                [('pfc','TWINS_ABLATE_PFC_RESUME'),('int','TWINS_ABLATE_INT_OFFSET')] if width==64 else []):
            label=f'uid{width}-{mode}'+('-original' if env else '')
            cmd=base[:-1]+(['-e',env+'=1'] if env else [])+[base[-1],binary,mode]
            start=time.monotonic_ns()
            with (output/(label+'.log')).open('wb') as stream:
                rc=subprocess.run(cmd,stdout=stream,stderr=subprocess.STDOUT,timeout=60).returncode
            records.append(dict(label=label,rc=rc,command=cmd,wall_ns=time.monotonic_ns()-start))
        libraries=source/SIM/'trial-build/lib'
        (output/f'libraries-uid{width}.json').write_text(json.dumps({
            p.name:hashlib.sha256(p.read_bytes()).hexdigest()
            for p in libraries.glob('*.so') if any(x in p.name for x in ('core','point-to-point','network','applications'))
        },indent=2)+'\n')
    (output/'results.json').write_text(json.dumps({
        'scope':'Boundary probes call frozen native functions. UID counter is explicitly seeded immediately before uint32 wrap.',
        'probe_sha256':hashlib.sha256(probe.read_bytes()).hexdigest(),'records':records,
    },indent=2)+'\n')
    expected={'uid64-pfc-original':139}
    for record in records:
        if record['rc'] != expected.get(record['label'],0):
            raise RuntimeError('unexpected probe exit: '+record['label'])
    def observed(label):
        return (output/(label+'.log')).read_text()
    orders={}
    for width in (64,32):
        orders[width]=[int(line.split()[1]) for line in observed(f'uid{width}-uid').splitlines()
                       if line.startswith('executed ')]
    if orders != {64:[0,1,2,3],32:[2,3,0,1]}:
        raise RuntimeError('UID wrap did not exercise the expected order boundary')
    if 'hops 1 rate 100000000000' not in observed('uid64-int'):
        raise RuntimeError('correct INT header was not updated')
    if 'hops 0' not in observed('uid64-int-original'):
        raise RuntimeError('original INT offset did not exercise the packet corruption')

if __name__=='__main__':
    main()
