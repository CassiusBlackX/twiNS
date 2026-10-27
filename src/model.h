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
#include "rng.h"

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
constexpr int64_t MIN_RTO_PS = 1000000000; // 1 ms
constexpr int64_t DELACK_PS = 10000000;    // 10 us
constexpr int64_t CONN_TIMEOUT_PS = 10000000000; // 10 ms (SYN retransmission)
constexpr int64_t CLOCK_GRAN_PS = 1000000;       // 1 us
constexpr int64_t INIT_RTT_PS = 100000000;       // RttEstimator::InitialEstimation, 100 us
constexpr uint32_t DELACK_COUNT = 2;
constexpr uint32_t INIT_CWND = 10 * MSS;
constexpr int DUPTHRESH = 3;
#ifndef TWINS_CHAIN_RUNS
#define TWINS_CHAIN_RUNS 9 // 64-byte frames; 25 gives 128-byte frames with longer histories
#endif
constexpr int CHAIN_RUNS = TWINS_CHAIN_RUNS; // recorded runs; then the root (2 words)
constexpr int CHAIN = 2 * CHAIN_RUNS + 2;
constexpr double DCTCP_G = 0.0625;

// frame kinds (flags)
constexpr uint8_t F_ACK = 1, F_SYN = 2, F_ECE = 4, F_ECT = 8, F_CE = 16, F_DATA = 32, F_CWR = 64, F_FIN = 128;
// TcpTxItem flags
constexpr uint8_t IT_SACKED = 1, IT_LOST = 2, IT_RETX = 4;
// closing the sending side (FlowRec::fin): Close() called (m_closeOnEmpty), FIN sent
// (FIN_WAIT_1), FIN acknowledged (FIN_WAIT_2), the peer's FIN received (TIME_WAIT)
constexpr uint8_t FIN_CLOSE = 1, FIN_SENT = 2, FIN_ACKED = 4, FIN_TW = 8;
// the receiving socket (FlowRec::r_st): the PacketSink's sockets have ShutdownSend set, so the
// peer's FIN closes them at once: CLOSE_WAIT -> LAST_ACK, then CLOSED at the last ACK
enum : uint8_t
{
    R_OPEN = 0,
    R_LAST_ACK,
    R_CLOSED
};
constexpr int DATA_RETRIES = 6; // TcpSocket::DataRetries

// A frame: 64 bytes (128 with TWINS_CHAIN_RUNS=25).
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
static_assert(sizeof(Frame) == 24 + 2 * CHAIN && sizeof(Frame) % 16 == 0, "Frame layout");

enum : uint8_t
{
    S_IDLE = 0,
    S_SYN_SENT,
    S_EST,  // ESTABLISHED and the closing states
    S_DONE  // every message acknowledged
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

// Per-connection state, structure of arrays (index = connection id; a connection carries one
// or more flows, its messages). Sequence numbers are relative to the first data byte.
// X(type, name, initial value)
#define NS_FLOW_FIELDS(X)                \
    /* static */                         \
    X(int, src, 0)                       \
    X(int, dst, 0)                       \
    X(uint32_t, hash_data, 0)            \
    X(uint32_t, hash_ack, 0)             \
    X(int, m_first, 0)                   /* its messages: m_first .. m_first + m_cnt - 1 */ \
    X(int, m_cnt, 0)                     \
    X(uint8_t, fin_on, 0)                /* the application closes after its last message */ \
    /* application */                    \
    X(int, m_due, 0)                     /* messages whose start has come */ \
    X(int, m_wr, 0)                      /* messages written to the socket */ \
    X(int, m_dn, 0)                      /* messages acknowledged */ \
    /* sender */                         \
    X(uint8_t, st, S_IDLE)               \
    X(uint8_t, cong, C_OPEN)             \
    X(uint8_t, fin, 0)                   \
    X(uint32_t, tail, 0)                 /* TcpTxBuffer::TailSequence: bytes written */ \
    X(uint32_t, una, 0)                  /* SND.UNA (TcpTxBuffer::HeadSequence) */ \
    X(uint32_t, nxt, 0)                  \
    X(uint32_t, high, 0)                 /* m_highTxMark, the end of the sent list */ \
    X(uint32_t, cwnd, INIT_CWND)         \
    X(uint32_t, ssthresh, 0xFFFFFFFFu)   \
    X(uint32_t, dupacks, 0)              \
    /* TcpTxBuffer: the sent list (items it_head.., it_n of them), byte counters, cursor */ \
    X(uint32_t, it_head, 0)              \
    X(uint32_t, it_n, 0)                 \
    X(uint32_t, sack_b, 0)               /* m_sackedOut */ \
    X(uint32_t, lost_b, 0)               /* m_lostOut */ \
    X(uint32_t, rtx_b, 0)                /* m_retrans */ \
    X(uint32_t, sacked_n, 0)             /* sacked items: Reno emulation sacks items 1, 2, .. */ \
    X(uint32_t, cand_n, 0)               /* items lost, not retransmitted, not sacked */ \
    X(uint32_t, cand, 0)                 /* no such item below this index */ \
    X(uint32_t, hisack, 0)               /* m_highestSack.second */ \
    X(uint8_t, sack_seen, 0)             /* m_sackSeen */ \
    X(uint32_t, h_head, 0)               /* RTT history (m_history): entries h_head.., h_n */ \
    X(uint32_t, h_n, 0)                  \
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
    X(int64_t, syn_at, -1)               /* SYN timer, or a pending SendPendingData (Send's TimeStep) */ \
    /* receiver */                       \
    X(uint8_t, r_up, 0)                  \
    X(uint8_t, r_est, 0)                 \
    X(uint8_t, r_st, R_OPEN)             \
    X(int64_t, r_fin, -1)                /* TcpRxBuffer m_finSeq (-1: no FIN yet) */ \
    X(int64_t, r_synack_t, -1)           /* the first SYN-ACK (its RTT history entry) */ \
    X(int64_t, r_srtt, -1)               /* the receiving socket's RTT estimator */ \
    X(int64_t, r_rttvar, 0)              \
    X(int64_t, r_last_at, -1)            /* LastAckTimeout */ \
    X(uint8_t, r_last_n, 0)              /* its retries left (m_dataRetrCount) */ \
    X(int64_t, r_frx_at, -1)             /* the FIN+ACK's own retransmission (SendEmptyPacket) */ \
    X(uint8_t, srv_syn_n, 0)             \
    X(int64_t, srv_syn_at, -1)           \
    X(uint32_t, rcv_nxt, 0)              \
    X(uint32_t, ooo_n, 0)                /* out-of-order blocks (a ring from ooo_h) */ \
    X(uint32_t, ooo_h, 0)                \
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
    uint16_t *dchain;   // [conns][CHAIN] event-order chain of the delayed-ACK timer
    uint16_t *rchain;   // [conns][CHAIN] ... of the retransmission (or SYN) timer
    uint16_t *schain;   // [conns][CHAIN] ... of a pending SendPendingData (Send's TimeStep)
    // per-connection rings of cap[f] entries at off[f]: the sent list (item start, flags) and the
    // RTT history (seq, count with the retransmitted flag in bit 31, time)
    uint64_t *it_off;
    uint32_t *it_cap;
    uint32_t *it_start;
    uint8_t *it_fl;
    uint32_t *h_seq, *h_cnt;
    int64_t *h_t;
    // out-of-order blocks of the receiver, [start, end), sorted (oo_cap[f] at oo_off[f])
    uint64_t *oo_off;
    uint32_t *oo_cap;
    uint32_t *oo_s, *oo_e;
};

// Flows as the application sees them: messages written to a connection at their start times.
struct MsgSoA
{
    int *conn;
    uint32_t *size, *end; // end: offset of its last byte + 1 in the connection's byte stream
    int64_t *start;       // ps
    int64_t *est, *done;  // handed to the socket; its last byte acknowledged (ps, -1: not yet)
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
    int *pend_next, *pend_end; // messages by (src, start): next not yet started, end of the host's range
    int *pend_msg;             // [messages] message ids sorted by (src, start, id)
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
    TOPO_LEAFSPINE = 1,
    TOPO_GRAPH = 2 // a topology file: routes from tables
};

struct Sim
{
    int k, hk, H, S, L, F; // k: ports per switch (FatTree: its k); F: connections
    int M;                 // messages (flows)
    int H1, S1;            // hosts and switches of one world (worlds are disjoint copies)
    int topo;              // TOPO_FATTREE, TOPO_LEAFSPINE
    int leaves, spines, hpl; // leaf-spine: switches 0..leaves-1 are leaves, then the spines
    int ecmp_n;            // equal-cost uplinks a flow hash chooses among (FatTree k/2, spines)
    // TOPO_GRAPH (world-local ids): a host's switch and port; for switch x and destination
    // switch e, the equal-cost ports (in interface order) rt_ports[rt_off[x S1 + e] ..], rt_n of them
    int *host_sw;
    uint8_t *host_port;
    uint32_t *rt_off;
    uint8_t *rt_n, *rt_ports;
    // link failures (TOPO_GRAPH): epoch i starts at fail_t[i - 1] (epoch 0 at the start); the
    // tables above are per epoch ([epoch][S1 S1]); port_dn[epoch][S1 k]: the port's interface is down
    int n_fail;
    int64_t *fail_t;
    uint8_t *port_dn;
    int host_blocks, host_lanes, sw_per_block; // host_lanes: hosts per block (one per lane)
    // TCP of a host (its world): CC_NEWRENO, CC_DCTCP (ECN), CC_CUBIC
    int ring; // egress FIFO capacity: the largest queue limit + the device queue + 1
    FlowSoA fl;
    MsgSoA mg;
    int *mg_first_of_world; // [worlds] the first message of each world (message keys are world-local)
    HostSoA hs;
    PortSoA ps;
    nsm_rng::State *sw_rng; // [S] per-packet ECMP: each switch's random variable (null: per-flow ECMP)
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
    unsigned long long *ties_w; // [worlds] undecided ties per world
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
