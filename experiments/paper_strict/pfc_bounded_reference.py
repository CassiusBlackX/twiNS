"""Recover completed SimAI observables with an explicit simulation-time cap."""

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import subprocess
import sys

import pfc_sustained as pfc
from crosspod import sha


HOOK = r'''
#include <ns3/simulator.h>
#include <cstdio>
#include <cstdlib>
#include <dlfcn.h>

void ns3::Simulator::Run()
{
    auto real_run = reinterpret_cast<void (*)()>(dlsym(RTLD_NEXT, "_ZN3ns39Simulator3RunEv"));
    const char *value = std::getenv("TWINS_REFERENCE_STOP_NS");
    if (!real_run || !value)
        std::abort();
    const long long stop_ns = std::strtoll(value, nullptr, 10);
    if (stop_ns <= 0)
        std::abort();
    std::fprintf(stderr, "strict_reference_stop_ns=%lld\n", stop_ns);
    ns3::Simulator::Stop(ns3::NanoSeconds(static_cast<uint64_t>(stop_ns)));
    real_run();
}
'''


def compile_hook(out):
    src = out / 'reference-stop.cc'
    src.write_text(HOOK)
    command = ['docker', 'run', '--rm', '--network=none', '--cpuset-cpus', '5', '--memory=10g',
               '--user', f'{os.getuid()}:{os.getgid()}', '-v', f'{pfc.MODEL}:/simai:ro', '-v', f'{out}:/job',
               'localhost/simai-build', 'g++', '-std=c++20', '-O2', '-shared', '-fPIC',
               '-I', f'/simai/{pfc.SIM}/trial-build/include', '/job/reference-stop.cc', '-o', '/job/reference-stop.so', '-ldl']
    with (out / 'build.log').open('wb') as log:
        subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=90)
    pfc.save(out / 'build.json', {'command': command, 'source_sha256': sha(src), 'hook_sha256': sha(out / 'reference-stop.so')})


def run(out, stop_ms, core):
    # Include both established endpoints as controls for a cap later than their completion.
    cap = out / f'cap-{stop_ms}ms'
    cases = cap / f'ref-{stop_ms}' / 'pfc'
    pfc.DESIGN = [(f'pool-{pool:02d}', pool, 8) for pool in (64, 24, 16, 12, 6)]
    pfc.freeze(cases)
    options = ['-v', f'{out / "reference-stop.so"}:/job/reference-stop.so:ro',
               '-e', 'LD_PRELOAD=/job/reference-stop.so', '-e', f'TWINS_REFERENCE_STOP_NS={stop_ms * 1_000_000}']
    pfc.reference(cases, core, trace_wire=False, timeout=105, docker_options=options)
    for label, _, _ in pfc.DESIGN:
        d = cases / label / 'reference'
        text = (d / 'process.log').read_text()
        if f'strict_reference_stop_ns={stop_ms * 1_000_000}' not in text:
            raise ValueError(f'The simulator did not install the cutoff for {label}')


def main():
    out = Path(sys.argv[1]).resolve()
    if len(sys.argv) > 2:
        return run(out, *map(int, sys.argv[2:4]))
    out.mkdir()
    compile_hook(out)

    def child(job):
        stop_ms, core = job
        subprocess.run([sys.executable, __file__, out, str(stop_ms), str(core)], check=True)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(child, ((20, 5), (40, 7))))


if __name__ == '__main__':
    main()
