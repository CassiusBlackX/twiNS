#!/bin/bash
set -u
cd "$(dirname "$0")/../.."
R=/home/cassius/Work/SpecDES/twiNS/out/paper-evidence-20261116
while systemctl --user is-active --quiet twins-evidence-profile; do sleep 10; done
# Preserve preliminary failed input-path attempts; they are not scientific samples.
python3 - <<'PY'
from pathlib import Path
import json,time
r=Path('/home/cassius/Work/SpecDES/twiNS/out/paper-evidence-20261116/local/profile')
for p in r.glob('rdma-pfc-*/result.json'):
 d=json.loads(p.read_text())
 if d['rc']==1 and 'cannot open' in (p.parent/'process.log').read_text():
  dest=r/'input-path-failures'/p.parent.name;dest.parent.mkdir(exist_ok=True);p.parent.rename(dest)
PY
bash experiments/paper_evidence/final_builds.sh || exit $?
python3 experiments/paper_evidence/final_micro.py || exit $?
python3 experiments/paper_evidence/final_profile.py || exit $?
python3 experiments/paper_evidence/growth_runs.py || exit $?
python3 experiments/paper_evidence/unified_runs.py || exit $?
python3 experiments/paper_evidence/timer_regression.py || exit $?
TWINS_PROFILE_STRICT_ONLY=1 python3 experiments/paper_evidence/final_profile.py || exit $?
TWINS_GROWTH_STRICT_ONLY=1 python3 experiments/paper_evidence/growth_runs.py || exit $?
python3 - <<'PY'
import json,time
from pathlib import Path
Path('/home/cassius/Work/SpecDES/twiNS/out/paper-evidence-20261116/local/all-finished.json').write_text(json.dumps(dict(utc_ns=time.time_ns(),meaning='scheduled stages executed; scientific acceptance is per result')))
PY
