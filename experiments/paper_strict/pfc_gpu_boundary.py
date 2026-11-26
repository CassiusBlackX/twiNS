"""Start the constant-size PFC sweep with an explicit host-side cutoff."""

from concurrent.futures import ThreadPoolExecutor
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

from crosspod import CANDIDATE, LOCK, OBSERVER, ROOT, replace, sha
from mechanism_followup import PFC_CASES, pfc_sweep
from pfc_sustained import normalize, save


PREVIOUS = ROOT / 'mechanism_followup/20261123T140937Z'
BOUNDED = ROOT / 'pfc_bounded_reference/20261123T141438Z/results'
DIAGNOSTIC = ROOT / 'pfc_sustained/20261123T085920Z/results/build-receipts/source'
CAP_NS = 20_000_000


def build(out, source, core, backend='CUDA'):
    out.mkdir(parents=True)
    source_code = out / 'source'
    source_code.mkdir()
    shutil.copytree(source / 'src', source_code / 'src')
    shutil.copy2(source / 'CMakeLists.txt', source_code)
    (source_code / 'out').mkdir()
    (source_code / 'out/astra').symlink_to((source / 'out/astra').resolve(), target_is_directory=True)
    closed = source_code / 'src/astra/closed.h'
    base_hash = sha(closed)
    replace(closed, '            if (fe->finished)\n                return;\n            // nothing more to come',
            '''            if (fe->finished)
                return;
            if (S >= s.max_slots)
            {
                fprintf(stderr, "strict_pfc_stop slot=%lld time_ns=%lld\\n",
                        (long long)S, (long long)reach);
                return;
            }
            // nothing more to come''')
    output = out / 'compiled'
    arch, compiler = {'CUDA': ('120', '/usr/local/cuda/bin/nvcc'),
                      'HIP': ('gfx1200', '/opt/rocm/llvm/bin/clang++')}[backend]
    commands = [['taskset', '-c', core, 'cmake', '-S', source_code, '-B', output, '-G', 'Ninja',
                 f'-DTWINS_GPU_BACKEND={backend}', f'-DCMAKE_{backend}_ARCHITECTURES={arch}',
                 f'-DCMAKE_{backend}_COMPILER={compiler}'],
                ['taskset', '-c', core, 'cmake', '--build', output, '--target', 'twins-ai', '-j2']]
    with (out / 'build.log').open('wb') as stream:
        for command in commands:
            subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT, check=True, timeout=650)
    binary = output / 'twins-ai'
    save(out / 'receipt.json', {'commands': [list(map(str, cmd)) for cmd in commands], 'input_source': str(source),
         'unmodified_closed_sha256': base_hash, 'closed_sha256': sha(closed), 'binary_sha256': sha(binary)})
    return binary


def load_reference(out):
    base = PREVIOUS / 'results/pfc'
    out.mkdir()
    for label, pool, _ in PFC_CASES:
        target = out / label
        target.mkdir()
        shutil.copytree(base / label / 'input', target / 'input')
        record = json.loads((target / 'input/provenance.json').read_text())
        assert record['common_stop_time_ns'] == CAP_NS and record['config_sha256'] == sha(target / 'input/config.log')
        reference = base / label / 'reference'
        method = 'native-exit'
        if pool in (12, 16, 24):
            refs = [BOUNDED / f'cap-{stop}ms/ref-{stop}/pfc/{label}/reference' for stop in (20, 40)]
            for r in refs:
                status = json.loads((r / 'receipt.json').read_text())
                assert status['rc'] == status['extract_rc'] == 0
            for name, dag in (('messages.fct', False), ('actions.dag', True)):
                assert normalize(refs[0] / name, dag) == normalize(refs[1] / name, dag)
            shutil.copytree(reference, target / 'native-reference')
            reference = refs[0]
            method = 'stop-at-20ms-stable-at-40ms'
        shutil.copytree(reference, target / 'reference')
        save(target / 'reference-provenance.json', {'method': method, 'reference_path': str(reference)})


def handoff(out):
    """Keep all completed AllGather data, then switch the pending sweep to the bounded build."""
    allgather = PREVIOUS / 'results/allgather'
    limit = time.monotonic() + 2400
    while time.monotonic() < limit:
        trials = allgather / 'trials.json'
        if trials.is_file() and len(json.loads(trials.read_text())) >= 11:
            break
        time.sleep(6)
    else:
        raise TimeoutError('The last AllGather repetition did not arrive')
    with LOCK.open('r') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        result = allgather / 'summary.json'
        deadline = time.monotonic() + 25
        while not result.is_file() and time.monotonic() < deadline:
            time.sleep(0.25)
        status = json.loads(result.read_text())
        assert len(json.loads((allgather / 'trials.json').read_text())) == 15
        needle = [str(PREVIOUS / 'mechanism_followup.py'), str(PREVIOUS / 'results'), 'timed']
        tasks = []
        for path in Path('/proc').glob('[0-9]*/cmdline'):
            try:
                args = path.read_bytes().decode(errors='replace').strip('\0').split('\0')
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                continue
            if args[-3:] == needle and len(args) == 4:
                tasks.append(int(path.parent.name))
        assert len(tasks) == 1, f'Expected exactly one scheduled old GPU worker, got {tasks}'
        assert not list((PREVIOUS / 'results/pfc').glob('pool-*/checked-*'))
        os.kill(tasks[0], signal.SIGTERM)
        save(out / 'handoff.json', {'allgather_output': str(allgather), 'allgather_accepted': status['accepted'],
             'reason': 'The earlier host coordinator could keep launching after the device reached the cutoff.',
             'stopped_worker': tasks[0]})
        timed = out / 'build/clean/compiled/twins-ai'
        counts = out / 'build/diagnostic/compiled/twins-ai'
        rc = subprocess.run([sys.executable, OBSERVER, out / 'resources', sys.executable, __file__, out,
                             'sweep', timed, counts]).returncode
        return rc


def main():
    out = Path(sys.argv[1]).resolve()
    if len(sys.argv) > 2:
        return pfc_sweep(out / 'pfc', *map(Path, sys.argv[3:5]))
    out.mkdir()
    load_reference(out / 'pfc')
    with ThreadPoolExecutor(max_workers=2) as workers:
        work = [workers.submit(build, out / 'build' / arm, source, cores)
                for arm, source, cores in [('clean', CANDIDATE, '6,10'), ('diagnostic', DIAGNOSTIC, '12,14')]]
        for job in work:
            job.result()
    return handoff(out)


if __name__ == '__main__':
    raise SystemExit(main())
