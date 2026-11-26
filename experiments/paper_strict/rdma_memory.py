"""Measure original 8,192-host RDMA runs by PID and audit explicit allocations."""

from concurrent.futures import ThreadPoolExecutor
import fcntl
import json
from pathlib import Path
import re
import shutil
from statistics import median
import subprocess
import sys
import time

import pynvml as nv

from crosspod import CANDIDATE, LOCK, VIDEOS, replace, sha
from pfc_sustained import save


CASES = (('a2a30', 1), ('a2a30', 3), ('a2a50s', 1), ('a2a50s', 3), ('incast16', 1))
DATA = VIDEOS / 'netsim/rdma'
SAMPLE_NS = 2_000_000


def build(out):
    out.mkdir()
    tree = out / 'source'
    tree.mkdir()
    shutil.copytree(CANDIDATE / 'src', tree / 'src')
    shutil.copy2(CANDIDATE / 'CMakeLists.txt', tree)
    driver = tree / 'src/rdma/rmain.cu'
    digest = sha(driver)
    replace(driver, '    CK(cudaMalloc((void **)&p, std::max<size_t>(n, 1) * sizeof(T)));', '''    const size_t bytes = std::max<size_t>(n, 1) * sizeof(T);
    CK(cudaMalloc((void **)&p, bytes));
    extern size_t rdma_explicit_allocation_bytes;
    extern size_t rdma_explicit_allocation_count;
    rdma_explicit_allocation_bytes += bytes;
    ++rdma_explicit_allocation_count;''')
    replace(driver, 'int main(int argc, char **argv)', '''size_t rdma_explicit_allocation_bytes = 0;
size_t rdma_explicit_allocation_count = 0;

int main(int argc, char **argv)''')
    replace(driver, '    printf("flows done %u of %d; simulated', '''    printf("rdma_explicit_device_bytes=%zu rdma_explicit_allocations=%zu\\n",
           rdma_explicit_allocation_bytes, rdma_explicit_allocation_count);
    printf("flows done %u of %d; simulated''')
    binary = out / 'build/twins-rdma'
    commands = [['cmake', '-S', tree, '-B', binary.parent, '-G', 'Ninja', '-DTWINS_GPU_BACKEND=CUDA',
                 '-DCMAKE_CUDA_ARCHITECTURES=120', '-DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc'],
                ['cmake', '--build', binary.parent, '--target', 'twins-rdma', '-j2']]
    with (out / 'build.log').open('wb') as log:
        for cmd in commands:
            subprocess.run(['taskset', '-c', '10,12', *cmd], stdout=log, stderr=subprocess.STDOUT,
                           check=True, timeout=450)
    save(out / 'build-receipt.json', {'base_driver_sha256': digest, 'diagnostic_driver_sha256': sha(driver),
         'binary_sha256': sha(binary), 'commands': [list(map(str, cmd)) for cmd in commands]})
    return binary


def normalized(path):
    with path.open() as stream:
        return sorted(tuple(map(int, line.split())) for line in stream)


def samples(command, work, gpu):
    checks, values, changes = [], [], []
    with (work / 'process.log').open('wb') as log:
        start = time.monotonic_ns()
        with subprocess.Popen(command, cwd=work, stdout=log, stderr=subprocess.STDOUT) as proc:
            next_tick = start
            while proc.poll() is None:
                tick = time.monotonic_ns()
                checks.append(tick)
                for p in nv.nvmlDeviceGetComputeRunningProcesses(gpu):
                    if p.pid == proc.pid and p.usedGpuMemory is not None:
                        used = int(p.usedGpuMemory)
                        values.append(used)
                        if not changes or changes[-1][1] != used:
                            changes.append((tick - start, used))
                if tick - start > 90_000_000_000:
                    proc.kill()
                    proc.wait()
                    raise TimeoutError(work)
                next_tick = max(next_tick + SAMPLE_NS, tick)
                time.sleep(max(0, next_tick - time.monotonic_ns()) / 1_000_000_000)
            wall_ns = time.monotonic_ns() - start
    gaps = sorted(b - a for a, b in zip(checks, checks[1:]))
    return {'pid': proc.pid, 'rc': proc.returncode, 'wall_ns': wall_ns,
            'target_period_ns': SAMPLE_NS, 'polls': len(checks), 'pid_samples': len(values),
            'median_gap_ns': median(gaps) if gaps else None, 'max_gap_ns': max(gaps, default=None),
            'p99_gap_ns': gaps[(len(gaps)-1)*99//100] if gaps else None,
            'peak_process_bytes': max(values, default=None), 'pid_memory_changes_ns_bytes': changes}


def run(out, binary, case, repeat, gpu, phase, prior):
    traffic, cc = case
    name = f'k32_{traffic}_cc{cc}'
    work = out / phase / name / f'trial-{repeat}'
    work.mkdir(parents=True)
    cfg, topo, flows = DATA / 'hpcc.cfg', DATA / 'ft32.topo', DATA / f'k32_{traffic}.csv'
    command = ['taskset', '-c', '0', str(binary), str(cfg), f'--topo={topo}', f'--flows={flows}',
               '--out=output.fct', f'CC_MODE={cc}', '--fp-bases=1000003,1000033']
    inputs = {p.name: sha(p) for p in (cfg, topo, flows)}
    assert inputs == prior['input_sha256']
    hash_ = sha(binary)
    if phase == 'native':
        assert hash_ == prior['binary_sha256']
    row = samples(command, work, gpu)
    reference = DATA / f'{name}.fct'
    fct = work / 'output.fct'
    ref = normalized(reference)
    actual = normalized(fct) if fct.is_file() else []
    log = (work / 'process.log').read_text()
    explicit = re.findall(r'rdma_explicit_device_bytes=(\d+) rdma_explicit_allocations=(\d+)', log)
    if phase == 'explicit':
        assert len(explicit) == 1
    row.update(case=name, phase=phase, trial=repeat, command=command, binary_sha256=hash_,
               input_sha256=inputs, reference_sha256=sha(reference), flow_rows=len(actual),
               reference_rows=len(ref), matched=bool(ref) and actual == ref,
               explicit_requested_bytes=int(explicit[0][0]) if explicit else None,
               explicit_allocations=int(explicit[0][1]) if explicit else None)
    row['accepted'] = row['rc'] == 0 and row['matched'] and row['pid_samples'] >= 20
    save(work / 'receipt.json', row)
    print(name, phase, repeat, row['accepted'], row['peak_process_bytes'], row['explicit_requested_bytes'], flush=True)
    return row


def main():
    out = Path(sys.argv[1]).resolve()
    out.mkdir()
    prior = {row['case']: row for row in json.loads(Path(sys.argv[2]).read_text())['rows']}
    with ThreadPoolExecutor(max_workers=1) as jobs:
        diagnostic = jobs.submit(build, out / 'explicit-build')
        nv.nvmlInit()
        try:
            assert nv.nvmlDeviceGetCount() == 1
            gpu = nv.nvmlDeviceGetHandleByIndex(0)
            info = nv.nvmlDeviceGetMemoryInfo(gpu)
            name, driver = nv.nvmlDeviceGetName(gpu), nv.nvmlSystemGetDriverVersion()
            values = {'name': name.decode() if isinstance(name, bytes) else name,
                      'driver': driver.decode() if isinstance(driver, bytes) else driver,
                      'device_total_bytes': info.total, 'device_free_bytes': info.free, 'rows': []}
            save(out / 'summary.json', values)
            with LOCK.open('r') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                for repeat in range(3):
                    for case in reversed(CASES) if repeat % 2 else CASES:
                        name = f'k32_{case[0]}_cc{case[1]}'
                        values['rows'].append(run(out, CANDIDATE / 'build/twins-rdma', case, repeat, gpu, 'native', prior[name]))
                        save(out / 'summary.json', values)
            binary = diagnostic.result()
            with LOCK.open('r') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                for case in CASES:
                    name = f'k32_{case[0]}_cc{case[1]}'
                    values['rows'].append(run(out, binary, case, 0, gpu, 'explicit', prior[name]))
                    save(out / 'summary.json', values)
        finally:
            nv.nvmlShutdown()
    return int(len(values['rows']) != 20 or not all(r['accepted'] for r in values['rows']))


if __name__ == '__main__':
    raise SystemExit(main())
