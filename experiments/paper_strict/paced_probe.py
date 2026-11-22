"""Untimed viability checks during a CPU-only timer audit on a different core."""
from pathlib import Path
import json
import subprocess
import sys
import crosspod

out=Path(sys.argv[1]).resolve()
out.mkdir(parents=True)
if len(sys.argv)==2:
    raise SystemExit(subprocess.run([sys.executable,crosspod.OBSERVER,out/'resources',
        sys.executable,__file__,out/'preflight','run']).returncode)
root=crosspod.ROOT
inputs=sorted((root/'crosspod_paced').glob('20*'))[-1]/'results/inputs'
build=sorted((root/'crosspod').glob('20*'))[-1]/'results/builds'
engines={'days':crosspod.DAYS,'twins':build/'twins/cmake/twins','gedes':build/'GeDES/cmake/GeDES'}
rows=[]
for case in sorted(inputs.glob('k*')):
    spec=json.loads((case/'input.json').read_text())
    case_out=out/case.name
    case_out.mkdir()
    crosspod.route_hashes(case,case_out)
    for engine in engines:
        rows.append(dict(input=case.name,**crosspod.run(case_out,engines,case,spec,engine,'probe')))
(out/'summary.json').write_text(json.dumps(rows,indent=2)+'\n')
