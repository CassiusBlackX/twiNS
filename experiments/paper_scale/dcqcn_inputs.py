"""Freeze a separate DCQCN-enabled scale series; preserve the original IRN anchor series."""
from pathlib import Path
import hashlib,json,shutil,sys
source,target=map(Path,sys.argv[1:])
shutil.copytree(source,target)
for config in target.glob('*/*/config'):
    lines=config.read_text().splitlines()
    values={'ENABLE_QCN':'1','KMIN_MAP':'1 100000000000 32','KMAX_MAP':'1 100000000000 64','PMAX_MAP':'1 100000000000 0.2'}
    assert dict(x.split(' ',1) for x in lines)['CC_MODE']=='1'
    config.write_text(''.join(x.split()[0]+' '+values[x.split()[0]]+'\n' if x.split()[0] in values else x+'\n' for x in lines))
p=target/'manifest.json';m=json.loads(p.read_text())
m['irn_protocol']=dict(mode='DCQCN',ecn=True,pfc=False,kmin_kB=32,kmax_kB=64,pmax=.2)
m['sha256']={n:hashlib.sha256((target/n).read_bytes()).hexdigest() for n in m['sha256']}
p.write_text(json.dumps(m,indent=2))
