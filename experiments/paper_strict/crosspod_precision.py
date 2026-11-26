"""Replace quantized process timings and check that idle skipping can jump a known gap."""

from collections import Counter
import json
from pathlib import Path
import shutil
import subprocess
import sys

import crosspod
from crosspod_ablation import FROZEN, trial
from crosspod_dispersed import ORDER, check_endpoints, gedes_ip_map, records
from crosspod_large import binaries, runner
from crosspod_large_inputs import write_csv
from pfc_sustained import save


ABLATION = crosspod.ROOT / 'crosspod_ablation/20261123T182929Z/results'
DISPERSED = crosspod.ROOT / 'crosspod_dispersed/20261123T182646Z/results'
ALIGNED_BUILD = crosspod.ROOT / 'crosspod-aligned-20261123/build'
ORDER4 = (('graph-skip','direct-skip','direct-step','graph-step'),
          ('direct-skip','graph-step','graph-skip','direct-step'),
          ('graph-step','direct-step','direct-skip','graph-skip'),
          ('direct-step','graph-skip','graph-step','direct-skip'),
          ('graph-skip','graph-step','direct-step','direct-skip'))


def gap_control(root, meter):
    root.mkdir()
    case = root/'inputs'
    case.mkdir()
    fields = ('src','dst','flow_size','start_ns','conn')
    orig = list(tuple(int(r[key]) for key in fields) for r in records(ABLATION/'inputs/512/twins.csv'))
    records_ = [(a,b,size,start+5_000_000 if start>=5_121_000 else start,c) for a,b,size,start,c in orig]
    assert sum(left[3]!=right[3] for left,right in zip(orig,records_,strict=True))==256
    write_csv(case/'twins.csv',fields,records_)
    shutil.copy2(ABLATION/'inputs/512/twins-hash.csv',case/'twins-hash.csv')
    spec=dict(case='gap-control-512',cap_ns=100_000_000,frozen_output_sha256=None,
              workload_sha256=crosspod.sha(case/'twins.csv'), ecmp_sha256=crosspod.sha(case/'twins-hash.csv'))
    save(case/'input.json',spec)
    runs = {a:trial(root/a,case,spec,meter,a,True) for a in ('graph-skip','graph-step')}
    a,b = runs.values()
    assert a['accepted'] and b['accepted'] and a['fct_sha256']==b['fct_sha256']
    assert a['work']==b['work'] and a['stop']==b['stop']
    assert a['slot_activity']['active']==b['slot_activity']['active']
    assert a['slot_activity']['skipped']>0 and b['slot_activity']['skipped']==0
    save(root/'summary.json',runs)


def measure(out):
    clean=ABLATION/'build/clean/compiled/twins'
    meter=ABLATION/'build/metered/compiled/twins'
    collected={}
    for key in FROZEN:
        source=ABLATION/'inputs'/key
        spec=json.loads((source/'input.json').read_text())
        rows=[]
        for repeat,arms in enumerate(ORDER4):
            for arm in arms:
                row=trial(out/'ablation'/key/arm/f'trial-{repeat}',source,spec,clean,arm,False)
                rows.append(row|{'repeat':repeat})
        assert len(rows)==20 and all(r['accepted'] for r in rows)
        collected[key]=rows
        save(out/'ablation-timings.json',collected)
    gap_control(out/'gap-control',meter)

    engines,_=binaries(ALIGNED_BUILD)
    source=DISPERSED/'inputs'
    d=out/'dispersed'
    d.mkdir()
    spec=json.loads((source/'input.json').read_text())
    summary=json.loads((DISPERSED/'measurements/summary.json').read_text())
    for name in ('twins-hash.csv','gedes-hash.csv'):
        shutil.copy2(source/name,d/name)
    expected=Counter(tuple(int(row[f]) for f in ('src','dst','start_ns','flow_size')) for row in records(source/'twins.csv'))
    ip_map=gedes_ip_map()
    rows=[]
    for repeat,order in enumerate(ORDER):
        for engine in order:
            row=runner(d,source,spec,engines,engine,repeat,True,True)
            assert row['accepted']
            if engine=='days':
                assert summary['days_state_sha_lane']==[row['state_fnv1a64']]
            else:
                assert check_endpoints(expected,d/f'trial-{repeat}'/engine/'fct.csv',engine,ip_map)
                archived=next(x for x in summary['diagnostic'] if x['engine']==engine)
                assert row['fct_sha256']==archived['fct_sha256']
            rows.append(row)
    save(out/'dispersed-timings.json',rows)


def main():
    out=Path(sys.argv[1]).resolve()
    if len(sys.argv)>2:
        return measure(out)
    out.mkdir()
    return subprocess.run(['flock',crosspod.LOCK,sys.executable,crosspod.OBSERVER,out/'resources',
                           sys.executable,__file__,out,'measure']).returncode


if __name__=='__main__':
    raise SystemExit(main())
