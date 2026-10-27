// TwiNS: a GPU packet-level model of a data center (FatTree or leaf-spine) that follows ns-3's
// models (TcpSocketBase with NewReno / DCTCP / Cubic, RED with ECN, per-flow ECMP, point-to-point
// links) exactly, so that every flow's result equals sequential ns-3's.
//
// Execution model: time advances in slots of SLOT_PS (the propagation delay, i.e. the
// lookahead): a frame that starts transmission in slot s arrives at the next hop no earlier
// than slot s + 1. Within a slot every host and switch port processes its own events
// (frame arrivals, timers, flow starts) in timestamp order, so the slot structure only
// synchronises entities; it does not quantise protocol behaviour. Times are picoseconds.
#ifndef TWINS_MODEL_H
#define TWINS_MODEL_H

#include <cstddef>
#include <climits>
#include <cstdint>
#include "gpu_runtime.h"

namespace nsm
{

constexpr int64_t SLOT_PS = 1000000;    // 1 us = link propagation delay
constexpr int64_t PROP_PS = 1000000;    // link propagation delay
constexpr int64_t PS_PER_BYTE = 80;     // 100 Gbps
constexpr uint32_t MSS = 1460;
constexpr int DATA_FRAME = 1460 + 40 + 2; // payload + IPv4/TCP headers + PPP
constexpr int CTRL_FRAME = 40 + 2;        // pure ACK
constexpr int SYN_FRAME = 40 + 4 + 2;     // SYN / SYN-ACK carry an MSS option (padded)
constexpr int MAX_TX = 300;             // frames a link can start in one slot: 1 us / 3.36 ns (a 42-byte frame at 100G) < 300
constexpr int NB = 4;                   // message ring (receivers read slots s-1 and s-2)
constexpr int QDISC_CAP = 200;          // default RED MaxSize (packets); per queue: red_k, qcap
constexpr int RED_K = 30;               // default RED MinTh = MaxTh (packets), QW = 1
constexpr int MAX_ACTIVE = 256;         // active flows per host (as sender and as receiver)
constexpr int MAX_DUE = 16;             // timers due in one slot on one host
// out-of-order bitmap per receiver: FlowSoA::ooo_w[f] 32-bit words (32 segments each) beyond rcv_nxt
constexpr int64_t MIN_RTO_PS = 1000000000; // 1 ms
constexpr int64_t DELACK_PS = 10000000;    // 10 us
constexpr int64_t CONN_TIMEOUT_PS = 10000000000; // 10 ms (SYN retransmission)
constexpr int64_t CLOCK_GRAN_PS = 1000000;       // 1 us
constexpr int64_t INIT_RTT_PS = 100000000;       // RttEstimator::InitialEstimation, 100 us
constexpr uint32_t DELACK_COUNT = 2;
constexpr uint32_t INIT_CWND = 10 * MSS;
constexpr int DUPTHRESH = 3;
constexpr int CHAIN_RUNS = 9;              // recorded runs; then the root (2 words)
constexpr int CHAIN = 2 * CHAIN_RUNS + 2;
constexpr double DCTCP_G = 0.0625;

// frame kinds (flags)
constexpr uint8_t F_ACK = 1, F_SYN = 2, F_ECE = 4, F_ECT = 8, F_CE = 16, F_DATA = 32, F_CWR = 64;

// A frame: 64 bytes.
//
// chain[] records the frame's place in ns-3's event order (run-length encoded, see chain_push). ns-3 runs simultaneous events in the
// order they were scheduled, and every transmission start has one parent event: the frame's
// enqueue event (on an idle link) or the TransmitComplete of the previous frame, which itself
// was scheduled by that frame's start. chain[0] encodes the lead from the parent event to this
// one, chain[1] the parent's lead to its own parent, and so on, each code increasing with the
// lead (see chain_code). Simultaneous events are ordered by their chains (larger codes first,
// i.e. earlier parents first).
struct alignas(16) Frame
{
    int64_t ts;         // when it is ready at the queue (enqueue time) / arrives at the next hop
    uint32_t flow;      // flow id (a connection carries one flow)
    uint32_t sa;        // data: first byte; ACK: acknowledgement number
    uint16_t len;       // bytes on the wire (data carry len - 42 payload bytes)
    uint16_t dst_node;  // destination host (world-local)
    uint8_t flags;
    uint8_t to_sender;  // 1: travels to the flow's sender (ACK, SYN-ACK)
    uint16_t up;        // upward port index (flow hash % k/2), the same at every tier
    uint16_t chain[CHAIN]; // queued: the enqueue event's chain; on a link: the start's
};
static_assert(sizeof(Frame) == 64, "Frame layout");

enum : uint8_t
{
    S_IDLE = 0,
    S_SYN_SENT,
    S_EST,
    S_DONE
};
enum : uint8_t
{
    C_OPEN = 0,
    C_DISORDER,
    C_CWR,
    C_RECOVERY,
    C_LOSS
};
enum : uint8_t
{
    E_IDLE = 0,
    E_CE_RCVD,
    E_SENDING_ECE,
    E_ECE_RCVD,
    E_CWR_SENT
};

// Per-flow state, structure of arrays (index = flow id).
// X(type, name, initial value)
#define NS_FLOW_FIELDS(X)                \
    /* static */                         \
    X(int, src, 0)                       \
    X(int, dst, 0)                       \
    X(uint32_t, size, 0)                 \
    X(int64_t, start, 0)                 \
    X(uint32_t, hash_data, 0)            \
    X(uint32_t, hash_ack, 0)             \
    /* sender */                         \
    X(uint8_t, st, S_IDLE)               \
    X(uint8_t, cong, C_OPEN)             \
    X(uint32_t, una, 0)                  \
    X(uint32_t, nxt, 0)                  \
    X(uint32_t, high, 0)                 \
    X(uint32_t, cwnd, INIT_CWND)         \
    X(uint32_t, ssthresh, 0xFFFFFFFFu)   \
    X(uint32_t, dupacks, 0)              \
    X(uint32_t, sacked, 0)               \
    X(uint32_t, lost_n, 0)               \
    X(uint32_t, rtx_n, 0)                \
    X(uint8_t, head_rx, 0)               \
    X(uint32_t, recover, 0)              \
    X(uint8_t, recover_active, 0)        \
    X(uint8_t, first_partial, 1)         \
    X(uint32_t, cwnd_cnt, 0)             \
    X(uint32_t, ack_rem, 0)              /* m_bytesAckedNotProcessed */ \
    X(uint8_t, cwnd_limited, 0)          \
    /* TcpCubic (ns-3 member names in comments) */ \
    X(uint32_t, cub_cnt, 0)              /* m_cWndCnt */ \
    X(uint32_t, cub_lastmax, 0)          /* m_lastMaxCwnd */ \
    X(uint32_t, cub_origin, 0)           /* m_bicOriginPoint */ \
    X(double, cub_k, 0.0)                /* m_bicK */ \
    X(int64_t, cub_delaymin, INT64_MIN)  /* m_delayMin */ \
    X(int64_t, cub_epoch, INT64_MIN)     /* m_epochStart */ \
    X(uint8_t, cub_found, 0)             /* m_found */ \
    X(int64_t, cub_roundstart, INT64_MIN) /* m_roundStart */ \
    X(uint32_t, cub_endseq, 0)           /* m_endSeq (ns-3 sequence numbers) */ \
    X(int64_t, cub_lastack, INT64_MIN)   /* m_lastAck */ \
    X(int64_t, cub_currrtt, INT64_MIN)   /* m_currRtt */ \
    X(uint32_t, cub_samples, 0)          /* m_sampleCnt */ \
    X(uint32_t, cub_ackcnt, 0)           /* m_ackCnt */ \
    X(uint32_t, cub_tcpcwnd, 0)          /* m_tcpCwnd */ \
    X(int64_t, ecn_echo_seq, -1)         \
    X(int64_t, ecn_cwr_seq, -1)          \
    X(uint8_t, s_ce, 0)                  \
    X(uint8_t, ecn_s, E_IDLE)            \
    X(double, alpha, 1.0)                /* TcpDctcp::m_alpha (a double: float drifts) */ \
    X(uint32_t, acked_total, 0)          \
    X(uint32_t, acked_ecn, 0)            \
    X(uint32_t, next_seq, 0)             \
    X(uint8_t, next_seq_flag, 0)         \
    X(int64_t, srtt, -1)                 \
    X(int64_t, rttvar, 0)                \
    X(uint8_t, syn_tries, 0)             \
    X(int64_t, rto, MIN_RTO_PS)          \
    X(int64_t, rto_at, -1)               \
    X(uint32_t, hist_lo, 0)              \
    X(int64_t, timing_time, 0)           \
    X(int64_t, syn_at, -1)               \
    X(int64_t, est, -1)                  \
    X(int64_t, done, -1)                 \
    /* receiver */                       \
    X(uint8_t, r_up, 0)                  \
    X(uint8_t, r_est, 0)                 \
    X(uint8_t, srv_syn_n, 0)             \
    X(int64_t, srv_syn_at, -1)           \
    X(uint32_t, rcv_nxt, 0)              \
    X(uint32_t, ooo_n, 0)                \
    X(uint8_t, delack_count, 0)          \
    X(int64_t, delack_at, -1)            \
    X(uint8_t, ce_state, 0)              \
    X(uint8_t, prior_flag, 0)            \
    X(uint32_t, prior_rcv_nxt, 0)        \
    X(uint8_t, reserved, 0)              \
    X(uint8_t, ecn_r, E_IDLE)            \
    X(int64_t, ecn_ce_seq, -1)

// Flow state is stored as an array of records (one thread works through one host's flows,
// and a record keeps a flow's fields in a few cache lines); F.field[f] addresses the field of
// record f.
struct FlowRec
{
#define NS_REC(T, n, v) T n;
    NS_FLOW_FIELDS(NS_REC)
#undef NS_REC
};

template <typename T>
struct Field
{
    char *base; // address of the field in record 0
    __host__ __device__ T &operator[](size_t i) const { return *reinterpret_cast<T *>(base + i * sizeof(FlowRec)); }
};

struct FlowSoA
{
    FlowRec *rec;
#define NS_PTR(T, n, v) Field<T> n;
    NS_FLOW_FIELDS(NS_PTR)
#undef NS_PTR
    uint32_t *ooo_bm; // out-of-order segments (bit per segment, ring of ooo_w[f] words at ooo_off[f])
    uint16_t *dchain;   // [flows][CHAIN] event-order chain of the delayed-ACK timer
    uint16_t *rchain;   // [flows][CHAIN] ... of the retransmission (or SYN) timer
    int64_t *hist_t;    // first-transmission time of segment (ring of hist_n[f] entries at hist_off[f])
    uint32_t *hist_tag; // segment index + 1 of the entry, 0 = none/retransmitted
    // per-flow sizes: min(the flow's segments, a cap), so flows shorter than the cap never wrap
    uint64_t *hist_off, *ooo_off;
    uint32_t *hist_n, *ooo_w;
};

struct HostSoA
{
    Frame *ring;      // [H][ring] egress FIFO (frames not yet started)
    int *q_head, *q_size;
    int64_t *link_free, *last_start;
    uint16_t *last_chain; // [H][CHAIN]
    uint8_t *cc;          // [H] congestion control of the host's world
    int *red_k, *qcap;    // [H] the host qdisc's RED threshold and limit
    int *act_snd, *n_snd; // [H][MAX_ACTIVE], flows this host sends
    int *act_rcv, *n_rcv; // [H][MAX_ACTIVE], flows this host receives
    int *pend_next, *pend_end; // flows by (src, start): next not yet started, end of the host's range
    int *pend_flow;            // [flows] flow ids sorted by (src, start)
    int64_t *wake;             // earliest slot the host must run (no input)
};

struct PortSoA
{
    Frame *ring; // [S*k][ring]
    int *q_head, *q_size;
    int64_t *link_free, *last_start;
    uint16_t *last_chain; // [S*k][CHAIN]
    int *red_k, *qcap;    // [S*k]
};

// one enqueue, for comparing queue histories with ns-3 (twins-ref TRACE_ALLQ)
struct QRec
{
    int64_t ts;
    int32_t node, dev, src, dst; // ns-3 node/device numbering; source/destination host
    uint32_t sa;                 // relative seq (data) or ack
    uint16_t len, qlen;          // qlen: queue disc length after the enqueue (0: dropped)
    uint8_t flags, pad[3];
};

enum : uint8_t
{
    CC_NEWRENO = 0,
    CC_DCTCP = 1,
    CC_CUBIC = 2
};

enum : int
{
    TOPO_FATTREE = 0,
    TOPO_LEAFSPINE = 1
};

struct Sim
{
    int k, hk, H, S, L, F; // k: ports per switch (FatTree: its k)
    int H1, S1;            // hosts and switches of one world (worlds are disjoint copies)
    int topo;              // TOPO_FATTREE, TOPO_LEAFSPINE
    int leaves, spines, hpl; // leaf-spine: switches 0..leaves-1 are leaves, then the spines
    int ecmp_n;            // equal-cost uplinks a flow hash chooses among (FatTree k/2, spines)
    int host_blocks, host_lanes, sw_per_block; // host_lanes: hosts per block (one per lane)
    // TCP of a host (its world): CC_NEWRENO, CC_DCTCP (ECN), CC_CUBIC
    int ring; // egress FIFO capacity: the largest queue limit + the device queue + 1
    FlowSoA fl;
    HostSoA hs;
    PortSoA ps;
    int *host_in_link; // [H]
    int *sw_in_link;   // [S*k]
    Frame *msg;        // [NB][L][MAX_TX]
    long long *msg_cnt; // [NB][L] slot << 16 | frames (a count from another slot reads as 0)
    unsigned int *activity;        // [2] entities that sent frames or hold queued ones: this slot, previous
    unsigned long long *next_wake; // earliest slot a host must run (timers, flow starts)
    int64_t *slot;
    unsigned int *done_blocks;
    unsigned int *flows_done;
    int *finished, *finished_host;
    int64_t max_slots;
    int *error;
    struct QRec *qtrace;         // optional trace of every enqueue (--qtrace), qtrace_n records
    unsigned long long *qtrace_cnt;
    unsigned long long qtrace_n;
    int64_t qtrace_until, qtrace_from;
    unsigned long long *ev; // [16] counters: drops, marks, retx, rto, tx-cap hits, active overflow, ooo overflow,
                            // due overflow, retransmissions by congestion state (8 + C_*)
};

__host__ __device__ inline int64_t mi(const Sim &s, int64_t slot, int link)
{
    return (slot & (NB - 1)) * s.L + link;
}

// frames in link's message of slot `slot` (0 if that ring entry was last written for another slot)
__host__ __device__ inline int msg_count(const Sim &s, int64_t slot, int link)
{
    long long v = s.msg_cnt[mi(s, slot, link)];
    return (v >> 16) == slot ? (int)(v & 0xFFFF) : 0;
}

__host__ __device__ inline void msg_set(const Sim &s, int64_t slot, int link, int n)
{
    s.msg_cnt[mi(s, slot, link)] = (long long)slot << 16 | n;
}

} // namespace nsm

#endif
