"""Freeze the original four-round plan and a peer-rotated single-use variant."""

import csv
from collections import Counter
from pathlib import Path
import re
import shutil
import subprocess

from crosspod import ROOT, sha
from pfc_sustained import save


K = 16
HOSTS = 1024
CAP_NS = 100_000_000
CASES = ('reused', 'one-use-same-peers', 'one-use-distinct-peers')
ORIGIN = ROOT / 'paced_shared/20261122T211357Z/results/measurements/k16-paced'


def write_csv(path, fields, rows):
    with path.open('w', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(fields)
        writer.writerows(rows)


def days_host(node):
    return node // 8 + (node % 8) * 128


def directed_links():
    links = []
    for h in range(HOSTS):
        links.append((h, HOSTS + h % 128))
    for i in range(128):
        for j in range(8):
            links.append((HOSTS + i, HOSTS + 128 + (i // 8) * 8 + j))
            links.append((HOSTS + 128 + i, HOSTS + 256 + (i % 8) * 8 + j))
    return sorted(links + [(b, a) for a, b in links])


def decode_routes(case, routes_binary, records):
    days_routes = case / 'days-route-ids.csv'
    with days_routes.open('wb') as stream:
        subprocess.run([routes_binary, case / 'days.toml'], stdout=stream, check=True)
    with days_routes.open() as stream:
        decoded = list(csv.DictReader(stream))
    assert len(decoded) == len(records)
    links = directed_links()
    port = {}
    decoded_pairs = Counter()
    for row in decoded:
        a, b = (int(row[field]) for field in ('source', 'target'))
        source, target = (node % 128 * 8 + node // 128 for node in (a, b))
        decoded_pairs[source, target] += 1
        for path_type, src, dst, left, right in (('forward', a, b, source, target),
                                                  ('reverse', b, a, target, source)):
            path = [links[int(link)] for link in row[path_type].split()]
            assert len(path) == 6 and path[0][0] == src and path[-1][1] == dst
            assert all(x[1] == y[0] for x, y in zip(path, path[1:]))
            edge = (path[1][1] - HOSTS - 128) % 8
            core = path[2][1] - HOSTS - 256
            assert core // 8 == edge
            code = edge | ((core % 8) << 8)
            assert port.setdefault((left, right), code) == code
    assert decoded_pairs == Counter((a, b) for a, b, *_ in records)
    write_csv(case / 'gedes-hash.csv', ('src', 'dst', 'size', 'start', 'est', 'done', 'hash_data'),
              ((a, b, 0, 0, 0, 0, code) for (a, b), code in sorted(port.items())))
    write_csv(case / 'twins-hash.csv', ('src', 'dst', 'size', 'start', 'est', 'done', 'hash_data', 'hash_ack'),
              ((a, b, size, start, 0, 0, port[a, b], port[b, a]) for a, b, size, start, _ in records))
    return len(port)


def prepare(out):
    out.mkdir(parents=True)
    routes_binary = sorted((ROOT / 'crosspod_paced').glob('*/results/inputs/days-routes'))[-1]
    with (ORIGIN / 'twins.csv').open() as stream:
        rows = [tuple(int(row[x]) for x in ('src', 'dst', 'flow_size', 'start_ns', 'conn'))
                for row in csv.DictReader(stream)]
    source_toml = (ORIGIN / 'days.toml').read_text()
    assert len(rows) == 2048 and len(re.findall(r'^graph = ', source_toml, re.M)) == len(rows)
    for label in CASES:
        case = out / f'k16-{label}'
        case.mkdir()
        records = []
        for order, (src, dst, size, start, conn) in enumerate(rows):
            if label == 'one-use-distinct-peers':
                dst = dst - dst % 8 + (dst + order % 4) % 8
            records.append((src, dst, size, start, conn if label == 'reused' else order))
        distinct = len({row[:2] for row in records})
        assert distinct == (2048 if label == 'one-use-distinct-peers' else 512)
        write_csv(case / 'twins.csv', ('src', 'dst', 'flow_size', 'start_ns', 'conn'), records)
        if label == 'reused':
            shutil.copy2(ORIGIN / 'gedes.csv', case / 'gedes.csv')
        else:
            write_csv(case / 'gedes.csv', ('node', 'dst', 'flow_size', 'start_ns'), (row[:4] for row in records))
        target_rows = iter(records)

        def graph(match):
            src, target, *_ = next(target_rows)
            assert int(match[1]) == days_host(src)
            return f'graph = [[{days_host(src)}, {days_host(target)}]]'

        toml = re.sub(r'graph = \[\[(\d+), (\d+)\]\]', graph, source_toml)
        assert next(target_rows, None) is None
        if label != 'one-use-distinct-peers':
            assert toml == source_toml
        (case / 'days.toml').write_text(toml)
        port_pairs = decode_routes(case, routes_binary, records)
        save(case / 'input.json', dict(k=K, hosts=HOSTS, label=label, path_hops=6, segments_per_flow=128,
            cap_ns=CAP_NS, pacing_ns=20_000, flow_rows=len(records), payload_bytes=sum(row[2] for row in records),
            connections=len({row[4] for row in records}), distinct_peers=distinct, directional_routes=port_pairs,
            input_sha256={file.name: sha(file) for file in case.iterdir()}))
