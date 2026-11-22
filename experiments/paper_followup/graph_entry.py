"""Exercise the adaptive startup through the final candidate's normal graph entry."""
import json
from pathlib import Path
import subprocess
import sys

home=Path.home()/'Videos'
candidate=home/'twins-paper-followup-20261123/candidate'
previous=home/'twins-paper-followthrough-20261122/project'
script=home/'twins-paper-tcp-policy-20261122/20261122T125038Z/experiments/paper_review/tcp_wide_matrix.py'
output=Path(sys.argv[1]).resolve()
output.mkdir(parents=True,exist_ok=False)
if len(sys.argv)>2:
    command=[sys.executable,str(script),str(previous/'data-input/tcp'),
             str(previous/'data-input/ns3-101'),str(previous/'data-input/ns3-102'),
             str(candidate/'build'),str(output/'matrix'),'--process-timing','--all-scales',
             '--memory-backend=cuda','--core','0']
else:
    command=['flock',str(home/'twins-isolated-20261118/out/exclusive-campaign.lock'),
             sys.executable,str(previous/'experiments/paper_review/observe_cuda.py'),str(output/'resources'),
             sys.executable,str(Path(__file__)),str(output/'measurements'),'run']
(output/'command.json').write_text(json.dumps(command,indent=2)+'\n')
raise SystemExit(subprocess.run(command).returncode)
