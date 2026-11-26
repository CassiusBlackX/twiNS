#pragma once

#include "ns3/custom-header.h"
#include "ns3/packet.h"
#include "ns3/simulator.h"
#include "rdma-queue-pair.h"
#include "switch-mmu.h"
#include <cstdio>
#include <cstdlib>
#include <initializer_list>
#include <limits>
#include <map>
#include <utility>
#include <vector>

namespace ns3::pfc_witness {

struct Identity {
    uint32_t id, src, dst, sport;
};

struct Threshold {
    uint64_t calls = 0, wraps = 0;
    int64_t min_room = std::numeric_limits<int64_t>::max();
    int64_t min_at_ns = -1, first_wrap_at_ns = -1;
    uint32_t max_shared = 0, threshold_at_min = 0;
};

struct Trace {
    FILE *output = nullptr;
    uint64_t ordinal = 0;
    std::vector<Identity> watch;
    std::map<std::pair<uint32_t, uint32_t>, Threshold> thresholds;

    Trace() {
        const char *file = std::getenv("TWINS_PFC_WATCH");
        if (!file) return;
        FILE *input = std::fopen(file, "r");
        const char *path = std::getenv("TWINS_PFC_WITNESS");
        if (!input || !path || !(output = std::fopen(path, "w"))) std::abort();
        Identity id{};
        while (std::fscanf(input, "%u %u %u %u", &id.id, &id.src, &id.dst, &id.sport) == 4)
            watch.push_back(id);
        if (!std::feof(input) || watch.empty()) std::abort();
        std::fclose(input);
    }

    ~Trace() {
        if (!output) return;
        for (const auto &[key, s] : thresholds)
            std::fprintf(output, "M %u %u %llu %llu %lld %lld %lld %u %u\n", key.first, key.second,
                         (unsigned long long)s.calls, (unsigned long long)s.wraps,
                         (long long)s.min_room, (long long)s.min_at_ns, (long long)s.first_wrap_at_ns,
                         s.max_shared, s.threshold_at_min);
        std::fclose(output);
    }
};

inline Trace &trace() {
    static Trace state;
    return state;
}

inline uint32_t node(uint32_t ipv4) { return (ipv4 - 0x0b000001u) >> 8; }

inline uint32_t id(uint32_t src, uint32_t dst, uint32_t sport) {
    for (const auto &entry : trace().watch)
        if (entry.src == node(src) && entry.dst == node(dst) && entry.sport == sport)
            return entry.id;
    return 0;
}

inline uint32_t id(const CustomHeader &h) {
    if (h.l3Prot == 0x11) return id(h.sip, h.dip, h.udp.sport);
    if (h.l3Prot == 0xFC || h.l3Prot == 0xFD) return id(h.dip, h.sip, h.ack.dport);
    return 0;
}

inline uint32_t id(const RdmaQueuePair &q) { return id(q.sip.Get(), q.dip.Get(), q.sport); }

inline void event(const char *stage, uint32_t identity, uint32_t where, uint32_t port,
                  std::initializer_list<int64_t> fields) {
    auto &state = trace();
    if (!state.output) return;
    std::fprintf(state.output, "E %llu %lld %s %u %u %u", (unsigned long long)state.ordinal++,
                 (long long)Simulator::Now().GetTimeStep(), stage, identity, where, port);
    for (auto field : fields) std::fprintf(state.output, " %lld", (long long)field);
    std::fputc('\n', state.output);
}

inline void wire(const char *stage, Ptr<const Packet> packet, const CustomHeader &h, uint32_t node,
                 uint32_t port) {
    if (h.l3Prot == 0xFE) {
        event(stage, 0, node, port, {h.l3Prot, h.pfc.qIndex, h.pfc.time, (int64_t)packet->GetUid()});
    } else if (uint32_t value = id(h)) {
        event(stage, value, node, port, {h.l3Prot, int64_t(h.l3Prot == 0x11 ? h.udp.seq : h.ack.seq),
                                       (int64_t)packet->GetUid(), packet->GetSize(), h.m_tos & 3});
    }
}

inline int64_t room(const SwitchMmu &m) {
    return int64_t(m.buffer_size) - m.total_hdrm - m.total_rsrv - m.shared_used_bytes;
}

inline void admission(const char *stage, uint32_t identity, const SwitchMmu &m, uint32_t in, uint32_t out,
                      uint32_t pg, const CustomHeader &h, Ptr<const Packet> p) {
    if (!identity) return;
    const auto available = room(m);
    event(stage, identity, m.node_id, in,
          {out, pg, h.l3Prot, int64_t(h.l3Prot == 0x11 ? h.udp.seq : h.ack.seq), (int64_t)p->GetUid(), p->GetSize(),
           m.buffer_size, m.total_hdrm, m.total_rsrv, m.shared_used_bytes, available,
           uint32_t(available) >> m.pfc_a_shift[in], m.pfc_a_shift[in], m.ingress_bytes[in][pg],
           m.hdrm_bytes[in][pg], m.headroom[in],
           m.ingress_bytes[in][pg] > m.reserve ? m.ingress_bytes[in][pg] - m.reserve : 0, m.paused[in][pg],
           (int64_t)m.egress_bytes[out][pg]});
}

inline void threshold(const SwitchMmu &m, uint32_t port, uint32_t actual) {
    auto &state = trace();
    if (!state.output) return;
    const auto available = room(m);
    auto &stats = state.thresholds[{m.node_id, port}];
    ++stats.calls;
    if (available < 0) {
        if (stats.wraps++ == 0) stats.first_wrap_at_ns = Simulator::Now().GetTimeStep();
    }
    if (available < stats.min_room) {
        stats.min_room = available;
        stats.min_at_ns = Simulator::Now().GetTimeStep();
        stats.threshold_at_min = actual;
    }
    if (stats.max_shared < m.shared_used_bytes) stats.max_shared = m.shared_used_bytes;
}

inline void sender(const char *stage, Ptr<RdmaQueuePair> q, uint32_t where,
                   int64_t marker = -1, int64_t extra = -1) {
    if (uint32_t value = id(*q))
        event(stage, value, where, 0,
              {(int64_t)q->snd_nxt, (int64_t)q->snd_una, (int64_t)q->m_size, (int64_t)q->GetBytesLeft(),
               q->IsWinBound(), int64_t(q->m_nextAvail.GetTimeStep()), marker, extra});
}

} // namespace ns3::pfc_witness
