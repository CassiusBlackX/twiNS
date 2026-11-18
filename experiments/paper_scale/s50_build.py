"""Native CUDA executables, with diagnostic counters in a separate build."""
from pathlib import Path
import hashlib,json,subprocess
from scale import sha

out=Path('current')
for name,cmd in [('cpu',['lscpu']),('gpu',['nvidia-smi','-q']),('cuda',['/usr/local/cuda/bin/nvcc','--version'])]:
    (out/(name+'.txt')).write_bytes(subprocess.check_output(cmd))
for build,flags,targets in [('release',[],['twins','twins-rdma']),('profile',['-DCMAKE_CUDA_FLAGS=-DTWINS_IRN_PROFILE -DTWINS_SCALE_HIST'],['twins-rdma','twins-links','twins512-links'])]:
    subprocess.run(['cmake','-S','.','-B',build,'-G','Ninja','-DTWINS_GPU_BACKEND=CUDA','-DCMAKE_CUDA_ARCHITECTURES=120','-DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc',*flags],check=True)
    subprocess.run(['cmake','--build',build,'--target',*targets,'-j','2'],check=True)
    (out/(build+'-commands.txt')).write_bytes(subprocess.check_output(['ninja','-C',build,'-t','commands',*targets]))
bins=[p for b in ['release','profile'] for p in Path(b).glob('twins*') if p.is_file()]
base=Path('/home/cassius/Videos/twins-extensions-20261119')
bins += [Path('live/binary'),Path('diagnostic-runtime/binary'),base/'tcp-oracle/bin',base/'build/twins-ai']
(out/'binaries.json').write_text(json.dumps({str(p):sha(p) for p in bins},indent=2))
