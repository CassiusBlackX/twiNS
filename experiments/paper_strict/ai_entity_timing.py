"""Same-binary controls for single-slot, entity-window and global-window execution."""

import json
import os
from pathlib import Path
import subprocess
import sys

import ai_drift
from crosspod import CANDIDATE, LOCK, OBSERVER, sha
from pfc_sustained import save


CASES={'gpt3_13b_probe_hpn128':128,'c_allgather_512m_hpn1024':1024}
ARMS={'single':('--win=1',), 'entity':('--win=32',), 'global':('--gwin=32',)}


def sample(out, name, arm, binary):
    env={k:v for k,v in os.environ.items() if k not in ('TWINS_CL_ONE_SLOT','TWINS_STOP_SYNCS')}
    return ai_drift.run_one(out,binary,name,CASES[name],('--rank=0',*ARMS[arm]),env)


def main():
    out=Path(sys.argv[1]).resolve()
    binary=CANDIDATE/'build/twins-ai'
    if len(sys.argv)>2:
        name,arm=sys.argv[2:4]
        row=sample(out,name,arm,binary)
        print(name,arm,row['accepted'],row['wall_ns']/1e9,row['metrics'],flush=True)
        return 0
    out.mkdir()
    rows={}
    # Start with the shorter correctness gates while the scale inputs and observer build are prepared.
    for name,order in ((list(CASES)[1],('global','entity','single')),(list(CASES)[0],('single','entity','global'))):
        rows[name]={}
        for arm in order:
            target=out/name/arm
            rc=subprocess.run(['flock',LOCK,sys.executable,OBSERVER,out/'resources'/name/arm,
                               sys.executable,__file__,target,name,arm]).returncode
            assert rc==0
            last=json.loads((target/'receipt.json').read_text())
            assert last['binary_sha256']==sha(binary)
            rows[name][arm]=last
            save(out/'summary.json',rows)


if __name__=='__main__':
    raise SystemExit(main())
