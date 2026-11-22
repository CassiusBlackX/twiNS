"""Three GPU repetitions on a reserved core during the CPU-only container audit."""
from pathlib import Path
import json
import subprocess
import sys
import crosspod

out=Path(sys.argv[1]).resolve()
out.mkdir(parents=True)
root=crosspod.ROOT
inputs=sorted((root/'crosspod_paced').glob('20*'))[-1]/'results/inputs'
build=sorted((root/'crosspod').glob('20*'))[-1]/'results/builds'
state=sorted((root/'days_state').glob('20*'))[-1]/'results/summary.json'
checks=[x for key,x in json.loads(state.read_text()).items() if key.startswith('paced_probe')]
if len(checks)!=2 or not all(x['state_digest_equal'] and x['complete']==x['total'] for x in checks):
    raise SystemExit('paced inputs are not ready for three-way timing')
memory=Path('/proc/meminfo').read_text()
if int(next(x for x in memory.splitlines() if x.startswith('MemAvailable:')).split()[1]) < 8*1024**2:
    raise SystemExit('less than 8 GiB of host memory available')
usage=dict(gpu_host_core=0,hpcc_audit_core=4,hpcc_host_reservation_gib=70,
           gpu_host_cap_gib=5,meminfo_at_admission=memory,resource_epoch='paired GPU with the CPU-only audit on a separate physical core')
(out/'shared-provenance.json').write_text(json.dumps(usage,indent=2)+'\n')
rc=subprocess.run(['systemd-run','--user','--scope','-p','MemoryMax=5G',
    sys.executable,crosspod.OBSERVER,out/'resources',sys.executable,Path(__file__).with_name('crosspod.py'),
    out/'measurements',build,inputs]).returncode
raise SystemExit(rc)
