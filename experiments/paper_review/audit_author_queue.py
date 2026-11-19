"""Add observation only to ConWeave's dequeue site, without altering event scheduling."""
import argparse
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("tree", type=Path)
    parser.add_argument("objects", type=Path)
    args = parser.parse_args()
    tree = args.tree.resolve()
    model = tree / "src/point-to-point/model"
    args.objects.mkdir(parents=True, exist_ok=True)
    source = model / "qbb-net-device.cc"
    text = source.read_text()
    anchor = "        int qIndex = m_rdmaEQ->GetNextQindex(m_paused);"
    if text.count(anchor) != 1:
        raise ValueError("author source changed")
    patched = text.replace(anchor, r'''
        const int64_t auditNow = Simulator::Now().GetTimeStep();
        const auto auditHost = m_node->GetId();
        const bool audit =
            (auditHost == 32 && auditNow >= 2000640000 && auditNow <= 2000651060) ||
            (auditHost == 233 && auditNow >= 2000457000 && auditNow <= 2000468550);
        if (audit) {
            fprintf(stderr, "rr-state %lld %u %u %u %u\n", (long long)auditNow, auditHost,
                    m_rdmaEQ->m_rrlast, m_rdmaEQ->GetFlowCount(), m_rdmaEQ->m_ackQ->GetNPackets());
            for (uint32_t i = 0; i < m_rdmaEQ->GetFlowCount(); ++i) {
                const auto qp = m_rdmaEQ->GetQp(i);
                fprintf(stderr, "rr-candidate %lld %u %u %u %d %llu %llu %lld %d\n",
                    (long long)auditNow, auditHost, i,
                    (unsigned)qp->m_flow_id, (int)qp->IsFinishedConst(),
                    (unsigned long long)qp->snd_una, (unsigned long long)qp->snd_nxt,
                    (long long)qp->m_nextAvail.GetTimeStep(), (int)qp->CanIrnTransmit(m_rdmaEQ->m_mtu));
            }
        }
''' + anchor + r'''
        if (audit) {
            fprintf(stderr, "rr-selected %lld %u %d %d\n", (long long)auditNow, auditHost, qIndex,
                qIndex >= 0 ? m_rdmaEQ->GetQp((uint32_t)qIndex)->m_flow_id : -1);
        }
''')
    output = args.objects / source.name
    output.write_text(patched)
    subprocess.run(["g++", "-O3", "-fPIC", "-std=gnu++11", "-w",
        "-DHAVE_PACKET_H=1", "-DHAVE_SQLITE3=1", "-DHAVE_IF_TUN_H=1",
        f"-I{tree/'build'}", f"-I{tree}", f"-I{model}", "-c", output,
        "-o", args.objects / "qbb-net-device.o"], check=True)


if __name__ == "__main__":
    main()
