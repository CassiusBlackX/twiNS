// TwiNS-RDMA: a GPU model of the HPCC simulator's RDMA network (github.com/alibaba-edu/
// High-Precision-Congestion-Control, ns-3.17/18: QbbNetDevice, RdmaHw, SwitchNode, SwitchMmu) that
// follows it exactly, so that every flow's completion time equals the reference's
// (validation/hpcc/rdma-ref.cc).
//
// The network: hosts with one RDMA NIC each (queue pairs paced by rate, a window, go-back-N with
// NACKs, ACKs in a high-priority queue), switches with a shared-buffer MMU (PFC with dynamic
// thresholds and headroom, ECN marking at dequeue), PFC pause/resume, and congestion control
// DCQCN (Mellanox's version, CC_MODE 1) or HPCC (INT, CC_MODE 3). All links run at one rate and
// delay. Times are nanoseconds (ns-3.17's resolution).
//
// Execution model (as TwiNS's TCP model): slots of the link delay D, the lookahead. A frame that
// starts in slot s arrives no earlier than slot s + 1. In a slot each host and each switch
// processes its own events in ns-3's order: by time, and simultaneous events by their chains (see
// Chain). A switch is processed by one thread, all its ports together, since its MMU couples them.
#ifndef TWINS_RDMA_H
#define TWINS_RDMA_H

#include <cstdint>
#include "gpu_runtime.h"
#include "rng.h"

namespace rdma
{

#ifndef TWINS_RDMA_RUNS
#define TWINS_RDMA_RUNS 12
#endif
constexpr int RUNS = TWINS_RDMA_RUNS; // chain runs kept
constexpr int MAXF = 256;    // frames a link can start in one slot
constexpr int NB = 4;        // message ring: receivers read slots s - 1 and s - 2
constexpr int MAX_HOP = 5;   // IntHeader::maxHop
constexpr int PG = 3;        // the data priority (flows' pg), RdmaEgressQueue::ack_q_idx
constexpr uint32_t RESERVE = 4 * 1024, RESUME_OFFSET = 3 * 1024; // SwitchMmu

// frame kinds
enum : uint8_t
{
    K_DATA = 0,
    K_ACK,
    K_NACK,
    K_PAUSE,
    K_RESUME
};

// An event's place in ns-3's event order. ns-3 runs simultaneous events in the order they were
// scheduled. An event's chain is the sequence of leads from it back to its ancestors (run-length
// encoded): code = (lead + 1) * 8 + 7 - ordinal, where the ordinal counts the events its parent
// had scheduled before it. Two simultaneous events compare by their first differing lead, the
// larger first (an earlier ancestor); if all leads are equal, by the ordinals at the deepest level
// where they differ (siblings in the order they were scheduled; see ccmp). A chain ends at a
// flow's StartApplication (see the host), which records its own lineage and a root key, the
// flow's place in the start order. When the runs overflow, the oldest spills into a TailNode.
struct Run
{
    uint32_t code, cnt;
};
struct Chain
{
    Run r[RUNS];
    uint64_t tail;  // the runs after r[RUNS - 1]: a TailNode (its position + 1; 0: none)
    uint32_t root;  // 1 + the flow's index in start order (0: none)
    uint32_t pad;
};
static_assert(sizeof(Chain) == 8 * RUNS + 16, "Chain");

// The older runs of long chains, in a ring shared by all chains: a node is immutable, and pos
// (its position + 1) tells whether the ring has since overwritten it.
struct TailNode
{
    uint32_t code, cnt;
    uint64_t next, pos;
};

// A frame's contents. The INT hops are IntHop's 64-bit words (lineRate:3 time:24 bytes:20
// qlen:17, from the low bits).
struct Pkt
{
    uint64_t hop[MAX_HOP];
    uint32_t flow;
    uint32_t seq;   // data: first byte; ACK/NACK: the receiver's next expected byte
    uint16_t size;  // bytes on the wire
    uint16_t nhop;
    uint8_t kind;
    uint8_t ecn;    // the IP header's ECN bits are CE (3), else 0
    uint8_t cnp;    // ACK/NACK: the CNP flag
    uint8_t q;      // PFC: the queue index
    uint8_t inport; // queued at a switch: the ingress port (FlowIdTag)
    uint8_t ord;    // on a link: the arrival's ordinal in the event that started it
    uint16_t sport; // the flow's source port, source and destination hosts (data and ACKs)
    uint16_t src, dst;
};
static_assert(sizeof(Pkt) == 64, "Pkt");

// A frame on a link: its arrival time, its contents, and the chain of the event that started it.
struct LFrame
{
    int64_t ts;
    Chain c;
    Pkt p;
};
static_assert(sizeof(LFrame) % 8 == 0, "LFrame");

// A queue pair (sender side), RdmaQueuePair
struct SndQP
{
    uint64_t size, snd_nxt, snd_una;
    uint64_t rate, max_rate; // bps
    int64_t next_avail;
    uint32_t last_pkt;       // lastPktSize
    uint32_t win;
    uint64_t base_rtt;
    uint8_t done;            // QpComplete ran (the QP left m_qpMap)
    // DCQCN, Mellanox's version (mlx)
    uint8_t alpha_cnp, dec_cnp, first_cnp;
    uint32_t rp_stage;
    double alpha;
    uint64_t target;
    int64_t t_alpha, t_dec, t_rp; // pending timers (-1: none)
    // HPCC (hp)
    uint32_t last_update_seq;
    int32_t inc_stage;
    uint64_t cur_rate;
    double u;
    uint64_t hop[MAX_HOP];
};

struct QPChains
{
    Chain alpha, dec, rp;
};

// the receiving side, RdmaRxQueuePair
struct RcvQP
{
    uint32_t expected;  // ReceiverNextExpectedSeq
    uint32_t last_nack; // m_lastNACK
    int64_t nack_timer; // m_nackTimer
};

// a switch's equal-cost ports (in table order) towards a host
constexpr int RT_PORTS = 15;
struct Route
{
    uint8_t n;
    uint8_t port[RT_PORTS];
};

struct FlowInfo
{
    int src, dst;
    uint64_t size;
    int64_t start;      // ns
    uint32_t sport;     // dport is 100
    uint32_t order;     // index in start order
    Chain start_chain;  // the chain of its StartApplication
};

struct Host
{
    int64_t tc_at;      // pending TransmitComplete (-1: the device is READY)
    int64_t ns_at;      // pending m_nextSend (-1: expired)
    uint32_t paused;    // m_paused[] as a bit mask
    int ackq_head, ackq_n;
    int qp_n, rr;       // RdmaQueuePairGroup: qp_n entries; m_rrlast
    int next_start;     // next of its flows (in start order) to start
    int start_end;
    int64_t wake;       // the earliest time it must run without input (for idle skipping)
};

struct Port
{
    int64_t tc_at;
    uint32_t paused;      // m_paused[] (set by PFC frames from the peer)
    int q0_head, q0_n;    // queue 0 (PFC frames; ACKs with ACK_HIGH_PRIO)
    int q3_head, q3_n;    // queue PG (data and ACKs)
    uint32_t bytes_total; // m_bytesInQueueTotal
    uint64_t tx_bytes;    // SwitchNode::m_txBytes
    // SwitchMmu, priority PG, this port as ingress (ing, hdrm, mmu_paused) and egress (egr)
    uint32_t ing, hdrm, egr;
    uint32_t mmu_paused;
    nsm_rng::State rng;   // its ECN marking stream
};

struct Switch
{
    uint32_t shared_used; // SwitchMmu::shared_used_bytes
    uint32_t total_hdrm, total_rsrv;
    uint32_t headroom;    // per port (uniform links)
};

struct Params
{
    int cc;               // CC_MODE: 1 DCQCN, 3 HPCC
    int int_size;         // IntHeader::GetStaticSize: 42 (HPCC) or 0
    uint32_t mtu;         // PACKET_PAYLOAD_SIZE
    uint32_t ack_interval; // L2_ACK_INTERVAL
    uint32_t chunk;       // L2_CHUNK_SIZE
    int64_t nack_interval; // ns
    uint64_t bps;         // link rate
    int64_t delay;        // link delay, ns
    uint32_t win;         // maxBdp (HAS_WIN, GLOBAL_T)
    uint64_t base_rtt;    // maxRtt
    int var_win, rate_bound, ack_high_prio, ecn, qbb;
    // DCQCN
    double g;             // EWMA_GAIN
    double rate_first_cnp;
    int clamp;            // CLAMP_TARGET_RATE
    int64_t alpha_int, dec_int, rp_int; // ns
    uint32_t rpg_threshold; // FAST_RECOVERY_TIMES
    uint64_t rai, rhai, min_rate;
    // HPCC
    double u_target;
    uint32_t mi_thresh;
    int fast_react, multi_rate, sample_feedback;
    uint32_t int_multi;
    // switch
    uint32_t buffer_size;
    uint32_t pfc_shift;
    uint32_t kmin, kmax;  // bytes
    double pmax;
};

struct Sim
{
    int H, S, N, L, K; // hosts, switches, nodes (hosts first), directed links, max ports per node
    int F;             // flows
    Params P;
    // topology: node n's port p (device p + 1) sends on link out_link[n K + p], receives on
    // in_link[n K + p]; a link's receiving node and port
    int *deg, *out_link, *in_link, *link_dst, *link_dport;
    // routes: switch x towards host h, rt[x H + h]
    struct Route *rt;
    FlowInfo *fi;
    SndQP *qp;
    QPChains *qc;
    RcvQP *rx;
    int *start_list;   // flows by (src, start order)
    // hosts
    Host *hs;
    Chain *h_tc, *h_ns; // [H] chains of the pending TransmitComplete and m_nextSend
    Pkt *ackq;          // [H][ackq_cap]
    int ackq_cap;
    int *qps;           // [H][qps_cap] RdmaQueuePairGroup::m_qps
    int qps_cap;
    // switches
    Switch *sw;
    Port *ps;           // [S K]
    Chain *p_tc;        // [S K]
    Pkt *q0, *q3;       // [S K][q0_cap], [S K][q3_cap]
    int q0_cap, q3_cap;
    // links
    LFrame *msg;        // [NB][L][MAXF]
    long long *msg_cnt; // [NB][L] slot << 16 | frames
    // run control
    int64_t *slot;
    unsigned int *done_blocks;
    unsigned int *flows_done;
    unsigned int *activity; // [2] entities that sent or hold frames: this slot, the previous
    unsigned long long *next_wake;
    int *finished;
    int64_t max_slots;
    int host_blocks, spread; // spread: lanes per entity (see rdma_slot)
    int64_t *fct;       // [F] completion time - start (ns), -1: not yet
    // tracing: every transmission start in [tx_from, tx_until]
    struct TxRec *tx;
    unsigned long long *tx_cnt, tx_cap;
    int64_t tx_from, tx_until;
    unsigned long long *ev; // counters (see main)
};

struct TailPool
{
    TailNode *n;
    unsigned long long *cnt;
    uint64_t mask; // capacity - 1 (a power of two)
};

// one transmission start (rdma-ref --txtrace)
struct TxRec
{
    int64_t ts;
    int32_t node, dev;
    uint32_t a, b, sport, seq; // src/dst host (PFC: queue, 0)
    uint16_t size;
    uint8_t kind, ecn;
};

__host__ __device__ inline int64_t mi(const Sim &s, int64_t slot, int link) { return (slot & (NB - 1)) * s.L + link; }

__host__ __device__ inline int msg_count(const Sim &s, int64_t slot, int link)
{
    long long v = s.msg_cnt[mi(s, slot, link)];
    return (v >> 16) == slot ? (int)(v & 0xFFFF) : 0;
}

} // namespace rdma

#endif
