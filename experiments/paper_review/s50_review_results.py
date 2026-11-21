"""Summarize returned CUDA trials against the local independent references."""
import argparse
import json
from pathlib import Path
from statistics import median
import sys
from causal_dag import dag

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'paper_scale'))
from ai_compare import compare


def read(path):
    return json.loads(path.read_text()) if path.is_file() else None


def analyze(run, prior, review):
    results = {
        'source_revision': (run/'revision').read_text().strip(),
        'gates': read(run/'gates.json'),
        'tcp_correctness': read(run/'tcp-validation'/'summary.json'),
        'tcp_timing': read(run/'tcp-timing'/'summary.json'),
        'gpu_interference': read(run/'gpu-ownership.json'),
        'irn': {},
        'ai': {},
    }
    for mode in ('serial','parallel'):
        results['irn'][mode] = {
            name.name: read(name/'result.json') or read(name/'failure.json')
            for name in sorted((run/f'irn-{mode}').glob('*'))
        }
    for kind in ('small','large','128'):
        reference = (review/'a87-final/ai128-final' if kind == '128'
                     else prior/'a87/ai-wire'/f'{kind}-buffer')
        group = run/('ai128' if kind == '128' else f'ai-{kind}')
        rows = []
        for folder in sorted(group.glob('trial-*')):
            try:
                receipt = read(folder/('gpu-run.json' if kind == '128' else 'receipt.json'))
                if kind != '128':
                    (folder/'gpu.rc').write_text(f"{receipt['rc']}\n")
                row = compare(folder,reference)
                row['same_causal_dag'] = dag(folder/'gpu.dag') == dag(reference/'reference.dag')
                row['accepted'] = row['accepted'] and row['same_causal_dag']
                row['wall_s'] = receipt['wall_s']
            except (OSError,ValueError,TypeError,KeyError) as error:
                row = {'accepted': False,'error': str(error)}
            rows.append(row)
        accepted = len(rows) == 4 and all(row['accepted'] for row in rows)
        result = {'accepted': accepted, 'trials': rows}
        if accepted:
            times = [r['wall_s'] for r in rows[1:]]
            result.update(median_s=median(times),range_s=[min(times),max(times)])
        results['ai'][kind] = result
    return results


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    for name in ('run','prior','review'):
        parser.add_argument(name,type=Path)
    args=parser.parse_args()
    summary=analyze(args.run.resolve(),args.prior.resolve(),args.review.resolve())
    (args.run/'acceptance.json').write_text(json.dumps(summary,indent=2)+'\n')
