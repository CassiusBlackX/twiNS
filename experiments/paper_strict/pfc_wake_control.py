"""Replay just the missed pacing wake on each of the two affected sender QPs."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import shutil
import subprocess
import sys

from crosspod import replace, sha
import pfc_last_messages as study
import pfc_sustained as pfc
from pfc_sustained import save


CASES = {24: (1, 108, 2, 850204), 16: (4, 121, 2, 835067)}


def patch_wake(module):
    nic = module / 'qbb-net-device.cc'
    replace(nic, 'const bool schedule = expired && t < Simulator::GetMaximumSimulationTime() && t > Simulator::Now();',
            'bool schedule = expired && t < Simulator::GetMaximumSimulationTime() && t > Simulator::Now();')
    token = '''                if (schedule) {
                    m_nextSend = Simulator::Schedule(t - Simulator::Now(), &QbbNetDevice::DequeueAndTransmit, this);
                }'''
    replace(nic, token, '''                if (!schedule && expired && t <= Simulator::Now()) {
                    static const char *control = std::getenv("TWINS_PFC_TEST_WAKE");
                    uint32_t target, host, dev;
                    long long after;
                    if (control && std::sscanf(control, "%u,%u,%u,%lld", &target, &host, &dev, &after) == 4 &&
                        m_node->GetId() == host && m_ifIndex == dev && Simulator::Now().GetTimeStep() >= after) {
                        for (uint32_t i = 0; i < m_rdmaEQ->GetFlowCount(); ++i) {
                            auto q = m_rdmaEQ->GetQp(i);
                            if (pfc_witness::id(*q) == target && q->GetBytesLeft() && !q->IsWinBound() &&
                                !m_paused[q->m_pg] && q->m_nextAvail > Simulator::Now()) {
                                t = q->m_nextAvail;
                                schedule = true;
                                pfc_witness::event("test_wake", target, host, dev,
                                                   {int64_t(t.GetTimeStep()), int64_t(q->snd_una)});
                                break;
                            }
                        }
                    }
                }
''' + token)


def selected_events(path, watch):
    selected = []
    with path.open() as stream:
        for line in stream:
            fields = line.split()
            if fields[0] != 'E' or int(fields[4]) != watch:
                continue
            stage = fields[3]
            if not stage.endswith('_timer'):
                selected.append([int(fields[1]), int(fields[2]), stage, *map(int, fields[4:])])
    return selected


def run(out, pool, core):
    label = f'pool-{pool:02d}'
    watch, host, port, earliest = CASES[pool]
    rows = json.loads((out / 'watch-matrix.json').read_text())[str(pool)]
    base, _ = study.case_paths(pool)
    design = out / f'wake-{pool:02d}' / 'measurements'
    pfc.MODEL = out.parent / 'simai-copy'
    pfc.DESIGN = [(label, pool, 8)]
    case = design / label
    case.mkdir(parents=True)
    shutil.copytree(base / 'input', case / 'input')
    watch_path = case / 'input/watch.txt'
    watch_path.write_text(''.join(f'{v["watch"]} {v["src"]} {v["dst"]} {v["sport"]}\n' for v in rows))
    options = ['-v', f'{study.STOP_HOOK}:/job/reference-stop.so:ro', '-e', 'LD_PRELOAD=/job/reference-stop.so',
               '-e', 'TWINS_REFERENCE_STOP_NS=40000000', '-e', 'TWINS_PFC_WATCH=/case/watch.txt',
               '-e', 'TWINS_PFC_WITNESS=/raw/last-message-events.tsv',
               '-e', f'TWINS_PFC_TEST_WAKE={watch},{host},{port},{earliest}']
    pfc.reference(design, core, trace_wire=False, timeout=140, docker_options=options)
    reference = case / 'reference'
    receipt = json.loads((reference / 'receipt.json').read_text())
    assert receipt['rc'] == receipt['extract_rc'] == 0
    raw = design.parent.parent / 'raw-pfc' / label
    fct = {int(line.split()[0]) for line in (reference / 'messages.fct').open()}
    observations = study.read_flows(raw / 'events.trace')
    target = next(row for row in rows if row['watch'] == watch)
    assert study.key(observations[target['trace_id']]) == study.key(target)
    capture = raw / 'last-message-events.tsv'
    evidence = selected_events(capture, watch)
    interventions = [r for r in evidence if r[2] == 'test_wake']
    assert interventions and interventions[0][1] == earliest
    result = {'pool_mib': pool, 'complete_count': len(fct), 'stranded_ids': sorted(set(observations)-fct),
              'target_completed': target['trace_id'] in fct, 'target': target, 'extra_wakes': len(interventions),
              'first_injected_wake': interventions[0], 'full_witness_sha256': sha(capture)}
    save(out / f'wake-{pool:02d}-summary.json', result)
    with (out / f'wake-{pool:02d}-target.log').open('w') as stream:
        for line in evidence:
            stream.write(' '.join(map(str, line))+'\n')
    print('wake', pool, result['target_completed'], len(fct), len(interventions), flush=True)


def main():
    out = Path(sys.argv[1]).resolve()
    if len(sys.argv) > 2:
        return run(out, *map(int,sys.argv[2:4]))
    out.mkdir()
    study.freeze_watch(out)
    study.build(out, patch_wake)

    def worker(item):
        pool, core = item
        subprocess.run([sys.executable, __file__, out, str(pool), str(core)], check=True)

    with ThreadPoolExecutor(max_workers=2) as jobs:
        list(jobs.map(worker, ((24, 4), (16, 8))))


if __name__ == '__main__':
    main()
