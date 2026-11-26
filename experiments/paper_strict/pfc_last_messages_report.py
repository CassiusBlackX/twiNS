"""Pair the native packet-level witnesses and verify the isolated wake trials."""

from collections import Counter
import json
from pathlib import Path
import subprocess
import sys


BLOCKERS = {1: (2, 850204), 4: (5, 835067)}
POOLS = (24, 16, 12, 8, 6)


def load(path):
    return json.loads(path.read_text())


def checked_archive(root):
    assert (root / 'remote.rc').read_text().strip() == '0', root
    index = root / 'returned-files.sha256'
    subprocess.run(['sha256sum', '--quiet', '-c', index.name], cwd=root, check=True)
    return len(index.read_text().splitlines())


def transcript(path):
    selected = {i: [] for i in range(8)}
    with path.open() as stream:
        for line in stream:
            watch, ordinal, ns, stage, node, port, *detail = line.split()
            selected[int(watch)].append([int(ordinal), int(ns), stage, int(node), int(port), *map(int, detail)])
    return selected


def packet_event(row):
    return {'ordinal': row[0], 'time_ns': row[1], 'stage': row[2], 'node': row[3], 'ingress_port': row[4],
            'egress_port': row[5], 'seq': row[8], 'uid': row[9], 'wire_bytes': row[10], 'shared_used': row[14],
            'signed_room': row[15], 'threshold': row[16], 'ingress_bytes': row[18], 'headroom': row[19],
            'headroom_limit': row[20], 'ingress_shared': row[21], 'mmu_paused': row[22]}


def compare_drop(loss, control):
    same = next((r for r in control if r[2] == 'admit' and r[3:5] == loss[3:5] and r[7:9] == loss[7:9]), None)
    drop = packet_event(loss)
    assert drop['signed_room'] >= 0
    assert drop['wire_bytes'] + drop['headroom'] > drop['headroom_limit']
    assert drop['wire_bytes'] + drop['ingress_shared'] > drop['threshold']
    return {'losing': drop, 'admitted_control': packet_event(same) if same is not None else None}


def timeline(events, target, controls):
    counts = Counter(x[2] for x in events)
    sender = [r for r in events if r[2] == 'wire_tx' and r[3] == target['src'] and r[5] == 17]
    feedback = [r for r in events if r[2] == 'sender_feedback_trigger']
    rx = [r for r in events if r[2] == 'rdma_data_rx']
    nacks = [r for r in rx if r[-3] == 2]
    suppressed = [r for r in rx if r[-3] == 4]
    dropped = [r for r in events if r[2] == 'mmu_drop']
    missing = max(r[9] for r in rx)
    assert sender and feedback and missing < target['size'] and counts['sender_complete'] == 0
    assert counts['queue_drop'] == counts['no_route'] == 0
    result = {'identity': target, 'last_sender_data': sender[-1], 'last_feedback_trigger': feedback[-1],
              'last_receiver_data': rx[-1], 'receiver_expected_bytes': missing, 'event_counts': counts,
              'nack_generators': nacks, 'suppressed_ooo': len(suppressed),
              'last_suppressed_ooo': suppressed[-1] if suppressed else None,
              'sender_rewinds': [r for r in events if r[2] == 'sender_nack_recover'],
              'mmu_drop_pairs_to_8mib': [compare_drop(r, controls[8]) for r in dropped],
              'control_terminal_acks': {p: next(r for r in rows if r[2] == 'sender_complete') for p,rows in controls.items()}}
    watch = target['watch']
    if watch in BLOCKERS:
        blocker, time = BLOCKERS[watch]
        stalled = next(r for r in events if r[2] == 'nic_no_send' and r[1] == time)
        assert stalled[5:7] == [missing, missing]
        assert stalled[7] == target['size'] - missing and stalled[8] > time
        assert stalled[9:11] == [0, 0] and stalled[11] < time and stalled[12] == 1 and stalled[-1] == 0
        assert stalled[-3] == 1  # The QP contributing the stale minimum is window bound.
        result.update(reason='stale_nic_minimum', blocker=blocker, no_wake=stalled,
                      paired_next_data={p: next(r for r in rows if r[2] == 'wire_tx' and r[3] == target['src']
                                                and r[5:7] == [17, missing]) for p,rows in controls.items()})
    elif watch == 2:
        assert len(dropped) == 1 and missing == dropped[0][8] == sender[-1][6] and not nacks
        stop = next(r for r in reversed(events) if r[2] == 'nic_no_send')
        assert stop[10] == 1 and stop[9] == 0  # Window-bound after actual feedback handling.
        result.update(reason='last_in_flight_packet_lost', no_wake=stop)
    else:
        copies = [row for row in dropped if row[8] == missing]
        assert len(copies) == 2 and len(nacks) == len(result['sender_rewinds']) == 1
        assert suppressed and suppressed[-1][1] < nacks[0][-2]
        assert copies[0][1] < nacks[0][1] < result['sender_rewinds'][0][1] < copies[1][1]
        assert all(pair['admitted_control'] for pair in result['mmu_drop_pairs_to_8mib'])
        result.update(reason='retransmission_dropped_during_nack_suppression')
    return result


def verify_intervention(root, pool, baseline, expected):
    record = load(root / f'wake-{pool:02d}-summary.json')
    injection = record['first_injected_wake']
    trace = []
    with (root / f'wake-{pool:02d}-target.log').open() as stream:
        for row in stream:
            order, ns, stage, identity, *data = row.split()
            if int(order) < injection[0]:
                trace.append([int(order), int(ns), stage, *map(int, data)])
    selected = [r for r in baseline if r[0] < injection[0] and not r[2].endswith('_timer')]
    assert trace == selected, (pool, 'pre-intervention prefix changed')
    assert record['target_completed'] and record['stranded_ids'] == expected
    return record | {'unchanged_prefix_event_count': len(trace)}


def main():
    witness, wake, output = map(Path, sys.argv[1:4])
    archive_counts = {'native_witness': checked_archive(witness), 'wake_trials': checked_archive(wake)}
    thresholds, cases, events = {}, {}, {}
    for pool in POOLS:
        stem = witness / f'pool-{pool:02d}-evidence'
        stats = load(stem / 'threshold-by-port.json')
        assert not any(row[3] for row in stats) and all(row[4] > 0 for row in stats)
        thresholds[pool] = {'node_ports': len(stats), 'calls': sum(row[2] for row in stats),
                            'wraps': 0, 'minimum_signed_room_bytes': min(row[4] for row in stats)}
        cases[pool] = load(stem / 'messages.json')
        events[pool] = transcript(stem / 'selected-events.log')
        assert load(stem/'receipt.json')['native_outputs_equal']

    causal = []
    for pool in (24, 16, 12):
        failed = [f for f in cases[pool] if not f['reference_completed']]
        for target in failed:
            watch = target['watch']
            result = timeline(events[pool][watch], target, {p: events[p][watch] for p in (8,6)})
            for control in (8, 6):
                state = next(f for f in cases[control] if f['watch'] == watch)
                assert state['reference_completed']
            if watch in BLOCKERS:
                other = next(f for f in failed if f['watch'] == BLOCKERS[watch][0])
                assert result['no_wake'][14:16] == [other['dst'], other['sport']]
                result['intervention'] = verify_intervention(wake, pool, events[pool][watch],
                                                            sorted(f['trace_id'] for f in failed if f != target))
            causal.append(result)
    assert len(causal) == 7
    output.write_text(json.dumps({'cases': causal, 'dynamic_mmu': thresholds, 'verified_files':archive_counts}, indent=2)+'\n')
    print(output)


if __name__ == '__main__':
    main()
