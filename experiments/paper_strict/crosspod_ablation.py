"""Factor CUDA Graph replay and idle-slot skipping independently on Fig. 6(b)."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
from time import monotonic_ns

import crosspod
from crosspod import replace, sha
from crosspod_connections import baseline
from crosspod_large import receipts
from pfc_sustained import save


ROOT = crosspod.ROOT
SINGLE = ROOT / 'crosspod_connections/20261123T135630Z/results'
ALIGNED = ROOT / 'crosspod-aligned-20261123'
FROZEN = {'512': (SINGLE / 'inputs/k16-one-use', SINGLE / 'measurements/k16-one-use'),
          '2048': (ALIGNED/'inputs/k16-one-use-distinct-peers', ALIGNED/'adapter-v2/results/k16-one-use-distinct-peers')}
ARMS = {'graph-skip': (True, True), 'direct-skip': (False, True),
        'graph-step': (True, False), 'direct-step': (False, False)}
ORDERS = (('graph-skip','direct-skip','direct-step','graph-step'),
          ('graph-step','direct-step','direct-skip','graph-skip'),
          ('direct-step','graph-skip','graph-step','direct-skip'))


def patch_tree(tree, meter):
    source = tree/'src'
    model, kernel, main = (source/x for x in ('model.h','kernel.cuh','main.cu'))
    replace(model, '    int64_t *slot;', '    bool ablation_skip;\n    int64_t *slot;')
    replace(kernel, 'if (flight_empty && cur == 0 && prev == 0 && nw != ~0ull && (int64_t)nw > next)',
            'if (s.ablation_skip && flight_empty && cur == 0 && prev == 0 && nw != ~0ull && (int64_t)nw > next)')
    replace(main, 'int main(int argc, char **argv)\n{', '''int main(int argc, char **argv)
{
    const auto ablation_entry = std::chrono::steady_clock::now();
    const bool ablation_graph = getenv("TWINS_ABLATION_DIRECT") == nullptr;
    const bool ablation_profile = getenv("TWINS_ABLATION_PROFILE") != nullptr;''')
    replace(main, '#include <numeric>', '#include <numeric>\n#include <cuda_profiler_api.h>')
    replace(main, '    s.slot = dalloc<int64_t>(1);', '''    s.slot = dalloc<int64_t>(1);
    s.ablation_skip = getenv("TWINS_ABLATION_STEP") == nullptr;''')
    begin = '    cudaGraph_t graph;\n    cudaGraphExec_t exec;'
    end = '    int64_t slots;\n'
    text = main.read_text()
    a = text.index(begin)
    b = text.index(end, a)
    main.write_text(text[:a] + '''    auto enqueue_slot = [&]() {
        if (basic)
            kbasic::ns_slot<<<grid, BLOCK, 0, stream>>>(reinterpret_cast<kbasic::Due *>(due), sw_ts, sw_ref, sw_up);
        else
            kfull::ns_slot<<<grid, BLOCK, 0, stream>>>(due, sw_ts, sw_ref, sw_up);
    };
    cudaGraphExec_t exec{};
    long long graph_setup_ns = 0;
    if (ablation_graph)
    {
        const auto setup_start = std::chrono::steady_clock::now();
        cudaGraph_t graph;
        CHECK(cudaStreamBeginCapture(stream, cudaStreamCaptureModeGlobal));
        for (int i = 0; i < graph_k; ++i)
            enqueue_slot();
        CHECK(cudaStreamEndCapture(stream, &graph));
        CHECK(cudaGraphInstantiate(&exec, graph, 0));
        graph_setup_ns = std::chrono::duration_cast<std::chrono::nanoseconds>(
                         std::chrono::steady_clock::now() - setup_start).count();
    }
    CHECK(cudaDeviceSynchronize());
    const auto startup_ns = std::chrono::duration_cast<std::chrono::nanoseconds>(
                            std::chrono::steady_clock::now() - ablation_entry).count();
    if (ablation_profile) CHECK(cudaProfilerStart());
    uint64_t groups = 0;
    const auto t0 = std::chrono::steady_clock::now();
    while (!*(volatile int *)h_finished)
    {
        if (ablation_graph)
            CHECK(cudaGraphLaunch(exec, stream));
        else
        {
            for (int i = 0; i < graph_k; ++i)
                enqueue_slot();
            CHECK(cudaGetLastError());
        }
        CHECK(cudaStreamSynchronize(stream));
        ++groups;
    }
    const auto loop_ns = std::chrono::duration_cast<std::chrono::nanoseconds>(
                         std::chrono::steady_clock::now() - t0).count();
    if (ablation_profile) CHECK(cudaProfilerStop());
    const double sec = loop_ns * 1e-9;
    printf("strict_ablation graph=%u idle_skip=%u width=%d startup_ns=%lld graph_setup_ns=%lld loop_ns=%lld "
           "sync_groups=%llu graph_launches=%llu direct_launches=%llu submitted_slots=%llu\\n",
           unsigned(ablation_graph), unsigned(s.ablation_skip), graph_k, (long long)startup_ns,
           graph_setup_ns, (long long)loop_ns, (unsigned long long)groups,
           ablation_graph ? (unsigned long long)groups : 0ull,
           ablation_graph ? 0ull : (unsigned long long)(groups * graph_k),
           (unsigned long long)(groups * graph_k));
''' + text[b:])
    if meter:
        replace(model, '    bool ablation_skip;', '''    unsigned long long *ablation_slot_counts;
    bool ablation_skip;''')
        replace(main, '    s.ablation_skip = getenv("TWINS_ABLATION_STEP") == nullptr;', '''    s.ablation_skip = getenv("TWINS_ABLATION_STEP") == nullptr;
    s.ablation_slot_counts = dalloc<unsigned long long>(3);''')
        replace(kernel, '            s.activity[1] = cur;', '''            ++s.ablation_slot_counts[0];
            s.ablation_slot_counts[1] += cur != 0;
            if (!*s.finished)
                s.ablation_slot_counts[2] += next - (t + 1);
            s.activity[1] = cur;''')
        replace(main, '    report_exit_snapshot(s, out_rec);', '''    report_exit_snapshot(s, out_rec);
    const auto ac = ddown(s.ablation_slot_counts, 3);
    printf("strict_slot_activity processed=%llu active=%llu skipped=%llu\\n", ac[0], ac[1], ac[2]);''')


def build(root, name, origin, meter, core):
    d = root/name
    d.mkdir(parents=True)
    tree = d/'source'
    shutil.copytree(origin, tree, ignore=shutil.ignore_patterns('cmake','build-*','build','out'))
    patch_tree(tree, meter)
    target = d/'compiled'
    commands = [['taskset','-c',core,'cmake','-S',tree,'-B',target,'-G','Ninja','-DTWINS_GPU_BACKEND=CUDA',
                 '-DCMAKE_CUDA_ARCHITECTURES=120','-DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc'],
                ['taskset','-c',core,'cmake','--build',target,'--target','twins','-j2']]
    with (d/'build.log').open('wb') as log:
        for cmd in commands:
            subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=750)
    binary = target/'twins'
    save(d/'build-receipt.json', {'source':str(origin), 'binary_sha256':sha(binary), 'metered':meter,
         'commands':[list(map(str,c)) for c in commands],
         'source_sha256':{x:sha(tree/'src'/x) for x in ('main.cu','kernel.cuh','model.h','step.cuh')}})
    return binary


def prepare(out):
    inputs = out/'inputs'
    inputs.mkdir()
    for key, (source, archived) in FROZEN.items():
        d = inputs/key
        d.mkdir()
        shutil.copy2(source/'twins.csv', d/'twins.csv')
        shutil.copy2(archived/'twins-hash.csv', d/'twins-hash.csv')
        spec = json.loads((source/'input.json').read_text())
        save(d/'input.json', {'case':key, 'source':str(source), 'archive':str(archived),
             'flow_rows':spec['flow_rows'], 'payload_bytes':spec['payload_bytes'], 'cap_ns':spec['cap_ns'],
             'workload_sha256':sha(d/'twins.csv'), 'ecmp_sha256':sha(d/'twins-hash.csv'),
             'frozen_output_sha256':sha(archived/'trial-0/twins/fct.csv')})


def arm_options(arm, profile=False):
    graph, skip = ARMS[arm]
    env = dict(os.environ)
    for var, turn_on in (('TWINS_ABLATION_DIRECT',not graph),('TWINS_ABLATION_STEP',not skip),
                         ('TWINS_ABLATION_PROFILE',profile)):
        env.pop(var, None)
        if turn_on:
            env[var] = '1'
    return env


def command(binary, source, spec, output, width=64):
    return ['taskset','-c','0',str(binary),'--k=16','--tcp=newreno','--queue=200','--ecnK=200',
            f'--max_slots={spec["cap_ns"]//1000}',f'--flows={source}/twins.csv',
            f'--ecmp_from={source}/twins-hash.csv',f'--out={output}',f'--graph_k={width}']


def section(log, marker):
    row = next(x for x in log.splitlines() if x.startswith(marker+' '))
    return {k:int(v) for k,v in re.findall(r'\b(\w+)=(\d+)',row)}


def trial(out, source, spec, binary, arm, meter, width=64):
    out.mkdir(parents=True)
    cmd = command(binary, source, spec, 'output.fct', width=width)
    started = monotonic_ns()
    with (out/'process.log').open('wb') as stream:
        rc = crosspod.invoke_bounded(cmd,out,arm_options(arm),stream,100)
    elapsed = monotonic_ns()-started
    text = (out/'process.log').read_text()
    metrics = section(text, 'strict_ablation')
    graph, skip = ARMS[arm]
    assert metrics['graph']==graph and metrics['idle_skip']==skip and metrics['width']==width
    fct = out/'output.fct'
    actual = sha(fct) if fct.is_file() else None
    row = dict(case=spec['case'], arm=arm, command=cmd, rc=rc, wall_ns=elapsed, metered=meter,
               accepted=rc==0 and bool(actual) and spec['frozen_output_sha256'] in (None,actual),
               fct_sha256=actual, metrics=metrics)
    if meter:
        row.update(receipts(text), slot_activity=section(text,'strict_slot_activity'))
        slots, native = row['slot_activity'], row['stop']
        assert slots['processed']+slots['skipped']==native['last_slot']+1, row
        assert slots['active']<=slots['processed']<=metrics['submitted_slots']
    save(out/'receipt.json',row)
    print('ablation', spec['case'], arm, out.name, row['accepted'], round(elapsed/1e9,4), flush=True)
    return row


def profile(dest, scratch, source, spec, binary, arm):
    dest.mkdir(parents=True)
    scratch.mkdir(parents=True)
    prefix = scratch/'capture'
    cmd = command(binary, source, spec, 'output.fct')
    prof = ['nsys','profile','--trace=cuda','--sample=none','--cpuctxsw=none','--capture-range=cudaProfilerApi',
            '--capture-range-end=stop','--cuda-graph-trace=node','--output',str(prefix),*cmd]
    with (dest/'profile.log').open('wb') as stream:
        subprocess.run(prof,cwd=dest,env=arm_options(arm,True),stdout=stream,stderr=subprocess.STDOUT,check=True,timeout=180)
        subprocess.run(['nsys','export','--type=sqlite','--output',str(prefix)+'.sqlite',str(prefix)+'.nsys-rep'],
                       stdout=stream,stderr=subprocess.STDOUT,check=True,timeout=180)
    log = (dest/'profile.log').read_text()
    info = section(log,'strict_ablation')
    graph = ARMS[arm][0]
    with sqlite3.connect(str(prefix)+'.sqlite') as db:
        count, total, first, last, graph_nodes = db.execute('''SELECT count(*), sum(end-start), min(start),
                max(end), sum(case when graphNodeId>0 then 1 else 0 end)
                FROM CUPTI_ACTIVITY_KIND_KERNEL''').fetchone()
        symbol = db.execute('''SELECT coalesce(s.value,'?'), count(*) FROM CUPTI_ACTIVITY_KIND_KERNEL k
               LEFT JOIN StringIds s ON k.shortName=s.id GROUP BY s.value''').fetchall()
    actual = sha(dest/'output.fct')
    ok = (actual==spec['frozen_output_sha256'] and count==info['submitted_slots'] and
          graph_nodes==(count if graph else 0) and len(symbol)==1 and 'ns_slot' in symbol[0][0])
    data = {'case':spec['case'],'arm':arm,'command':prof,'fct_sha256':actual,'accepted':ok,
            'kernel_count':count,'kernel_sum_ns':total,'kernel_span_ns':last-first,'graph_nodes':graph_nodes,
            'kernel_symbols':symbol,'profile_sha256':sha(Path(str(prefix)+'.nsys-rep')),'metrics':info}
    save(dest/'receipt.json',data)
    print('profile', spec['case'],arm,ok,count,total,flush=True)
    return data


def measure(out):
    clean = out/'build/clean/compiled/twins'
    meter = out/'build/metered/compiled/twins'
    main = out/'measurements'
    main.mkdir()
    cases = {}
    for key in FROZEN:
        source=out/'inputs'/key
        spec=json.loads((source/'input.json').read_text())
        cases[key] = {'spec':spec,'trials':[],'instrumented':[],'profiles':[]}
        case = cases[key]
        for repeat, arms in enumerate(ORDERS):
            for arm in arms:
                case['trials'].append(trial(main/key/arm/f'trial-{repeat}',source,spec,clean,arm,False))
                save(main/'summary.json',cases)
        for arm in ARMS:
            case['instrumented'].append(trial(main/key/arm/'slot-work',source,spec,meter,arm,True))
            save(main/'summary.json',cases)
        for arm in ARMS:
            case['profiles'].append(profile(main/key/arm/'device-profile',out.parent/'profiles-raw'/key/arm,
                                            source,spec,clean,arm))
            save(main/'summary.json',cases)
        checked = case['instrumented']
        assert len({json.dumps(r['work'],sort_keys=True) for r in checked})==1
        assert len({r['slot_activity']['active'] for r in checked})==1
        assert len({r['stop']['last_slot'] for r in checked})==1
        case['accepted'] = all(r['accepted'] for field in ('trials','instrumented','profiles') for r in case[field])
        save(main/'summary.json',cases)
    return int(not all(case['accepted'] for case in cases.values()))


def main():
    out=Path(sys.argv[1]).resolve()
    if len(sys.argv)>2:
        return measure(out)
    out.mkdir()
    prepare(out)
    with ThreadPoolExecutor(max_workers=2) as jobs:
        base = jobs.submit(build,out/'build','clean',baseline()/'twins',False,'4,6')
        diag = jobs.submit(build,out/'build','metered',ALIGNED/'build/sources/twins',True,'8,10')
        base.result()
        diag.result()
    return subprocess.run(['flock',crosspod.LOCK,sys.executable,crosspod.OBSERVER,out/'resources',
                           sys.executable,__file__,out,'measure']).returncode


if __name__=='__main__':
    raise SystemExit(main())
