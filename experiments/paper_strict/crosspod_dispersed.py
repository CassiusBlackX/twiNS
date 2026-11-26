"""Measure a degree-balanced sparse all-to-all across all 16 pods."""

from collections import Counter
import csv
import json
from pathlib import Path
import random
import shutil
from statistics import median
import subprocess
import sys

import crosspod
from crosspod_inputs import CONFIG
from crosspod_large import binaries, runner
from crosspod_large_inputs import HOSTS, days_host, decode_routes, write_csv
from pfc_sustained import save


CAP_NS = 100_000_000
PACE_NS = 20_000
BYTES = 1460 * 128
SEED = 52026
ALIGNED = crosspod.ROOT / 'crosspod-aligned-20261123'
SINGLE = crosspod.ROOT / 'crosspod_connections/20261123T135630Z/results'
ORDER = (('twins', 'gedes', 'days'), ('gedes', 'days', 'twins'), ('days', 'twins', 'gedes'))


def prepare(case):
    case.mkdir()
    generator = random.Random(SEED)
    rows = []
    for repeat in range(2):
        sources = list(range(HOSTS))
        generator.shuffle(sources)
        for src in sources:
            pod, local = divmod(src, 64)
            peer_pod = (pod + 1 + (local + 7 * repeat) % 15) % 16
            peer_local = (local + 19 + 11 * repeat) % 64
            target = 64 * peer_pod + peer_local
            ident = len(rows)
            rows.append((src, target, BYTES, 1000 + ident * PACE_NS, ident))
    assert set(Counter(r[0] for r in rows).values()) == set(Counter(r[1] for r in rows).values()) == {2}
    assert len({r[:2] for r in rows}) == 2048
    pod_pairs = Counter((a // 64, b // 64) for a, b, *_ in rows)
    assert len(pod_pairs) == 240 and all(a != b for a, b in pod_pairs)
    write_csv(case / 'twins.csv', ('src', 'dst', 'flow_size', 'start_ns', 'conn'), rows)
    write_csv(case / 'gedes.csv', ('node', 'dst', 'flow_size', 'start_ns'), (row[:4] for row in rows))
    config = CONFIG.format(k=16, half=8).replace('duration = 0.01', 'duration = 0.1')
    with (case / 'days.toml').open('w') as stream:
        stream.write(config)
        for src, target, size, start, _ in rows:
            stream.write(f'''
[[flow]]
flow_type = "TCP"
graph = [[{days_host(src)}, {days_host(target)}]]
[flow.traffic]
initial_delay = {start / 1e9:.9f}
size = {size}
arr_dist = {{type = "Uniform", low = 1.0, high = 1.0}}
pkt_size_dist = {{type = "DiscreteUniform", low = 1460, high = 1460}}
[flow.traffic.tcp]
cc_algorithm = "TCPReno"
''')
    decoder = sorted((crosspod.ROOT / 'crosspod_paced').glob('*/results/inputs/days-routes'))[-1]
    paths = decode_routes(case, decoder, rows)
    assert paths == 4096
    save(case / 'input.json', dict(k=16, hosts=HOSTS, cap_ns=CAP_NS, pacing_ns=PACE_NS, segments_per_flow=128,
         flow_rows=len(rows), payload_bytes=len(rows)*BYTES, path_hops=6, directional_routes=paths,
         directed_pod_pairs=len(pod_pairs), min_flows_per_pod_pair=min(pod_pairs.values()),
         max_flows_per_pod_pair=max(pod_pairs.values()), traffic_seed=SEED,
         input_sha256={file.name: crosspod.sha(file) for file in case.iterdir()}))


def records(path):
    with path.open() as stream:
        return list(csv.DictReader(stream))


def gedes_ip_map():
    original = {int(r['start_ns']):r for r in records(SINGLE / 'inputs/k16-one-use/twins.csv')}
    mapping = {}
    for r in records(SINGLE / 'measurements/k16-one-use/trial-0/gedes/fct.csv'):
        wanted = original[int(r['start_timestamp'])]
        for observed, field in (('source_ip', 'src'), ('dst_ip', 'dst')):
            ip, node = int(r[observed]), int(wanted[field])
            assert mapping.setdefault(ip, node) == node
    assert len(mapping) == len(set(mapping.values())) == HOSTS
    return mapping


def check_endpoints(expected, path, engine, mapping):
    rows = records(path)
    if engine == 'twins':
        actual = Counter(tuple(int(row[f]) for f in ('src', 'dst', 'start_ns', 'flow_size')) for row in rows)
    else:
        actual = Counter((mapping[int(r['source_ip'])], mapping[int(r['dst_ip'])], int(r['start_timestamp']),
                          int(r['flow_size'])) for r in rows)
    return actual == expected


def measure(root):
    case = root / 'inputs'
    out = root / 'measurements'
    out.mkdir()
    spec = json.loads((case / 'input.json').read_text())
    for name in ('twins-hash.csv','gedes-hash.csv'):
        shutil.copy2(case / name, out / name)
    clean, diagnostic = binaries(ALIGNED / 'build')
    expected = Counter(tuple(int(row[f]) for f in ('src','dst','start_ns','flow_size'))
                       for row in records(case / 'twins.csv'))
    mapping = gedes_ip_map()
    rows = []
    for trial, order in enumerate(ORDER):
        for engine in order:
            row = runner(out, case, spec, clean, engine, trial, True, True)
            if engine != 'days':
                path = out / f'trial-{trial}' / engine / 'fct.csv'
                row['endpoints_equal'] = path.is_file() and check_endpoints(expected,path,engine,mapping)
                row['accepted'] = row['accepted'] and row['endpoints_equal']
            rows.append(row)
            save(out / 'trials.json', rows)
    observe = []
    for engine in diagnostic:
        row = runner(out, case, spec, diagnostic, engine, 'diagnostic', True, True)
        native = {r.get('fct_sha256') for r in rows if r['engine']==engine}
        row['fct_matches_clean'] = bool(row.get('fct_sha256')) and native == {row['fct_sha256']}
        observe.append(row)
    days_hashes = {row['state_fnv1a64'] for row in rows if row['engine']=='days'}
    ok = (all(row['accepted'] for row in rows) and all(row['fct_matches_clean'] for row in observe)
          and len(days_hashes)==1 and None not in days_hashes)
    walls = {e: median(r['wall_ns'] / 1e9 for r in rows if r['engine']==e) for e in clean}
    save(out / 'summary.json', {'accepted': ok, 'wall_median_s': walls, 'clean':rows, 'diagnostic':observe,
                              'input':spec, 'days_state_sha_lane': sorted(x for x in days_hashes if x)})
    return int(not ok)


def main():
    root = Path(sys.argv[1]).resolve()
    if len(sys.argv)>2:
        return measure(root)
    root.mkdir()
    prepare(root / 'inputs')
    return subprocess.run(['flock', crosspod.LOCK, sys.executable, crosspod.OBSERVER, root/'resources',
                           sys.executable,__file__,root,'measure']).returncode


if __name__ == '__main__':
    raise SystemExit(main())
