"""Verify the optimized heterogeneous TCP kernel against frozen ns-3 records."""
import argparse
import json
from pathlib import Path
from statistics import median
import subprocess
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'paper_scale'))
from scale import run, sha, verify


def memory_samples(stop, samples, backend):
    local_vram = next(iter(sorted(Path('/sys/class/drm').glob('card*/device/mem_info_vram_used'))), None)
    while not stop.is_set():
        if backend == 'hip' or backend is None and local_vram is not None:
            samples.append(int(local_vram.read_text()))
        else:
            used = subprocess.check_output(
                ['nvidia-smi', '--query-gpu=memory.used', '--format=csv,noheader,nounits'],
                text=True, timeout=3,
            )
            samples.append(int(used.splitlines()[0]) * 2**20)
        stop.wait(0.02)


def main():
    parser = argparse.ArgumentParser()
    for name in ('inputs', 'reference', 'holdout_reference', 'build', 'output'):
        parser.add_argument(name, type=lambda value: Path(value).resolve())
    parser.add_argument('--process-timing', action='store_true')
    parser.add_argument('--all-scales', action='store_true',
                        help='time builds at every frozen scale as well as the largest case')
    parser.add_argument('--memory-backend', choices=('cuda', 'hip'),
                        help='select the measured GPU when an integrated GPU is also present')
    parser.add_argument('--core', type=int)
    parser.add_argument('--cpu-reference-bin', type=lambda value: Path(value).resolve())
    args = parser.parse_args()
    if args.all_scales and not args.process_timing:
        parser.error('--all-scales requires --process-timing')
    args.output.mkdir(parents=True, exist_ok=False)
    sources = (
        (101, 'tcp', args.reference),
        (102, 'tcp-holdout', args.holdout_reference),
    )
    settings = [128, 256, 512, 1024]
    entry = args.build / ('twins' if args.process_timing else 'twins512-links')
    binaries = [entry]
    if args.process_timing:
        binaries = sorted(path for path in args.build.glob('twins*') if path.is_file())
    binary_hashes = {path.name: sha(path) for path in binaries}
    cases = []
    cpu_cases = []
    for seed, traffic_name, reference_root in sources:
        for hosts in ([1024] if args.process_timing and not args.all_scales else settings):
            inputs = args.inputs / str(hosts)
            flow_path = inputs / traffic_name / 'flows.csv'
            for fabric in ('uniform', 'heterogeneous'):
                topo = inputs / (fabric + '.graph')
                reference = reference_root / fabric / str(hosts) / 'reference.fct'
                common = [
                    str(entry), '--topo=graph', '--tcp=dctcp', '--ecnK=30', '--spray=0',
                    f'--topofile={topo}', f'--flows={flow_path}', '--out=gpu.fct',
                ]
                if args.process_timing and args.cpu_reference_bin:
                    times = []
                    for repeat in range(4):
                        dest = args.output / f'{seed}-{hosts}-{fabric}-reference' / f'trial-{repeat}'
                        dest.mkdir(parents=True)
                        measured = run([
                            args.cpu_reference_bin, '--topo=file', '--hostq=red', '--tcp=ns3::TcpDctcp',
                            '--flowEcmp=1', '--stop=0.1', '--ecnK=30',
                            f'--topofile={topo}', f'--flows={flow_path}', '--out=gpu.fct',
                        ], dest, 'cpu', {}, 600, args.core)
                        (dest / 'reference.fct').symlink_to(reference)
                        # The CPU binary uses the same ordered six result fields.
                        if measured['rc']:
                            raise RuntimeError(f'CPU reference exit {measured["rc"]}')
                        from reference_hashes import signature
                        if signature(dest / 'gpu.fct') != signature(reference):
                            raise ValueError(f'CPU reference output changed for {seed} {hosts} {fabric}')
                        times.append(measured['wall_ns'])
                    cpu_cases.append(dict(seed=seed, hosts=hosts, fabric=fabric, trials_ns=times,
                                          median_ns=median(times[1:]), range_ns=[min(times[1:]), max(times[1:])],
                                          binary_sha256=sha(args.cpu_reference_bin), reference_sha256=sha(reference)))
                policies = (
                    [('default', []), ('15-run', ['--start-history=256']),
                     ('31-run', ['--start-history=512'])]
                    if args.process_timing else [('31-run-single-pass', ['--single-pass'])]
                )
                for policy, options in policies:
                    trials = []
                    for repeat in range(4 if args.process_timing else 1):
                        dest = args.output / f'{seed}-{hosts}-{fabric}-{policy}' / f'trial-{repeat}'
                        dest.mkdir(parents=True)
                        (dest / 'reference.fct').symlink_to(reference)
                        stop = threading.Event()
                        usage = []
                        # The warm-up collects peak VRAM apart from formal timing trials.
                        monitor = threading.Thread(target=memory_samples, args=(stop, usage, args.memory_backend)) if args.process_timing and not repeat else None
                        if monitor:
                            monitor.start()
                        try:
                            measured = run(
                                common + options, dest, 'gpu',
                                {'HSA_DISABLE_COREDUMP_ON_EXCEPTION': '1'}, 180, args.core,
                            )
                        finally:
                            if monitor:
                                stop.set()
                                monitor.join()
                        if monitor:
                            measured['sampled_device_memory_bytes'] = usage
                            measured['sampled_peak_device_memory_bytes'] = max(usage, default=None)
                        measured['accepted'] = False
                        try:
                            if measured['rc'] != 0:
                                raise ValueError(f'GPU exit {measured["rc"]}')
                            measured['flows'] = verify(dest, flow_path, tcp=True)
                            measured['output_sha256'] = sha(dest / 'gpu.fct')
                            measured['accepted'] = True
                        except (OSError, ValueError, IndexError) as error:
                            measured['error'] = str(error)
                        (dest / 'comparison.json').write_text(json.dumps(measured, indent=2) + '\n')
                        trials.append(measured)
                    walls = [v['wall_ns'] for v in trials[1:]]
                    accepted = all(v['accepted'] for v in trials)
                    case = dict(seed=seed, hosts=hosts, fabric=fabric, policy=policy, accepted=accepted,
                                input_sha256=sha(flow_path), graph_sha256=sha(topo),
                                reference_sha256=sha(reference), trials=trials)
                    if accepted and walls:
                        case.update(median_ns=median(walls), range_ns=[min(walls), max(walls)])
                    cases.append(case)
                    print(json.dumps({k: case[k] for k in ('seed', 'hosts', 'fabric', 'policy', 'accepted')}), flush=True)
    (args.output / 'summary.json').write_text(json.dumps(dict(
        binary_sha256=binary_hashes, process_timing=args.process_timing,
        scope='Frozen independent ns-3 outputs, six ordered fields; timing mode discards trial 0 as warm-up.',
        cases=cases, cpu_cases=cpu_cases,
        accepted=sum(case['accepted'] for case in cases)), indent=2) + '\n')
    raise SystemExit(0 if all(case['accepted'] for case in cases) else 1)


if __name__ == '__main__':
    main()
