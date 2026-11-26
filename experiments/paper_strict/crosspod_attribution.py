"""Reproduce the two frozen three-engine workloads and isolate TwiNS synchronization."""

from collections import Counter
import json
from pathlib import Path
import shutil
import subprocess
import sys

import crosspod
from crosspod_ablation import trial
from crosspod_dispersed import ORDER, check_endpoints, gedes_ip_map, records
from crosspod_large import binaries, runner
from pfc_sustained import save


ROOT = crosspod.ROOT
ALIGNED = ROOT/'crosspod-aligned-20261123'
OTHER = ROOT/'crosspod_dispersed/20261123T182646Z/results'
ABLT = ROOT/'crosspod_ablation/20261123T182929Z/results'
CASES = {'8-pod-pairs':(ALIGNED/'inputs/k16-one-use-distinct-peers',
                        ALIGNED/'adapter-v2/results/k16-one-use-distinct-peers'),
         '240-pod-pairs':(OTHER/'inputs',OTHER/'measurements')}


def prepare(root):
    for tag,(src,native) in CASES.items():
        to=root/'inputs'/tag
        to.mkdir(parents=True)
        for name in ('twins.csv','gedes.csv','days.toml','input.json'):
            shutil.copy2(src/name,to/name)
        for name in ('twins-hash.csv','gedes-hash.csv'):
            shutil.copy2(native/name,to/name)
        controls={}
        for name in ('twins','gedes','days'):
            archive=native/'trial-0'/name
            text=(archive/'process.log').read_text()
            row=json.loads((archive/'receipt.json').read_text())
            controls[name]={'binary_sha256':row['sha256'], 'source_receipt':str(archive/'receipt.json')}
            if name=='days':
                controls[name]['fingerprint']=next(x.split('=',1)[1] for x in text.split() if x.startswith('result_fnv1a64='))
            else:
                controls[name]['fct_sha256']=crosspod.sha(archive/'fct.csv')
        save(to/'expected.json',controls)


def check(row, expected, engine):
    observed = row.get('state_fnv1a64') if engine=='days' else row.get('fct_sha256')
    key = 'fingerprint' if engine=='days' else 'fct_sha256'
    return observed==expected[engine][key] and row['sha256']==expected[engine]['binary_sha256']


def measure(root):
    out=root/'measurements'
    out.mkdir()
    clean,_=binaries(ALIGNED/'build')
    ip_map=gedes_ip_map()
    results={}
    for tag in CASES:
        source=root/'inputs'/tag
        spec=json.loads((source/'input.json').read_text())
        assert spec['k']==16 and spec['flow_rows']==2048 and spec['payload_bytes']==382730240
        baseline=json.loads((source/'expected.json').read_text())
        assert all(crosspod.sha(p)==baseline[name]['binary_sha256'] for name,p in clean.items())
        folder=out/tag
        folder.mkdir()
        for name in ('twins-hash.csv','gedes-hash.csv'):
            shutil.copy2(source/name,folder/name)
        expected=Counter(tuple(int(r[f]) for f in ('src','dst','start_ns','flow_size')) for r in records(source/'twins.csv'))
        results[tag]={'native':[],'sync':[],'work':[]}
        for repeat,order in enumerate(ORDER):
            for name in order:
                row=runner(folder,source,spec,clean,name,repeat,True,True)
                assert row['accepted'] and check(row,baseline,name)
                if name!='days':
                    assert check_endpoints(expected,folder/f'trial-{repeat}'/name/'fct.csv',name,ip_map)
                results[tag]['native'].append(row)
            save(out/'summary.json',results)
        paired(folder,source,spec,baseline,results[tag])
        save(out/'summary.json',results)


def paired(out,source,spec,expected,result):
    case=spec|{'case':source.name,'frozen_output_sha256':expected['twins']['fct_sha256']}
    with (out/'sync-binary-sha256.log').open('w') as stream:
        for label in ('clean','metered'):
            path=ABLT/f'build/{label}/compiled/twins'
            stream.write(f'{crosspod.sha(path)}  {path}\n')
    for i in range(5):
        for width in ((1,64) if i%2==0 else (64,1)):
            clean=ABLT/'build/clean/compiled/twins'
            data=trial(out/f'sync-{width}'/f'trial-{i}',source,case,clean,'direct-step',False,width)
            assert data['accepted']
            result['sync'].append(data|{'repeat':i})
    for width in (1,64):
        meter=ABLT/'build/metered/compiled/twins'
        row=trial(out/f'sync-{width}'/'slot-work',source,case,meter,'direct-step',True,width)
        assert row['accepted']
        row['trailing_no_ops']=row['metrics']['submitted_slots']-row['slot_activity']['processed']
        result['work'].append(row)
    a,b=result['work']
    assert all(a[key]==b[key] for key in ('stop','work','slot_activity','fct_sha256'))


def main():
    out=Path(sys.argv[1]).resolve()
    if len(sys.argv)>2:
        return measure(out)
    out.mkdir()
    prepare(out)
    return subprocess.run(['flock',crosspod.LOCK,sys.executable,crosspod.OBSERVER,out/'resources',
                           sys.executable,__file__,out,'measure']).returncode


if __name__=='__main__':
    raise SystemExit(main())
