"""Run the frozen AllGather factor and constant-workload PFC sweeps."""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
from statistics import median
import subprocess
import sys
import time

import ai_drift
import pfc_sustained as pfc
from crosspod import CANDIDATE, LOCK, OBSERVER, ROOT, sha


POOLS = (64, 48, 32, 24, 16, 12, 8, 6)
PFC_CASES = [(f'pool-{pool:02d}', pool, 8) for pool in POOLS]
ALLGATHER = 'c_allgather_512m_hpn1024'
ARMS = {
    'checked-window': (('--gwin=32',), 119),
    'serial-window': (('--serial', '--gwin=32'), 119),
    'checked-no-window': (('--gwin=0',), 119),
    'serial-no-window': (('--serial', '--gwin=0'), 119),
    'checked-one-slot': (('--gwin=0',), 1),
}
DIAGNOSTIC = ROOT / 'pfc_sustained/20261123T085920Z/results/build-receipts/compiled/twins-ai'
TIMING = CANDIDATE / 'build/twins-ai'
FIRST_REFERENCE = ROOT / 'mechanism_followup/20261123T135810Z/results/pfc'


def allgather(out):
    out.mkdir()
    refs = ai_drift.DATA / 'out/simai/paper'
    inputs = [CANDIDATE / 'validation/simai/SimAI.conf', ai_drift.DATA / 'data/simai/hpn1024.topo',
              ai_drift.DATA / 'out/simai/wl/paper/c_allgather_512m_1024.txt', refs / (ALLGATHER + '.ref'),
              refs / (ALLGATHER + '.dag')]
    pfc.save(out / 'input-hashes.json', {str(path): sha(path) for path in inputs})
    rows = []
    arms = list(ARMS)
    orders = (arms, arms[2:] + arms[:2], list(reversed(arms)))
    for repeat, order in enumerate(orders):
        with LOCK.open('r') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            for name in order:
                rows.append(allgather_trial(out, name, repeat))
                pfc.save(out / 'trials.json', rows)
        time.sleep(0.25)
    callbacks = {int(row['metrics']['callbacks']) for row in rows if row['accepted']}
    counts = {name: [row['wall_ns'] / 1e9 for row in rows if row['arm'] == name and row['accepted']] for name in ARMS}
    result = {'accepted': len(rows) == 15 and all(row['accepted'] for row in rows) and len(callbacks) == 1,
              'callbacks': sorted(callbacks), 'wall_median_s': {a: median(v) if len(v) == 3 else None for a, v in counts.items()},
              'wall_samples_s': counts}
    pfc.save(out / 'summary.json', result)


def allgather_trial(out, name, repeat):
    switches, horizon = ARMS[name]
    env = dict(os.environ)
    env.pop('TWINS_STOP_SYNCS', None)
    env.pop('TWINS_CL_ONE_SLOT', None)
    if horizon == 1:
        env['TWINS_CL_ONE_SLOT'] = '1'
    work = out / name / f'trial-{repeat}'
    row = ai_drift.run_one(work, TIMING, ALLGATHER, 1024, switches, env)
    counters = tcp_like_counters(work / 'process.log')
    launch = row['launch_report']
    actual = next((int(re.search(r'horizon: (\d+)', x)[1]) for x in launch if 'horizon:' in x), None)
    window = any('global windows:' in x for x in launch)
    row.update(repeat=repeat, arm=name, horizon=actual, global_window=window, counters=counters)
    row['accepted'] = (row['accepted'] and bool(counters) and actual == horizon and window == ('--gwin=32' in switches))
    pfc.save(work / 'receipt.json', row)
    print('allgather', repeat, name, row['accepted'], row['wall_ns'] / 1e9, flush=True)
    return row


def tcp_like_counters(path):
    pattern = r'drops (\d+) marks (\d+) pauses (\d+) resumes (\d+) nacks (\d+)'
    rows = re.findall(pattern, path.read_text())
    if not rows:
        return None
    return dict(zip(('drops', 'marks', 'pauses', 'resumes', 'nacks'), map(int, rows[-1])))


def switch_144(path):
    rows = []
    for line in path.open():
        if line.startswith('pfc_switch 16 '):
            bits, ports, count, ticks = map(int, line.split()[2:])
            rows.append(dict(bits=bits, active_ports=ports, count=count, ticks=ticks))
    counts = Counter()
    for row in rows:
        bits, count = row['bits'], row['count']
        if row['active_ports']:
            counts.update(active=count, checked_eligible=count if bits & 1 else 0,
                          pause_or_headroom=count if bits & 2 else 0, other_rejection=count if bits & 4 else 0,
                          pause_and_other=count if bits & 6 == 6 else 0, incoming_pause=count if bits & 8 else 0)
    return {'counts': dict(counts), 'bins': rows}


def trial_pfc(root, pool, mode, phase, binary, reference):
    d = root / f'{mode}-{phase}'
    d.mkdir()
    inputs = root / 'input'
    cmd = ['taskset', '-c', '0', binary, inputs / 'config.log', f'--topo={inputs}/topo.log',
           f'--astra={inputs}/workload.log', '--out=messages.fct', '--dag-out=actions.dag',
           '--nvl-islands=0', '--gwin=0', '--fp-bases=1000003,1000033']
    if mode == 'serial':
        cmd.append('--serial')
    rc, elapsed = pfc.timed(cmd, d, 'process.log', 90)
    log = d / 'process.log'
    n, dag = d / 'messages.fct', d / 'actions.dag'
    progress = re.findall(r'flows done (\d+) of (\d+); simulated ([\d.]+) s', log.read_text())
    cap_marker = re.findall(r'strict_pfc_stop slot=(\d+) time_ns=(\d+)', log.read_text())
    matches = {'messages': bool(reference[0]) and n.is_file() and pfc.normalize(n) == reference[0],
               'actions': bool(reference[1]) and dag.is_file() and pfc.normalize(dag, True) == reference[1]}
    row = dict(pool=pool, mode=mode, phase=phase, rc=rc, wall_ns=elapsed, matched=matches,
               completed_message_records=sum(1 for _ in n.open()) if n.is_file() else None,
               flow_progress=progress[-1] if progress else None,
               reached_simulation_cap=bool(cap_marker),
               counters=tcp_like_counters(log), command=list(map(str, cmd)), binary_sha256=sha(binary),
               reference_rc=reference[2], reference_complete=len(reference[0]) == 1920 and len(reference[1]) == 6080,
               accepted=(rc == 0 and reference[2] == 0 and len(reference[0]) == 1920 and len(reference[1]) == 6080
                         and all(matches.values())))
    if phase == 'diagnostic':
        row['node_144'] = switch_144(log)
    pfc.save(d / 'receipt.json', row)
    print('pfc', pool, mode, phase, rc, row['accepted'], elapsed / 1e9, flush=True)
    return row


def pfc_sweep(out, timing_binary=TIMING, diagnostic_binary=DIAGNOSTIC):
    results = {str(pool): {'trials': []} for pool in POOLS}
    references = {}
    for label, pool, _ in PFC_CASES:
        ref = out / label / 'reference'
        ref_rc = json.loads((ref / 'receipt.json').read_text())['rc']
        values = tuple(pfc.normalize(ref / filename, dag) if (ref / filename).is_file() else []
                       for filename, dag in (('messages.fct', False), ('actions.dag', True)))
        references[pool] = (*values, ref_rc)
    for phase in (0, 1, 2, 'diagnostic'):
        # Reverse the pool order in the second round; reverse a pair's engine order on every other position.
        cases = list(reversed(PFC_CASES)) if phase == 1 else PFC_CASES
        for index, (label, pool, _) in enumerate(cases):
            order = ('serial', 'checked') if (index + (phase == 1)) % 2 else ('checked', 'serial')
            for mode in order:
                item = trial_pfc(out / label, pool, mode, phase,
                                 diagnostic_binary if phase == 'diagnostic' else timing_binary,
                                 references[pool])
                results[str(pool)]['trials'].append(item)
            result = results[str(pool)]
            walls = {mode: [r['wall_ns'] / 1e9 for r in result['trials']
                            if r['mode'] == mode and r['phase'] != 'diagnostic' and r['accepted']]
                     for mode in ('checked', 'serial')}
            result['wall_samples_s'] = walls
            result['wall_median_s'] = {mode: median(v) if len(v) == 3 else None for mode, v in walls.items()}
            pfc.save(out / 'summary.json', results)


def references(out):
    for label, *_ in PFC_CASES:
        ref = FIRST_REFERENCE / label / 'reference'
        if (ref / 'receipt.json').is_file():
            shutil.copytree(ref, out / 'pfc' / label / 'reference')
    pending = [pool for label, pool, _ in PFC_CASES if not (out / 'pfc' / label / 'reference').exists()]

    cores = (4, 8, 12)

    def run(index):
        for pool in pending[index::len(cores)]:
            subprocess.run([sys.executable, __file__, out, 'reference-one', str(pool), str(cores[index])], check=True)

    with ThreadPoolExecutor(max_workers=len(cores)) as pool:
        list(pool.map(run, range(len(cores))))
    assert all((out / 'pfc' / label / 'reference/receipt.json').is_file() for label, *_ in PFC_CASES)


def main():
    out = Path(sys.argv[1]).resolve()
    mode = sys.argv[2] if len(sys.argv) > 2 else None
    if mode == 'timed':
        allgather(out / 'allgather')
        with LOCK.open('r') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            pfc_sweep(out / 'pfc')
        return 0
    if mode == 'reference-one':
        pool, core = map(int, sys.argv[3:5])
        pfc.DESIGN = [row for row in PFC_CASES if row[1] == pool]
        return pfc.reference(out / 'pfc', core=core, trace_wire=False, timeout=100)
    out.mkdir()
    pfc.DESIGN = PFC_CASES
    pfc.freeze(out / 'pfc')
    with ThreadPoolExecutor(max_workers=1) as pool:
        reference_job = pool.submit(references, out)
        # Run the independent references on CPU-only cores while the GPU matrix starts.
        measure = subprocess.Popen([sys.executable, OBSERVER, out / 'resources', sys.executable, __file__, out, 'timed'])
        reference_job.result()
        return measure.wait()


if __name__ == '__main__':
    raise SystemExit(main())
