"""Derive a versioned acceptance ledger without changing the frozen manifests."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from release_status import status

def sha(path):
    with path.open('rb') as f:
        return hashlib.file_digest(f,'sha256').hexdigest()

def main():
    output=Path(sys.argv[1])
    output.mkdir(parents=True,exist_ok=False)
    workspace=Path(__file__).resolve().parents[3]
    roots={
        'hip':workspace/'twins-paper-release-20261122/out/release-hip/results',
        'cuda':workspace/'twins-paper-review/out/review/release-cuda-20261122/results/20261121T205729Z/replay',
    }
    data={}
    false_positives=[]
    for backend,root in roots.items():
        rows=[]
        for suite in ('network','paper','whatif'):
            for p in sorted((root/suite).glob('*/result.json')):
                old=json.loads(p.read_text())
                parsed=status((p.parent/'run.log').read_text(errors='replace'))
                equal=old.get('equal',False) if suite=='network' else all(old.get(k,{}).get('equal',False) for k in ('fct','dag'))
                accepted=old.get('rc')==0 and equal and not parsed['flagged']
                row=dict(case=old['case'],suite=suite,rc=old.get('rc'),output_equal=equal,
                         old_accepted=old['accepted'],accepted=accepted,parsed_status=parsed,
                         original_receipt=str(p),receipt_sha256=sha(p),
                         log_sha256=sha(p.parent/'run.log'))
                rows.append(row)
                if accepted and not old['accepted']:
                    false_positives.append(row)
        summary={suite:dict(tested=sum(r['suite']==suite for r in rows),
                           accepted=sum(r['accepted'] for r in rows if r['suite']==suite))
                 for suite in ('network','paper','whatif')}
        data[backend]=dict(rows=rows,summary=summary,frozen_summary_sha256=sha(root/'finished.json'))
    if len(false_positives)!=8 or any(x['parsed_status']['final']['flagged'] for x in false_positives):
        raise ValueError('the eight recorded retry regressions did not classify as expected')
    candidate=workspace/'twins-paper-release-20261122'
    negative=output/'negative-final-flags'
    negative.mkdir()
    old=json.loads(Path(false_positives[0]['original_receipt']).read_text())
    command=[v for v in old['command'] if not v.startswith('--out=')]
    command+=['--fcap=256',f'--out={negative.resolve()}/output.fct']
    with (negative/'run.log').open('wb') as log:
        rc=subprocess.run(command,cwd=workspace/'twiNS',stdout=log,stderr=subprocess.STDOUT,timeout=180).returncode
    parsed=status((negative/'run.log').read_text())
    (negative/'result.json').write_text(json.dumps(dict(command=command,rc=rc,status=parsed),indent=2)+'\n')
    if not parsed['flagged']:
        raise ValueError('real final-pass positive flags must be rejected')
    result=dict(classifier_sha256=sha(Path(__file__).with_name('release_status.py')),backends=data,
                retry_regressions=8,negative_final_flags=parsed)
    (output/'derived-manifest.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({k:v['summary'] for k,v in data.items()},indent=2))

if __name__=='__main__':
    main()
