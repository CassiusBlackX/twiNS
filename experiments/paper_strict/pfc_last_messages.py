"""Trace the seven stranded DCN+ messages in the frozen sequential SimAI model."""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from crosspod import ROOT, replace, sha
import pfc_sustained as pfc
from pfc_sustained import save


POOLS = (24, 16, 12, 8, 6)
EXPECTED_MISSING = {24: 2, 16: 3, 12: 2, 8: 0, 6: 0}
PREVIOUS = ROOT / 'pfc_gpu_boundary/20261123T142123Z/results/pfc'
BOUNDED = ROOT / 'pfc_bounded_reference/20261123T141438Z/results'
STOP_HOOK = BOUNDED / 'reference-stop.so'
NORMAL = ROOT / 'mechanism_followup/20261123T140937Z'
ORIGIN_MODEL = pfc.MODEL
WATCH_SCHEMA = {
    'wire_tx/wire_rx/phy_drop/queue_drop': ['ip_protocol', 'seq_or_ack', 'packet_uid', 'wire_bytes', 'ecn'],
    'wire PFC (watch=0)': ['ip_protocol', 'priority_group', 'pause_time', 'packet_uid'],
    'admit/mmu_drop/priority_bypass': ['out_port', 'priority_group', 'ip_protocol', 'seq_or_ack', 'packet_uid',
        'wire_bytes', 'buffer_bytes', 'reserved_headroom', 'reserved_bytes', 'shared_used', 'signed_room', 'threshold',
        'alpha_shift', 'ingress_bytes', 'headroom_bytes', 'headroom_limit', 'ingress_shared', 'mmu_paused', 'egress_bytes'],
    'rdma_data_rx': ['seq', 'payload_bytes', 'packet_uid', 'expected_before', 'expected_after', 'receiver_code',
                     'nack_suppression_until_ns', 'last_nack'],
    'sender_*': ['snd_nxt', 'snd_una', 'size', 'bytes_left', 'window_bound', 'next_avail_ns', 'marker', 'extra'],
    'nic_no_send': ['snd_nxt', 'snd_una', 'bytes_left', 'own_next_avail_ns', 'own_paused', 'own_window_bound',
                    'minimum_next_avail_ns', 'wake_event_expired', 'wake_event_ns', 'minimum_dst',
                    'minimum_sport', 'minimum_bytes_left', 'minimum_window_bound', 'minimum_paused',
                    'would_schedule'],
    'event_prefix': ['ordinal', 'time_ns', 'stage', 'watch', 'node', 'port'],
    'mmu_threshold_aggregate': ['node', 'port', 'calls', 'wraps', 'min_signed_room', 'min_at_ns',
                                'first_wrap_at_ns', 'max_shared', 'threshold_at_min'],
}


def read_flows(raw):
    flows = {}
    with raw.open() as stream:
        for line in stream:
            x = line.split()
            if x[0] != 'F':
                continue
            n, src, dst, size, sport = map(int, x[1:6])
            flows[n] = {'trace_id': n, 'src': src, 'dst': dst, 'size': size, 'sport': sport,
                        'cause': x[7], 'cause_id': int(x[8]), 'ordinal': int(x[9]), 'created_ns': int(x[10]),
                        'simccl_flow_id': int(x[11]), 'tag': int(x[12])}
    return flows


def key(flow):
    return tuple(flow[k] for k in ('src', 'dst', 'size', 'simccl_flow_id', 'tag'))


def case_paths(pool):
    label = f'pool-{pool:02d}'
    base = PREVIOUS / label
    old = (NORMAL / 'raw-pfc' if pool == 8 else BOUNDED / 'cap-20ms/raw-pfc') / label
    return base, old / 'events.trace'


def freeze_watch(out):
    identities = {}
    by_case = {}
    for pool in POOLS:
        base, old = case_paths(pool)
        complete = {int(line.split()[0]) for line in (base / 'reference/messages.fct').read_text().splitlines()}
        observed = read_flows(old)
        assert len(observed) == 1920 and len({key(v) for v in observed.values()}) == 1920
        missing = sorted(set(observed) - complete)
        assert len(missing) == EXPECTED_MISSING[pool]
        for i in missing:
            k = key(observed[i])
            assert k not in identities
            identities[k] = {'watch': len(identities) + 1, 'stranded_pool_mib': pool}
        by_case[pool] = {key(v): v | {'reference_completed': i in complete} for i, v in observed.items()}
    targets = {}
    for pool, flows in by_case.items():
        targets[str(pool)] = [flows[k] | identity for k, identity in identities.items()]
    save(out / 'watch-matrix.json', targets)
    save(out / 'trace-schema.json', WATCH_SCHEMA)


def insert(path, marker, addition, expected=1):
    source = path.read_text()
    if source.count(marker) != expected:
        raise ValueError(f'Unexpected marker in {path}: {marker!r}')
    path.write_text(source.replace(marker, marker + addition))


def patch_model(tree):
    module = tree / pfc.SIM / 'src/point-to-point/model'
    shutil.copy2(Path(__file__).with_suffix('.h'), module / 'pfc-last-messages.h')
    for name in ('switch-mmu.cc', 'switch-node.cc', 'rdma-hw.cc', 'qbb-net-device.cc'):
        path = module / name
        path.write_text('#include "pfc-last-messages.h"\n' + path.read_text())

    mmu = module / 'switch-mmu.cc'
    replace(mmu, '\t\treturn (buffer_size - total_hdrm - total_rsrv - shared_used_bytes) >> pfc_a_shift[port];',
            '''        const uint32_t value = (buffer_size - total_hdrm - total_rsrv - shared_used_bytes) >> pfc_a_shift[port];
        pfc_witness::threshold(*this, port, value);
        return value;''')

    sw = module / 'switch-node.cc'
    replace(sw, '''		if (qIndex != 0){ //not highest priority
			if (m_mmu->CheckIngressAdmission(inDev, qIndex, p->GetSize()) && m_mmu->CheckEgressAdmission(idx, qIndex, p->GetSize())){			// Admission control''',
            '''        const auto identity = pfc_witness::id(ch);
        if (qIndex != 0) {
            const bool admitted = m_mmu->CheckIngressAdmission(inDev, qIndex, p->GetSize()) &&
                                  m_mmu->CheckEgressAdmission(idx, qIndex, p->GetSize());
            pfc_witness::admission(admitted ? "admit" : "mmu_drop", identity, *m_mmu,
                                   inDev, idx, qIndex, ch, p);
            if (admitted) {''')
    insert(sw, '\t\tm_bytes[inDev][idx][qIndex] += p->GetSize();', '''
        if (qIndex == 0)
            pfc_witness::admission("priority_bypass", identity, *m_mmu, inDev, idx, qIndex, ch, p);''')
    replace(sw, '\t}else\n\t{\n\t\treturn; // Drop', '''    }else
    {
        pfc_witness::wire("no_route", p, ch, GetId(), 0);
        return; // Drop''')

    nic = module / 'qbb-net-device.cc'
    insert(nic, '\t\tm_phyTxBeginTrace(m_currentPkt);', '''
        if (pfc_witness::trace().output) {
            CustomHeader h(CustomHeader::L2_Header | CustomHeader::L3_Header | CustomHeader::L4_Header);
            h.getInt = 1;
            p->PeekHeader(h);
            pfc_witness::wire("wire_tx", p, h, m_node->GetId(), m_ifIndex);
        }''', expected=2)
    replace(nic, '\t\tpacket->PeekHeader(ch);\n\t\tif (ch.l3Prot == 0xFE){ // PFC', '''        packet->PeekHeader(ch);
        pfc_witness::wire("wire_rx", packet, ch, m_node->GetId(), m_ifIndex);
        if (ch.l3Prot == 0xFE){ // PFC''')
    replace(nic, '\t\tm_queue->Enqueue(packet, qIndex);\n\t\t// DequeueAndTransmit();', '''        const auto watched = pfc_witness::id(ch);
        const auto before = watched ? m_queue->GetNBytes(qIndex) : 0;
        m_queue->Enqueue(packet, qIndex);
        if (watched && m_queue->GetNBytes(qIndex) == before)
            pfc_witness::wire("queue_drop", packet, ch, m_node->GetId(), m_ifIndex);
        // DequeueAndTransmit();''')
    replace(nic, '''				if (m_nextSend.IsExpired() && t < Simulator::GetMaximumSimulationTime() && t > Simulator::Now()){
					m_nextSend = Simulator::Schedule(t - Simulator::Now(), &QbbNetDevice::DequeueAndTransmit, this);
				}
			}
			return;''', '''                const bool expired = m_nextSend.IsExpired();
                const bool schedule = expired && t < Simulator::GetMaximumSimulationTime() && t > Simulator::Now();
                if (pfc_witness::trace().output && Simulator::Now().GetTimeStep() <= 2000000) {
                    Ptr<RdmaQueuePair> minimum;
                    for (uint32_t i = 0; i < m_rdmaEQ->GetFlowCount() && !minimum; ++i) {
                        auto q = m_rdmaEQ->GetQp(i);
                        if (q->m_nextAvail == t) minimum = q;
                    }
                    for (uint32_t i = 0; i < m_rdmaEQ->GetFlowCount(); ++i) {
                        auto q = m_rdmaEQ->GetQp(i);
                        if (auto watched = pfc_witness::id(*q))
                            pfc_witness::event("nic_no_send", watched, m_node->GetId(), m_ifIndex,
                                {int64_t(q->snd_nxt), int64_t(q->snd_una), int64_t(q->GetBytesLeft()),
                                 int64_t(q->m_nextAvail.GetTimeStep()), m_paused[q->m_pg], q->IsWinBound(),
                                 int64_t(t.GetTimeStep()), expired, int64_t(m_nextSend.GetTs()),
                                 minimum ? int64_t(pfc_witness::node(minimum->dip.Get())) : -1,
                                 minimum ? int64_t(minimum->sport) : -1,
                                 minimum ? int64_t(minimum->GetBytesLeft()) : -1,
                                 minimum ? int64_t(minimum->IsWinBound()) : -1,
                                 minimum ? int64_t(m_paused[minimum->m_pg]) : -1, schedule});
                    }
                }
                if (schedule) {
                    m_nextSend = Simulator::Schedule(t - Simulator::Now(), &QbbNetDevice::DequeueAndTransmit, this);
                }
            }
            return;''')

    hw = module / 'rdma-hw.cc'
    replace(hw, '\tint x = ReceiverCheckSeq(ch.udp.seq, rxQp, payload_size);', '''    const auto was_expected = rxQp->ReceiverNextExpectedSeq;
    int x = ReceiverCheckSeq(ch.udp.seq, rxQp, payload_size);
    if (auto watched = pfc_witness::id(ch))
        pfc_witness::event("rdma_data_rx", watched, m_node->GetId(), 0,
                           {ch.udp.seq, payload_size, (int64_t)p->GetUid(), (int64_t)was_expected,
                            (int64_t)rxQp->ReceiverNextExpectedSeq, x,
                            (int64_t)rxQp->m_nackTimer.GetTimeStep(), (int64_t)rxQp->m_lastNACK});''')
    insert(hw, '\tPtr<RdmaQueuePair> qp = GetQp(ch.sip, port, qIndex);', '''
    if (qp == NULL)
        pfc_witness::wire("ack_without_qp", p, ch, m_node->GetId(), 0);
    else
        pfc_witness::sender("sender_feedback_before", qp, m_node->GetId(), seq, ch.l3Prot);''')
    replace(hw, '\t\tif (qp->IsFinished()){\n\t\t\tQpComplete(qp);', '''        pfc_witness::sender("sender_feedback_after", qp, m_node->GetId(), seq, ch.l3Prot);
        if (qp->IsFinished()){
            pfc_witness::sender("sender_complete", qp, m_node->GetId(), seq, ch.l3Prot);
            QpComplete(qp);''')
    insert(hw, '\t// ACK may advance the on-the-fly window, allowing more packets to send',
           '\n    pfc_witness::sender("sender_feedback_trigger", qp, m_node->GetId(), seq, ch.l3Prot);')
    insert(hw, 'void RdmaHw::RecoverQueue(Ptr<RdmaQueuePair> qp){\n\tqp->snd_nxt = qp->snd_una;',
           '\n    pfc_witness::sender("sender_nack_recover", qp, m_node->GetId());')
    insert(hw, '\t// update state\n\tqp->snd_nxt += payload_size;', '''
    pfc_witness::sender("sender_emit", qp, m_node->GetId(), (int64_t)(qp->snd_nxt - payload_size),
                       (int64_t)p->GetUid());''')
    for function, stage in (('RateIncEventTimerMlx', 'sender_rate_timer'),
                            ('CheckRateDecreaseMlx', 'sender_decrease_timer'), ('UpdateAlphaMlx', 'sender_alpha_timer')):
        insert(hw, f'void RdmaHw::{function}(Ptr<RdmaQueuePair> q){{',
               f'\n    pfc_witness::sender("{stage}", q, m_node->GetId());')
    return module


def build(out, diagnostic_patch=None):
    tree = out.parent / 'simai-copy'
    subprocess.run(['cp', '-a', '--reflink=auto', ORIGIN_MODEL, tree], check=True, timeout=90)
    module = patch_model(tree)
    if diagnostic_patch is not None:
        diagnostic_patch(module)
    build_cmd = ['docker', 'run', '--rm', '--network=none', '--cpuset-cpus', '4,6,8',
                 '--memory=24g', '--user', f'{os.getuid()}:{os.getgid()}', '-v', f'{tree}:/simai',
                 'localhost/simai-build', 'cmake', '--build', f'/simai/{pfc.SIM}/trial-cmake',
                 '--target', 'libpoint-to-point', '-j3']
    with (out / 'build.log').open('wb') as log:
        subprocess.run(build_cmd, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=600)
    soname = 'trial-build/lib/libns3.36.1-point-to-point-optimized.so'
    base, variant = ((path / pfc.SIM / soname) for path in (ORIGIN_MODEL, tree))
    save(out / 'build.json', {'command': list(map(str, build_cmd)), 'native_library_sha256': sha(base),
                             'instrumented_library_sha256': sha(variant), 'stop_hook_sha256': sha(STOP_HOOK),
                             'source_sha256': {p.name: sha(p) for p in module.iterdir() if p.name in
                                 ('pfc-last-messages.h', 'switch-mmu.cc', 'switch-node.cc', 'qbb-net-device.cc', 'rdma-hw.cc')}})


def case_run(out, pool, core):
    label = f'pool-{pool:02d}'
    rows = json.loads((out / 'watch-matrix.json').read_text())[str(pool)]
    base, _ = case_paths(pool)
    design = out / f'run-{pool:02d}' / 'measurements'
    pfc.MODEL = out.parent / 'simai-copy'
    pfc.DESIGN = [(label, pool, 8)]
    case = design / label
    case.mkdir(parents=True)
    shutil.copytree(base / 'input', case / 'input')
    watch = case / 'input/watch.txt'
    watch.write_text(''.join(f'{v["watch"]} {v["src"]} {v["dst"]} {v["sport"]}\n' for v in rows))
    options = ['-v', f'{STOP_HOOK}:/job/reference-stop.so:ro', '-e', 'LD_PRELOAD=/job/reference-stop.so',
               '-e', 'TWINS_REFERENCE_STOP_NS=40000000', '-e', 'TWINS_PFC_WATCH=/case/watch.txt',
               '-e', 'TWINS_PFC_WITNESS=/raw/last-message-events.tsv']
    pfc.reference(design, core, trace_wire=False, timeout=140, docker_options=options)
    reference = case / 'reference'
    result = json.loads((reference / 'receipt.json').read_text())
    assert result['rc'] == result['extract_rc'] == 0, reference
    raw = design.parent.parent / 'raw-pfc' / label
    observed = read_flows(raw / 'events.trace')
    assert all(observed[x['trace_id']] == {k:v for k,v in x.items()
                 if k not in ('reference_completed','watch','stranded_pool_mib')} for x in rows)
    for filename, dag in (('messages.fct', False), ('actions.dag', True)):
        assert pfc.normalize(reference / filename, dag) == pfc.normalize(base / 'reference' / filename, dag)
    capture = raw / 'last-message-events.tsv'
    summarize(out, pool, rows, raw, capture, reference)


def summarize(out, pool, rows, raw, capture, reference):
    watched = {r['watch']: r for r in rows}
    events = {i: [] for i in watched}
    caps = []
    pause = []
    with capture.open() as stream:
        for line in stream:
            kind,*x = line.split()
            if kind == 'M':
                caps.append(tuple(map(int, x)))
                continue
            assert kind == 'E'
            ordinal, time, stage, code, node, port, *values = x
            event = [int(ordinal), int(time), stage, int(node), int(port), *map(int, values)]
            if int(code):
                events[int(code)].append(event)
            elif stage in ('wire_tx', 'wire_rx') and int(values[0]) == 254:
                pause.append(event)
    assert caps and all(events.values())
    flow_trace = (raw / 'events.trace').read_text().splitlines()
    for identity, entry in watched.items():
        ident = entry['trace_id']
        flows = [line for line in flow_trace if line.startswith(f'F {ident} ')]
        callbacks = [line for line in flow_trace if line.startswith((f'C r {ident} ', f'C s {ident} '))]
        children = []
        for line in flow_trace:
            x = line.split()
            if x[0] == 'F' and x[7] in ('r', 's') and int(x[8]) == ident:
                children.append(line)
            if x[0] == 'T' and x[3] in ('r', 's') and int(x[4]) == ident:
                children.append(line)
        entry.update(emission=flows, callbacks=callbacks, children=children,
                     event_counts=dict(Counter(row[2] for row in events[identity])))
        assert len(flows) == 1
        assert any(line.startswith('C r ') for line in callbacks) == entry['reference_completed']
    relevant = {(r[3], r[4]) for seq in events.values() for r in seq
                if r[2] in ('wire_tx', 'wire_rx', 'admit', 'mmu_drop', 'priority_bypass')}
    relevant.update((r[3], r[5]) for seq in events.values() for r in seq
                    if r[2] in ('admit', 'mmu_drop', 'priority_bypass'))
    dest = out / f'pool-{pool:02d}-evidence'
    dest.mkdir()
    save(dest / 'messages.json', list(watched.values()))
    save(dest / 'threshold-by-port.json', caps)
    save(dest / 'receipt.json', {'pool_mib': pool, 'full_witness_sha256': sha(capture), 'native_outputs_equal': True,
                                 'raw_events_sha256': sha(raw / 'events.trace'), 'returned_target_events': sum(map(len, events.values())),
                                 'threshold_calls': sum(c[2] for c in caps), 'wrap_calls': sum(c[3] for c in caps),
                                 'app_message_rows': len((reference / 'messages.fct').read_text().splitlines())})
    # Only the seven watched flows and PFC events on their route ports are returned.
    with (dest / 'selected-events.log').open('w') as stream:
        selected = [[identity, *row] for identity, seq in events.items() for row in seq]
        selected += [[0, *row] for row in pause if (row[3], row[4]) in relevant and row[1] <= 2_000_000]
        for row in sorted(selected, key=lambda row:row[1]):
            stream.write(' '.join(map(str, row))+'\n')
    print('witness', pool, 'QPs', len(rows), 'events', sum(map(len,events.values())),
          'threshold wrap calls', sum(c[3] for c in caps), flush=True)


def main():
    out = Path(sys.argv[1]).resolve()
    if len(sys.argv) > 2:
        return case_run(out, *map(int, sys.argv[2:4]))
    out.mkdir()
    freeze_watch(out)
    build(out)

    def worker(group):
        core, pools = group
        for pool in pools:
            subprocess.run([sys.executable, __file__, out, str(pool), str(core)], check=True)

    with ThreadPoolExecutor(max_workers=2) as jobs:
        list(jobs.map(worker, ((4, (24, 12, 6)), (8, (16, 8)))))


if __name__ == '__main__':
    main()
