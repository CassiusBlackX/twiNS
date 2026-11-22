"""Compare the original Unison partitioner on the two frozen heterogeneous graphs."""
import csv
import hashlib
import json
import os
from pathlib import Path
import re
from statistics import median
import subprocess
import sys
import time

VIDEOS = Path.home() / 'Videos'
DATA = VIDEOS / 'twins-paper-followthrough-20261122/project/data-input'
from adapt_unison import build
LOCK = VIDEOS / 'twins-isolated-20261118/out/exclusive-campaign.lock'

def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()

def flows(path):
    with path.open() as stream:
        return [tuple(map(int, row[:6])) for row in list(csv.reader(stream))[1:]]

def run_case(output, seed, hosts, threads, trial, preflight, binary):
    directory = output / f'{seed}-{hosts}' / f't{threads}' / f'trial-{trial}'
    directory.mkdir(parents=True)
    inputs = DATA / f'tcp/{hosts}'
    topo = inputs / 'heterogeneous.graph'
    traffic = inputs / ('tcp' if seed == 101 else 'tcp-holdout') / 'flows.csv'
    reference = DATA / f'ns3-{seed}/heterogeneous/{hosts}/reference.fct'
    actual = directory / 'flows.fct'
    if threads < 16:
        cpus = '4' if threads <= 1 else f'4-{3+threads}' if threads <= 8 else '0-15'
    else:
        cpus = '0-15' if threads == 16 else '0-31'
    argv = ['taskset', '-c', cpus, str(binary), '--topo=file', '--hostq=red',
            '--tcp=ns3::TcpDctcp', '--flowEcmp=1', '--stop=0.1', '--ecnK=30',
            f'--topofile={topo}', f'--flows={traffic}', f'--out={actual}', f'--threads={threads}']
    env = os.environ.copy()
    if preflight:
        env.update(TRACE_ALLQ='10000000')
    start = time.perf_counter_ns()
    with (directory / 'process.log').open('wb') as log:
        try:
            rc = subprocess.run(argv, cwd=directory, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=900).returncode
        except subprocess.TimeoutExpired:
            rc = 124
        elapsed = time.perf_counter_ns() - start
    left, right = flows(actual) if actual.is_file() else [], flows(reference)
    log = (directory / 'process.log').read_text(errors='replace')
    loop = re.findall(r'event_loop_ns (\d+)', log)
    differences = [(i, a, b) for i, (a, b) in enumerate(zip(left, right)) if a != b]
    row = dict(seed=seed, hosts=hosts, threads=threads, trial=trial, command=argv, rc=rc,
               process_wall_ns=elapsed, event_loop_ns=int(loop[-1]) if loop else None,
               input_sha256={p.name:sha(p) for p in (topo, traffic, reference)},
               binary_sha256=sha(binary), rows=len(left), expected_rows=len(right),
               equal=left==right, flow_differences=len(differences), first_differences=differences[:5])
    (directory / 'result.json').write_text(json.dumps(row, indent=2)+'\n')
    print(seed, hosts, threads, trial, rc, row['equal'], elapsed/1e9, flush=True)
    return row

def main():
    output = Path(sys.argv[1]).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if len(sys.argv) == 2:
        # The 32-thread arm uses all cores. Keep formal measurements isolated from the GPU queue.
        return subprocess.run(['flock', str(LOCK), sys.executable, __file__, str(output/'measurements'), 'run']).returncode
    binary = build(output/'adapted-build')
    samples = [run_case(output/'preflight', 101, 128, t, 0, True, binary) for t in (0,1,2)]
    if not samples[0]['equal']:
        raise ValueError('sequential preflight did not reproduce frozen six-field results')
    rows = []
    for trial in range(3):
        for seed in (101, 102):
            order = (0,1,2,4,8,16,32)
            for threads in order if trial%2==0 else reversed(order):
                rows.append(run_case(output, seed, 1024, threads, trial, False, binary))
    stats=[]
    for seed in (101,102):
        for threads in (0,1,2,4,8,16,32):
            group=[r for r in rows if (r['seed'],r['threads'])==(seed,threads) and r['rc']==0]
            if not group:
                continue
            wall=[r['process_wall_ns'] for r in group]
            loop=[r['event_loop_ns'] for r in group if r['event_loop_ns'] is not None]
            stats.append(dict(seed=seed,threads=threads,completed_processes=len(group),exact=sum(r['equal'] for r in group),
                              process_median_ns=median(wall),process_range_ns=[min(wall),max(wall)],
                              loop_median_ns=median(loop) if loop else None,loop_range_ns=[min(loop),max(loop)] if loop else None))
    (output/'summary.json').write_text(json.dumps(dict(rows=rows,statistics=stats,preflight=samples),indent=2)+'\n')
    return 0

if __name__=='__main__':
    raise SystemExit(main())
