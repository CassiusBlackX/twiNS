"""Two-rack AllToAll with the same graph in the two simulators' file formats."""
from pathlib import Path
import sys
base=Path(sys.argv[1]);old=Path(sys.argv[2])
d=base/'ai';d.mkdir(exist_ok=False)
hosts=16
links=[(i,16+i//8) for i in range(hosts)]+[(16,18),(17,18)]
edges=''.join(f'{i} {j} 100Gbps 1000ns 0\n' for i,j in links)
(d/'fabric.txt').write_text('19 3 18\n16 17 18\n'+edges)
(d/'simai.topo').write_text('19 1 0 3 18 H100\n16 17 18\n'+edges)
for case,size in [('clean',16384),('loss',8388608)]:
    p=d/case;p.mkdir()
    w=(old/'out/extension-evidence/ai-oracles/c_alltoall_64m_128.txt').read_text().replace('all_gpus: 128','all_gpus: 16').replace('model_parallel_NPU_group: 8','model_parallel_NPU_group: 1').replace('67108864',str(size))
    (p/'workload.txt').write_text(w)
    config=(old/'out/irn-wire-final/incast/config').read_text().replace('../leaf_spine_128_100G_OS2.txt','../fabric.txt')
    (p/'config').write_text(config.replace('ENABLE_QCN 0','ENABLE_QCN 1'))
    (p/'ref.flows').write_text('0\n')
