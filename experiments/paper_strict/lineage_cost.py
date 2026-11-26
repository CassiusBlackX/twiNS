"""Measure CUDA lineage ordering on two frozen paper workloads."""

from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pynvml as nv

from ai_drift import DATA as AI_DATA
from crosspod import CANDIDATE, LOCK, OBSERVER, replace, sha
from pfc_sustained import normalize, save
from rdma_memory import DATA as RDMA_DATA, samples

CASES = ('rdma', 'allgather')
BASE = 'bda9422cff61b74ac8491f50662dbf3f6712729c'
BASE_FILES = {
    'CMakeLists.txt': 'a0c2c52e2332bba21f17e396af8327171837929d69532b5c2ba29164a2cd0f4b',
    'src/rdma/rmain.cu': 'f521f1e34f57e4d6ba7e78aa63d0abfba7562b1797d2639313dbd5660c5ad122',
    'src/rdma/rstep.cuh': '72baef767b55686c59bf3ad221ffb67d3dad1e0a610d66cb1c91bd9e6602583a',
    'src/rdma/rdma.h': '9a5bdb1a4ab5851e2899c56f55434c6f12d1947f12a292dc48dbeb9364010b4a',
    'src/astra/closed.h': '420e94be1ded309d33dc97d3c2ed6627993cc132d9f33b1d54182a77e0795d17',
}
ARMS = {
    'normal': '',
    'local': '-DTWINS_NO_CHAIN',
    'fixed': '-DTWINS_NO_CHAIN -DTWINS_LINEAGE_FIXED',
    'shadow': '-DTWINS_LINEAGE_SHADOW',
}


def instrumentation(tree):
    main = tree / 'src/rdma/rmain.cu'
    closed = tree / 'src/astra/closed.h'
    replace(main, '#include <type_traits>', '#include <type_traits>\n#include <unordered_map>')
    # cpush zeroes root IDs under TWINS_NO_CHAIN. Normal roots are 1-based.
    replace(main, '            Chain c = w.troot[r];\n            c.root += fbase[wi];',
                  '            Chain c = w.troot[r];\n            if (!c.root) continue;\n            c.root += fbase[wi];')
    replace(main, '            if (!x.dyn)\n            {\n                x.start_chain.root += fbase[wi];',
                  '            if (!x.dyn && x.start_chain.root)\n            {\n                x.start_chain.root += fbase[wi];')
    replace(main, 'template <typename T>\nstatic T *dev_alloc(size_t n)', '''static size_t lc_live, lc_peak, lc_allocs, lc_frees;
static size_t lc_chain, lc_qpchain, lc_frame, lc_tail;
static std::unordered_map<void *, size_t> lc_sizes;

static void lc_freed(void *p)
{
    lc_live -= lc_sizes.at(p);
    lc_sizes.erase(p);
    ++lc_frees;
}

template <typename T>
static T *dev_alloc(size_t n)''')
    replace(main, '    CK(cudaMalloc((void **)&p, std::max<size_t>(n, 1) * sizeof(T)));', '''    const size_t bytes = std::max<size_t>(n, 1) * sizeof(T);
    CK(cudaMalloc((void **)&p, bytes));
    lc_sizes.emplace(p, bytes);
    lc_live += bytes;
    lc_peak = std::max(lc_peak, lc_live);
    ++lc_allocs;
    if constexpr (std::is_same<T, Chain>::value) lc_chain += bytes;
    if constexpr (std::is_same<T, QPChains>::value) lc_qpchain += bytes;
    if constexpr (std::is_same<T, LFrame>::value) lc_frame += bytes;
    if constexpr (std::is_same<T, TailNode>::value) lc_tail += bytes;''')
    for pointer, expected in [('d_idx', 3), ('d_seg', 2)]:
        old = f'CK(hipFree({pointer}));'
        code = closed.read_text()
        assert code.count(old) == expected
        closed.write_text(code.replace(old, old + f' lc_freed({pointer});'))
    marker = '    printf("flows done %u of %d; simulated'
    replace(main, marker, '''    printf("lineage_alloc live=%zu peak=%zu allocs=%zu frees=%zu chain=%zu qpchain=%zu "
           "frame=%zu tail=%zu frame_width=%zu packet_width=%zu chain_width=%zu tail_width=%zu\\n",
           lc_live, lc_peak, lc_allocs, lc_frees, lc_chain, lc_qpchain,
           lc_frame, lc_tail, sizeof(LFrame), sizeof(Pkt), sizeof(Chain), sizeof(TailNode));
''' + marker)


def initialize(out):
    tree = out / 'source'
    if tree.is_dir():
        return tree
    tree.mkdir(parents=True)
    actual = {p: sha(CANDIDATE / p) for p in BASE_FILES}
    assert actual == BASE_FILES, actual
    shutil.copytree(CANDIDATE / 'src', tree / 'src')
    shutil.copy2(CANDIDATE / 'CMakeLists.txt', tree)
    (tree / 'out').mkdir()
    (tree / 'out/astra').symlink_to((CANDIDATE / 'out/astra').resolve(), target_is_directory=True)
    instrumentation(tree)
    return tree


def build(out, arm, phase):
    tree = initialize(out)
    if phase == 'diag' or arm in ('fixed', 'shadow'):
        from lineage_diagnostics import patch_diagnostics, patch_shadow
        patch_shadow(tree)
        if phase == 'diag':
            patch_diagnostics(tree)
    folder = out / 'binaries' / phase / arm
    folder.mkdir(parents=True, exist_ok=True)
    builddir = out / 'compiled'
    defs = ARMS[arm] + (' -DTWINS_LINEAGE_DIAG' if phase == 'diag' else '')
    commands = [
        ['cmake', '-S', tree, '-B', builddir, '-G', 'Ninja', '-DTWINS_GPU_BACKEND=CUDA',
         '-DCMAKE_CUDA_ARCHITECTURES=120', '-DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc',
         '-DCMAKE_CUDA_FLAGS=' + defs.strip()],
        ['cmake', '--build', builddir, '--target', 'twins-rdma', 'twins-ai', '-j4'],
    ]
    with (folder / 'build.log').open('wb') as log:
        cpus = '6,8,10,12' if sys.argv[2:3] != ['followup'] else '14,15,18,20'
        for command in commands:
            subprocess.run(['taskset', '-c', cpus, *command], stdout=log,
                           stderr=subprocess.STDOUT, check=True, timeout=900)
    paths = [folder / x for x in ('twins-rdma', 'twins-ai')]
    for binary in paths:
        shutil.copy2(builddir / binary.name, binary)
    save(folder / 'build-receipt.json', dict(commit=BASE, arm=arm, phase=phase, defines=defs,
         source_sha256={str(p.relative_to(tree)): sha(p) for p in
             (tree/'src/rdma/rmain.cu', tree/'src/rdma/rstep.cuh', tree/'src/rdma/rdma.h', tree/'src/astra/closed.h')},
         binary_sha256={p.name: sha(p) for p in paths}, commands=[list(map(str, x)) for x in commands]))
    print('built', arm, phase, flush=True)


def workload(case, binaries):
    args = ['--fp-bases=1000003,1000033']
    if case == 'rdma':
        cfg, topo, flows = [RDMA_DATA / name for name in ('hpcc.cfg', 'ft32.topo', 'k32_a2a30.csv')]
        inputs = (cfg, topo, flows)
        command = [binaries/'twins-rdma', cfg, f'--topo={topo}', f'--flows={flows}', '--out=output.fct', 'CC_MODE=1', *args]
        return command, inputs, {'output.fct': RDMA_DATA/'k32_a2a30_cc1.fct'}
    cfg = CANDIDATE/'validation/simai/SimAI.conf'
    topo = AI_DATA/'data/simai/hpn1024.topo'
    wl = AI_DATA/'out/simai/wl/paper/c_allgather_512m_1024.txt'
    ref = AI_DATA/'out/simai/paper/c_allgather_512m_hpn1024'
    command = [binaries/'twins-ai', cfg, f'--topo={topo}', f'--astra={wl}', '--out=messages.fct',
               '--dag-out=actions.dag', '--nvl-islands=0', *args]
    return command, (cfg, topo, wl), {'messages.fct': ref.with_suffix('.ref'), 'actions.dag': ref.with_suffix('.dag')}


def exact_rows(rows):
    return hashlib.sha256(('\n'.join(rows)+'\n').encode()).hexdigest()


def metrics(text):
    flow = re.findall(r'flows done (\d+) of (\d+); simulated ([\d.]+) s in ([\d.]+) s wall \((\d+) launches\)', text)
    loop = re.findall(r'closed loop: (\d+) flows, (\d+) timers, (\d+) callbacks, (\d+) syncs, (\d+) of them with ties \((\d+) completions\), (\d+) lead ties; GPU ([\d.]+) s, system layer ([\d.]+) s, sync ([\d.]+) s', text)
    alloc = [dict((k, int(v)) for k, v in re.findall(r'(\w+)=(\d+)', row))
             for row in re.findall(r'^lineage_alloc (.*)$', text, re.M)]
    events = [[int(x) for x in row] for row in re.findall(
        r'drops (\d+) marks (\d+) pauses (\d+) resumes (\d+) nacks (\d+) \| undecided ties (\d+), on lost tails (\d+) \| overflows: tx (\d+) queue (\d+) ackq (\d+) qps (\d+) due (\d+) \| acks for done QPs (\d+)', text)]
    return dict(attempts=[dict(done=int(d), expected=int(n), simulated_s=float(s), loop_s=float(w), launches=int(l))
                          for d,n,s,w,l in flow],
                closed=[dict(zip(('flows','timers','callbacks','syncs','sort_runs','sorted','timer_ties'),map(int,row[:7])),
                             gpu_s=float(row[7]), system_s=float(row[8]), sync_s=float(row[9])) for row in loop],
                allocation=alloc,
                counters=[dict(zip(('drops','marks','pauses','resumes','nacks','undecided','lost','tx_overflow',
                                     'queue_overflow','ack_overflow','qp_overflow','due_overflow','late_ack'),row))
                          for row in events],
                settings=[line for line in text.splitlines() if line.startswith(
                          ('persistent:', 'global windows:', 'closed loop horizon:', 'slot ', 'lost tails:',
                           'DAG message overflow:', 'frames overflowed a slot:', 'redoing',
                           'rerunning', 'nvl:', 'network:'))])


def run(out, case, arm, phase, repeat, gpu):
    work = out / 'runs' / phase / case / f'{repeat}-{arm}'
    work.mkdir(parents=True)
    command, inputs, refs = workload(case, out/'binaries'/phase/arm)
    cmd = ['taskset', '-c', '0', *map(str, command)]
    row = samples(cmd, work, gpu)
    log = (work/'process.log').read_text()
    row.update(metrics(log))
    row.update(case=case, arm=arm, phase=phase, repeat=repeat, command=cmd,
               inputs={str(p):sha(p) for p in inputs}, binary_sha256=sha(command[0]), outputs={})
    for name, ref in refs.items():
        received = work / name
        if not received.is_file():
            continue
        actual = normalize(received, name.endswith('.dag'))
        expected = normalize(ref, name.endswith('.dag'))
        extras = Counter(actual) - Counter(expected)
        misses = Counter(expected) - Counter(actual)
        row['outputs'][name] = dict(rows=len(actual), sha256=sha(received), normalized_sha256=exact_rows(actual),
                                   reference_sha256=sha(ref), exact=actual==expected,
                                   different_rows=dict(extra=extras.total(), missing=misses.total()))
        if phase == 'timed':
            received.unlink()
    if phase == 'diag':
        from lineage_diagnostics import receipt
        row.update(receipt(work, log))
    row['accepted'] = (row['rc']==0 and row['pid_samples']>=20 and bool(row['attempts'])
                       and all(x in row['outputs'] for x in refs)
                       and (arm!='normal' or all(x['exact'] for x in row['outputs'].values())))
    save(work/'receipt.json', row)
    print(case, phase, repeat, arm, row['accepted'], round(row['wall_ns']/1e9, 4),
          [(k, x['exact']) for k, x in row['outputs'].items()], flush=True)
    return row


def measure(out, phase, cases, arms):
    for key in ('TWINS_CL_ONE_SLOT','TWINS_STOP_SYNCS','CUDA_VISIBLE_DEVICES'):
        os.environ.pop(key,None)
    nv.nvmlInit()
    try:
        gpu = nv.nvmlDeviceGetHandleByIndex(0)
        rows = []
        for case in cases:
            for repeat in range(3 if phase == 'timed' else 1):
                for arm in arms if repeat % 2 == 0 else reversed(arms):
                    rows.append(run(out, case, arm, phase, repeat, gpu))
                    save(out / ('-'.join([phase,*arms]) + '-runs.json'), rows)
        assert all(r['accepted'] for r in rows)
    finally:
        nv.nvmlShutdown()


def execute(out, phase, cases, arms, build_arms=True):
    if build_arms:
        for arm in arms:
            build(out, arm, phase)
    tag = '-'.join((phase,*arms))
    subprocess.run(['flock', LOCK, sys.executable, OBSERVER, out/'resources'/tag, sys.executable,
                    __file__, out, 'measure', phase, ','.join(cases), ','.join(arms)], check=True)
    assert json.loads((out/'resources'/tag/'gpu-ownership.json').read_text())['eligible']
    return json.loads((out/(tag+'-runs.json')).read_text())


def main():
    out = Path(sys.argv[1]).resolve()
    op = sys.argv[2] if len(sys.argv)>2 else 'baseline'
    if op == 'measure':
        phase, cases, arms = sys.argv[3:]
        measure(out, phase, cases.split(','), arms.split(','))
        return
    out.mkdir(parents=True, exist_ok=True)
    if op == 'baseline':
        execute(out, 'timed', CASES, ('normal','local'))
    elif op == 'primary':
        from lineage_diagnostics import patch_shadow
        patch_shadow(initialize(out))
        execute(out, 'timed', CASES, ('normal','fixed'))
    elif op == 'primary-replay':
        prior = out.parent.parent / sys.argv[3] / 'results/binaries/timed'
        for arm in ('normal','fixed'):
            source = prior / arm
            target = out / 'binaries' / 'timed' / arm
            target.mkdir(parents=True)
            shutil.copy2(source/'build-receipt.json',target)
            for binary in ('twins-rdma','twins-ai'):
                (target/binary).symlink_to(source/binary)
        execute(out, 'timed', CASES, ('normal','fixed'), build_arms=False)
    elif op == 'followup':
        from lineage_diagnostics import compare
        diagnosed = execute(out, 'diag', CASES, ('normal','local'))
        differences = {c:compare(out, diagnosed, c, ('normal','local')) for c in CASES}
        save(out/'normal-local-comparison.json', differences)
        changed = tuple(c for c, result in differences.items() if not result['same_trajectory'])
        if changed:
            diagnosed = execute(out, 'diag', changed, ('fixed','shadow'))
            checked = {c:compare(out, diagnosed, c, ('fixed','shadow')) for c in changed}
            save(out/'fixed-shadow-comparison.json', checked)
            assert all(v['same_trajectory'] for v in checked.values()), checked
            for row in diagnosed:
                if row['arm']=='shadow':
                    diag = row['lineage_diag'][-1]
                    assert diag['quick_decided'] and diag['full_decided'] and diag['tail_nodes'], diag
            timed = execute(out, 'timed', changed, ('fixed','shadow'))
            for r in timed:
                expected = next(d for d in diagnosed if d['case']==r['case'] and d['arm']==r['arm'])
                assert all(x['normalized_sha256']==expected['outputs'][k]['normalized_sha256']
                           for k, x in r['outputs'].items()), (r['case'], r['arm'], r['repeat'])
    else:
        raise ValueError(op)


if __name__ == '__main__':
    main()
