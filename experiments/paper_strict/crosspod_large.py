"""Validate equal offered load and record clean timing plus separate native work counters."""

from collections import Counter
import csv
import json
from pathlib import Path
import re
import shutil
from statistics import median
import subprocess
import sys

import crosspod
from crosspod_connections import baseline
from crosspod_large_inputs import CASES
from pfc_sustained import save


def binaries(build):
    original = baseline()
    return ({'twins': original / 'twins/cmake/twins',
             'gedes': build / 'sources/GeDES/build-gedes-clean/GeDES', 'days': crosspod.DAYS},
            {'twins': build / 'sources/twins/build-twins-diagnostic/twins',
             'gedes': build / 'sources/GeDES/build-gedes-diagnostic/GeDES'})


def receipts(log):
    markers = {'work': ('strict_work ',), 'stop': ('strict_exit ', 'strict_stop ')}
    return {key: {k: int(v) for line in log.splitlines() if line.startswith(prefixes)
                  for k, v in re.findall(r'\b(\w+)=(\d+)', line)} for key, prefixes in markers.items()}


def hosted_completion(case, path):
    with (case / 'twins.csv').open() as stream:
        expected = Counter((int(r['start_ns']), int(r['flow_size'])) for r in csv.DictReader(stream))
    with path.open() as stream:
        rows = list(csv.DictReader(stream))
    if not rows:
        return {'offered_rows_equal': False, 'completed_payload_bytes': 0}
    keys = rows[0]
    if 'completed_ns' in keys:
        start, size, end = 'start_ns', 'flow_size', 'completed_ns'
    else:
        start, size, end = 'start_timestamp', 'flow_size', 'completed_timestamp'
    offered = Counter((int(r[start]), int(r[size])) for r in rows)
    return {'offered_rows_equal': offered == expected,
            'completed_payload_bytes': sum(int(r[size]) for r in rows if int(r[end]) >= 0)}


def runner(out, case, spec, engines, name, trial, pair, independent):
    env = {'GEDES_PAIR_ROUTES': '1'} if pair else {}
    if pair and independent:
        env['GEDES_INDEPENDENT_CONNECTIONS'] = '1'
    row = crosspod.run(out, engines, case, spec, name, trial, environment=env, timeout=100)
    work = out / f'trial-{trial}' / name
    log = (work / 'process.log').read_text()
    row.update(receipts(log))
    if name == 'days':
        match = re.search(r'result_fnv1a64=([0-9a-f]+)', log)
        row['state_fnv1a64'] = match[1] if match else None
    elif (work / 'fct.csv').exists():
        row.update(hosted_completion(case, work / 'fct.csv'))
        row['fct_sha256'] = crosspod.sha(work / 'fct.csv')
        row['accepted'] = row['accepted'] and row['offered_rows_equal'] and row['completed_payload_bytes'] == spec['payload_bytes']
    save(work / 'receipt.json', row)
    return row


def diagnostics_original(out, engines):
    clean = crosspod.ROOT / 'crosspod_connections/20261123T135630Z/results'
    groups = [
        ('original-single', clean / 'inputs', 'k*-one-use', clean / 'measurements'),
        ('original-paced', crosspod.ROOT / 'paced_shared/20261122T211357Z/results/measurements', 'k*-paced', None),
        ('original-burst', crosspod.ROOT / 'crosspod/20261122T205340Z/results/measurements', 'k*-overlap', None),
    ]
    result = {}
    for group, base, pattern, baseline_path in groups:
        for case in sorted(base.glob(pattern)):
            label = f'{group}-{case.name}'
            target = out / label
            target.mkdir()
            crosspod.route_hashes(case, target)
            spec = json.loads((case / 'input.json').read_text())
            rows = []
            for engine in engines:
                row = runner(target, case, spec, engines, engine, 'diagnostic', False, False)
                reference = (baseline_path or base) / case.name / 'trial-0' / engine / 'fct.csv'
                row['native_fct_equal'] = reference.is_file() and row.get('fct_sha256') == crosspod.sha(reference)
                rows.append(row)
                save(target / f'{engine}-archive-match.json', row)
            result[label] = rows
            save(out / 'original-work.json', result)


def measure(root):
    out = root / 'results'
    out.mkdir()
    clean, diagnosis = binaries(root / 'build')
    inputs = root / 'inputs'
    # Run the historical diagnostics while the short clean matrix is prepared for subsequent blocks.
    diagnostics_original(out, diagnosis)
    times = {}
    for label in CASES:
        case = inputs / f'k16-{label}'
        spec = json.loads((case / 'input.json').read_text())
        target = out / case.name
        target.mkdir()
        for name in ('twins-hash.csv', 'gedes-hash.csv'):
            shutil.copy2(case / name, target / name)
        independent = label != 'reused'
        both = label != 'one-use-distinct-peers'
        orders = (('twins', 'gedes'), ('gedes', 'twins'), ('twins', 'gedes')) if both else (
            ('twins', 'gedes', 'days'), ('gedes', 'days', 'twins'), ('days', 'twins', 'gedes'))
        rows = [runner(target, case, spec, clean, engine, trial, True, independent)
                for trial, order in enumerate(orders) for engine in order]
        observed = [runner(target, case, spec, diagnosis, engine, 'diagnostic', True, independent) for engine in diagnosis]
        for row in observed:
            base = next(r for r in rows if r['engine'] == row['engine'])
            row['clean_fct_equal'] = bool(row.get('fct_sha256')) and row.get('fct_sha256') == base.get('fct_sha256')
        walls = {}
        for engine in orders[0]:
            valid = [r for r in rows if r['engine'] == engine and r['accepted']]
            walls[engine] = median(r['wall_ns'] / 1e9 for r in valid) if len(valid) == 3 else None
        times[label] = {'spec': spec, 'clean': rows, 'diagnostic': observed, 'wall_median_s': walls}
        save(out / 'summary.json', times)


def main():
    root = Path(sys.argv[1]).resolve()
    if len(sys.argv) > 2:
        return measure(root)
    return subprocess.run(['flock', crosspod.LOCK, sys.executable, crosspod.OBSERVER, root / 'resources',
                           sys.executable, __file__, root, 'gpu']).returncode


if __name__ == '__main__':
    raise SystemExit(main())
