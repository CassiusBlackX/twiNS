"""Freeze one additional, untouched lossy IRN case; keep it outside the 12+2 denominator."""
import json,random,hashlib
from pathlib import Path
from decimal import Decimal
import sys
inputs,out=map(Path,sys.argv[1:]);out.mkdir()
rng=random.Random(2026112707)
rows=[(s,2,rng.choice([16384,32768,65536]),2_000_000_000+rng.randrange(257)) for s in range(128) if s!=2]
(out/'flows.csv').write_text('src,dst,flow_size,start_ns\n'+''.join(','.join(map(str,r))+'\n'for r in rows))
(out/'ref.flows').write_text(str(len(rows))+'\n'+''.join(f'{s} {d} 3 {b} {Decimal(t)/Decimal(10**9)+Decimal("0.00000000025")}\n'for s,d,b,t in rows))
(out/'config').write_text((inputs/'irn-native-input/irn.cfg').read_text().replace('ENABLE_QCN 0','ENABLE_QCN 1').replace('100000000000 1600','100000000000 32',1).replace('KMAX_MAP 1 100000000000 1600','KMAX_MAP 1 100000000000 64').replace('PMAX_MAP 1 100000000000 0','PMAX_MAP 1 100000000000 0.2'))
(out/'topology.graph').write_bytes((inputs/'irn.graph').read_bytes())
(out/'native.topology').write_bytes((inputs/'irn-native-input/leaf_spine_128_100G_OS2.txt').read_bytes())
manifest=dict(seed=2026112707,flows=len(rows),selection='all sources except destination, size/launch jitter from fixed seed',source='not used by earlier tuning',protocol=dict(low_rto_ns=100000,high_rto_ns=320000,bdp=104000),denominator='additional predeclared lossy holdout; original 12+2 retained separately')
(out/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
files=sorted(p for p in out.iterdir()if p.is_file())
(out/'sealed.sha256').write_text(''.join(f'{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n'for p in files))
