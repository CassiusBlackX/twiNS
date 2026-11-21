#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
root=$(pwd -P)
evidence=$root/out/review/irn-boundary-20261122
host=/home/cassius/Videos/twins-irn-boundary-20261122
rsh='ssh -F /home/cassius/.ssh/config -o BatchMode=yes -o ConnectTimeout=10'
ssh_cmd=(ssh -F /home/cassius/.ssh/config -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=20 s50)
test -s "$evidence/boundary-summary.json"
"${ssh_cmd[@]}" '
s="$HOME/Videos/twins-irn-boundary-20261122"
cd "$s"
exec flock "$HOME/Videos/twins-isolated-20261118/out/exclusive-campaign.lock" /usr/bin/bash -s
' <<'REMOTE'
set -euo pipefail
ulimit -c 0
exe="$HOME/Videos/twins-paper-review-20261120/project/build/twins-rdma"
sha256sum "$exe" > cuda-binary.sha256
until [[ -z $(nvidia-smi --query-compute-apps=pid --format=csv,noheader) ]]; do sleep 10; done
for kmin in 1 2; do
 for mode in serial par; do
  flag=--serial
  if test "$mode" = par; then flag=--irn-parallel; fi
  (
   cd "kmin-$kmin"
   timeout 60 "$exe" config --topo=../graph --flows=flows.csv --out="cuda-$mode.fct" \
      --txtrace="cuda-$mode.tx" --from=2000000000 --until=2002000000 \
      --fp-bases=65537,1048573 "$flag" > "cuda-$mode.log" 2>&1
  )
 done
done
find . -type f -name 'cuda-*' -print0 | sort -z | xargs -0 sha256sum > cuda-files.sha256
REMOTE
rsync -a --include='*/' --include=cuda-\* --exclude='*' -e "$rsh" "s50:$host/" "$evidence/"
(cd "$evidence" && sha256sum --quiet -c cuda-files.sha256)
python3 - "$evidence" <<'PY'
from collections import defaultdict
from pathlib import Path
import re,json,sys
r=Path(sys.argv[1])
def ordered(path):
    links=defaultdict(list)
    for row in path.read_text().splitlines():
        fields=row.split()
        assert len(fields)==14
        links[tuple(fields[1:3])].append(tuple(fields))
    return {key:sorted(values,key=lambda f:int(f[0])) for key,values in links.items()}
s={}
for threshold in (1,2):
    d=r/f'kmin-{threshold}'
    expected=ordered(d/'reference.tx')
    exact={}
    for mode in ('serial','par'):
        wire=ordered(d/f'cuda-{mode}.tx')
        assert wire==expected, (threshold, mode)
        assert 'undecided ties 0, on lost tails 0' in (d/f'cuda-{mode}.log').read_text()
        exact[mode]={'records':sum(map(len,wire.values())),'accepted':True}
    number=re.search(r'IRN active switch slots: (\d+) parallel, (\d+) fallback',(d/'cuda-par.log').read_text())
    exact.update(parallel=int(number[1]),fallback=int(number[2]))
    s[str(threshold)]=exact
assert s['1']['fallback']>0 and s['2']['fallback']==0
(r/'cuda-boundary-summary.json').write_text(json.dumps(s,indent=2)+'\n')
print(s)
PY
