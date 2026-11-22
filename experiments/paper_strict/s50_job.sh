#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
root=$(pwd -P)
role=$1
stamp=$(date -u +%Y%m%dT%H%M%SZ)
remote="/home/cassius/Videos/twins-paper-strict-20261123/$role/$stamp"
dest="$root/out/strict-20261123/$role/$stamp"
ssh_cmd=(ssh -F /home/cassius/.ssh/config -o BatchMode=yes -o ConnectTimeout=8 -o ServerAliveInterval=20 s50)
mkdir -p "$dest"
"${ssh_cmd[@]}" "mkdir -p '$remote'"
rsync -a -e 'ssh -F /home/cassius/.ssh/config -o BatchMode=yes -o ConnectTimeout=8' \
    --include='*.py' --exclude='*' experiments/paper_strict/ "s50:$remote/"
case "$role" in
    crosspod_inputs|crosspod_paced)
        scp -F /home/cassius/.ssh/config -o BatchMode=yes experiments/paper_review/days_pair_routes.rs "s50:$remote/"
        ;;
    pfc_reference)
        scp -F /home/cassius/.ssh/config -o BatchMode=yes ../twiNS/workloads/trace2dag.py "s50:$remote/"
        ;;
esac
"${ssh_cmd[@]}" "timeout 20h python3 '$remote/$role.py' '$remote/results'" > "$dest/worker.log" 2>&1 &
worker=$!
while kill -0 "$worker" 2>/dev/null; do
    rsync -a --prune-empty-dirs --include='*/' --include='*.json' --include='*.jsonl' --include='*.log' \
        --include='*.fct' --include='*.csv' --include='*.dag' --include='*.toml' --include='*.graph' --exclude='*' \
        -e 'ssh -F /home/cassius/.ssh/config -o BatchMode=yes -o ConnectTimeout=8' \
        "s50:$remote/results/" "$dest/" 2>> "$dest/transfer.log" || true
    sleep 45
done
rc=0
wait "$worker" || rc=$?
printf '%s\n' "$rc" > "$dest/remote.rc"
rsync -a --checksum --prune-empty-dirs --include='*/' --include='*.json' --include='*.jsonl' --include='*.log' \
    --include='*.fct' --include='*.csv' --include='*.dag' --include='*.toml' --include='*.graph' --exclude='*' \
    -e 'ssh -F /home/cassius/.ssh/config -o BatchMode=yes -o ConnectTimeout=8' \
    "s50:$remote/results/" "$dest/"
"${ssh_cmd[@]}" "set -o pipefail; cd '$remote/results' &&
    find . -type f \( -name '*.json' -o -name '*.jsonl' -o -name '*.log' -o -name '*.fct' -o -name '*.csv' -o -name '*.dag' -o -name '*.toml' -o -name '*.graph' \) \
    -print0 | sort -z | xargs -0 -r sha256sum" > "$dest/returned-files.sha256"
test -s "$dest/returned-files.sha256"
(cd "$dest" && sha256sum --quiet -c returned-files.sha256)
exit "$rc"
