"""Measure the common, single-use TCP connection workload on three CUDA engines."""

import json
from pathlib import Path
from statistics import median
import subprocess
import sys

import crosspod
from crosspod_inputs import freeze
from pfc_sustained import save


def baseline():
    return crosspod.ROOT / 'crosspod/20261122T205340Z/results/builds'


def prepare(out):
    out.mkdir(parents=True)
    route_readers = sorted((crosspod.ROOT / 'crosspod_paced').glob('*/results/inputs/days-routes'))
    if not route_readers:
        raise RuntimeError('Missing original frozen Days route decoder')
    freeze(out, route_readers[-1], 16, 'one-use', 1, 128, 20000, 100_000_000)


def measure(out, inputs):
    out.mkdir()
    build = baseline()
    engines = {'twins': build / 'twins/cmake/twins', 'gedes': build / 'GeDES/cmake/GeDES', 'days': crosspod.DAYS}
    orders = (('twins', 'gedes', 'days'), ('gedes', 'days', 'twins'), ('days', 'twins', 'gedes'))
    summaries = {}
    for case in sorted(inputs.glob('k*')):
        spec = json.loads((case / 'input.json').read_text())
        target = out / case.name
        target.mkdir()
        crosspod.route_hashes(case, target)
        rows = []
        for trial, order in enumerate(orders):
            for engine in order:
                rows.append(crosspod.run(target, engines, case, spec, engine, trial))
            by_engine = {engine: median(r['wall_ns'] / 1e9 for r in rows if r['engine'] == engine)
                         for engine in engines if all(r['accepted'] for r in rows if r['engine'] == engine)}
            summaries[case.name] = {'inputs': spec, 'trials': rows, 'wall_median_s': by_engine}
            save(out / 'summary.json', summaries)
    return int(any(not row['accepted'] for suite in summaries.values() for row in suite['trials']))


def main():
    out = Path(sys.argv[1]).resolve()
    if len(sys.argv) > 2:
        return measure(out, Path(sys.argv[2]))
    out.mkdir()
    inputs = out / 'inputs'
    prepare(inputs)
    return subprocess.run(['flock', crosspod.LOCK, sys.executable, crosspod.OBSERVER, out / 'resources',
                           sys.executable, __file__, out / 'measurements', inputs]).returncode


if __name__ == '__main__':
    raise SystemExit(main())
