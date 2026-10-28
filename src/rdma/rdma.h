// TwiNS-RDMA: a GPU model of the HPCC simulator's RDMA network (github.com/alibaba-edu/
// High-Precision-Congestion-Control, ns-3.17/18: QbbNetDevice, RdmaHw, SwitchNode, SwitchMmu) that
// follows it exactly, so that every flow's completion time equals the reference's
// (validation/hpcc/rdma-ref.cc).
//
// The network: hosts with one RDMA NIC each (queue pairs paced by rate, a window, go-back-N with
// NACKs, ACKs in a high-priority queue), switches with a shared-buffer MMU (PFC with dynamic
// thresholds and headroom, ECN marking at dequeue), PFC pause/resume, and congestion control
// DCQCN (Mellanox's version, CC_MODE 1) or HPCC (INT, CC_MODE 3). Links have their own rates and
// delays. Times are nanoseconds (ns-3's resolution).
//
// Execution model (as TwiNS's TCP model): slots of the shortest link delay D, the lookahead. A frame
// that starts in slot s arrives no earlier than slot s + 1; its link files it under the slot it
// arrives in (see LinkInfo). In a slot each host and each switch
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
#define TWINS_RDMA_RUNS 6
#endif
constexpr int RUNS = TWINS_RDMA_RUNS; // chain runs kept
constexpr int MAXF = 256;    // frames a link can deliver in one slot, at most
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
// hash: of the leads pushed since the root's StartApplication; anc: the hashes of its ancestors
// NANC levels down (see ccmp: comparisons decided by the leads are remembered by these hashes)
struct Run
{
    uint32_t code, cnt;
};
constexpr int NANC = 4;
struct Chain
{
    Run r[RUNS];
    uint64_t tail;  // the runs after r[RUNS - 1]: a TailNode (its position + 1; 0: none)
    uint64_t hash, anc[NANC];
    uint32_t root;  // 1 + the flow's index in start order, over all worlds (0: none)
    uint32_t pad;
};
static_assert(sizeof(Chain) == 8 * RUNS + 24 + 8 * NANC, "Chain");

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
    uint8_t nic;             // its device (GetNicIdxOfQp)
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
    // the last tx time computed for this QP: tx_ns(tx_rate, tx_size) = tx_val (a division saved)
    uint64_t tx_rate;
    uint32_t tx_size;
    int64_t tx_val;
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
// (ports s.rtp[off] .. s.rtp[off + n - 1])
struct Route
{
    uint32_t off;
    uint32_t n;
};

struct FlowInfo
{
    int src, dst;
    uint64_t size;
    int64_t start;      // ns
    uint32_t sport;     // dport is 100
    uint32_t order;     // index in start order
    uint32_t win;       // its window and base RTT (maxBdp and maxRtt, or the pair's with GLOBAL_T 0)
    uint64_t base_rtt;
    uint8_t snic, rnic; // its sender's device (GetNicIdxOfQp), its receiver's for ACKs (GetNicIdxOfRxQp)
    uint8_t dyn;        // DAG: it starts when its dependencies have fired (start, sport, devices then)
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
    uint64_t tc_key;      // wide switches: its TransmitComplete's place (see Switch::evseq)
};

struct Switch
{
    uint32_t shared_used; // SwitchMmu::shared_used_bytes
    uint32_t total_hdrm, total_rsrv;
    uint32_t max_shift;   // the largest pfc_a_shift of its ports (the lowest PFC threshold)
    // wide switches (no safe slots: every event in order) number their events; a TransmitComplete's
    // key is its event's number and its ordinal there, and simultaneous TransmitCompletes run in key
    // order (their chains differ first at the scheduling events, whose order is that number's)
    uint64_t evseq;
};

// a switch port's configuration: its link's rate, SwitchMmu's headroom, pfc_a_shift and ECN
// thresholds (by the rate: KMIN_MAP, KMAX_MAP, PMAX_MAP)
struct PortCfg
{
    uint64_t bps;
    uint32_t hdrm_cap, shift;
    uint32_t kmin, kmax;  // bytes
    double pmax;
};

// what a switch port's slot needs of its static setup, in one load: its in link and that link's
// ring (see LinkInfo), the delays of both its links, its ECN thresholds
struct PortStatic
{
    int32_t link_in;
    int32_t in_delay, out_delay;
    uint32_t kmin, kmax;
    uint32_t in_coff, in_moff;
    uint16_t in_nb, in_cap;
};

// A directed link. Its frames are filed by the slot they arrive in: bucket a % nb of its ring
// holds those arriving in slot a (s.msg[moff + bucket * cap ..], s.msg_cnt[coff + bucket] = a << 16
// | frames). Its sender writes bucket a during slots before a, its receiver reads it in slot a; nb
// exceeds the most slots a frame can take, so a bucket is never written while it is read.
struct LinkInfo
{
    int64_t delay;  // ns
    uint64_t bps;
    uint64_t moff;
    uint32_t coff;
    uint16_t nb, cap;
    uint32_t rc;    // its rate's tx table
};

struct Params
{
    int cc;               // CC_MODE: 1 DCQCN, 3 HPCC
    int int_size;         // IntHeader::GetStaticSize: 42 (HPCC) or 0
    uint32_t mtu;         // PACKET_PAYLOAD_SIZE
    uint32_t ack_interval; // L2_ACK_INTERVAL
    uint32_t chunk;       // L2_CHUNK_SIZE
    int64_t nack_interval; // ns
    uint64_t bps;         // the first link's rate (uniform links: every link's)
    int64_t delay;        // the slot: the shortest link delay of all worlds, ns
    uint32_t win;         // maxBdp (HAS_WIN, GLOBAL_T)
    uint64_t base_rtt;    // maxRtt
    // SimAI's ns-3-alibabacloud (ns-3.36) instead of the HPCC fork: 64-bit sequence numbers (4 more
    // header bytes in data and ACK frames), tx times floor(bytes * 8e9 / bps), and the host's wake-up
    // also counting queue pairs that have sent everything
    int simai;
    uint32_t seq_extra;   // 4 with simai
    uint32_t ecn_pcnt;    // SwitchMmu::pCnt (ECN stream ids): 257, SimAI 1025
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

// a switch's place in its world: the hash seed (its node id in the world), its world's first
// host, its routes (rt_off: towards its world's host 0)
struct SwInfo
{
    uint32_t seed;
    int hbase;
    uint32_t rt_off;
    uint32_t nvs; // an NVSwitch (NVSwitchNode: no PFC, ECN or INT)
};

// Worlds are independent networks run side by side (a batch): their hosts come first, then their
// switches, links and flows, in world order. They share the slot (P.delay, the shortest link delay)
// and the tx rule (P.simai); everything else is per world (Pw) or per link and port.
struct Sim
{
    int H, S, N, L, K; // hosts, switches, nodes (hosts first), directed links, max ports per node
    int F;             // flows
    Params P;          // world 0's (its rate and delay are every world's)
    int W;             // worlds
    const Params *Pw;  // [W]
    const int *h_world, *s_world; // [H], [S]
    const SwInfo *si;  // [S]
    // topology: node n's port p (device p + 1) sends on link out_link[n K + p], receives on
    // in_link[n K + p]; a link's receiving node and port
    int *deg, *out_link, *in_link, *link_dst, *link_dport;
    // routes: switch x towards host h of its world, rt[si[x].rt_off + h]
    struct Route *rt;
    const uint8_t *rtp;
    FlowInfo *fi;
    const Chain *root_chain; // [F] the start chain of root r at r - 1
    const uint32_t *root_rank; // [F] root r's place in the order of the start chains (ccmp_walk's)
    SndQP *qp;
    QPChains *qc;
    RcvQP *rx;
    int *start_list;   // flows by (src, start order)
    // hosts: per NIC (host h's devices are NICs nic_off[h] ..). Hosts with one device run in
    // rdma_slot, those with more in rdma_slot_m (a kernel of its own: its larger per-lane state
    // would hold back every block)
    const int *nic_off; // [H + 1]
    const int *hlist1, *hlistm; // the hosts of each kind
    int n1, nm;
    // switches with at most KSMALL ports run in rdma_slot, the others in rdma_slot_big; ks, kb:
    // the ports each kernel's LDS has room for
    const int *slist_s, *slist_b;
    int ns_s, ns_b, ks, kb;
    Host *hs;
    Chain *h_tc, *h_ns; // [NICs] chains of the pending TransmitComplete and m_nextSend
    Pkt *ackq;          // [NICs][ackq_cap]
    int ackq_cap;
    int *qps;           // [NICs][qps_cap] RdmaQueuePairGroup::m_qps
    int qps_cap;
    // switches
    Switch *sw;
    const PortCfg *pc;  // [ports]
    const struct PortStatic *pst; // [ports]
    int *sw_busy;       // [S] it ended its last slot with frames queued or in transmission
    // switch x's ports are pbase[x] .. pbase[x] + deg - 1 of the per-port arrays
    const int *pbase;
    Port *ps;           // [ports]
    Chain *p_tc;        // [ports]
    Pkt *q0, *q3;       // [ports][q0_cap], [ports][q3_cap]
    struct ArrLds *apool; // [S][POOL] a switch's arrivals of the slot
    int *aidx;          // [S][2 POOL] their order per egress port (safe slots)
    int q0_cap, q3_cap;
    // links
    const LinkInfo *li; // [L]
    LFrame *msg;        // per link [nb][cap]
    long long *msg_cnt; // per link [nb]: slot << 16 | frames
    const int32_t *txt; // [R][TXT_N] tx times by rate class (c_txt holds class 0)
    int R;
    unsigned int *pend; // [pend_n] a frame arrives in slot a: pend[a % pend_n] != 0 (idle skipping)
    int pend_n;
    // run control
    int64_t *slot;
    unsigned int *done_blocks;
    unsigned int *flows_done;
    unsigned int *activity; // [2] entities that sent or hold frames: this slot, the previous
    unsigned long long *next_wake;
    int *finished;
    int64_t max_slots;
    int host_blocks, spread; // spread: lanes per entity (see rdma_slot)
    int serial;              // switches run every slot's events on one lane (no safe slots)
    int64_t *fct;       // [F] completion time - start (ns), -1: not yet
    int64_t *sent;      // [F] simai: its first SendComplete (ns), -1: none yet
    // DAG: children of flow g at ch_list[ch_off[g] ..] (child << 1 | kind: 0 received, 1 sent);
    // per flow its dependencies left and the latest StartApplication they gave; messages filed by
    // arrival slot (dmsg[b][..], dmsg_n[b]); a host's ready flows (dyn[h][..]); a world's host
    // pairs' devices and port counters (at wp_off[w] + src * H_w + dst, numbered in the world)
    int dag;
    int64_t send_lat;
    const int *ch_off;
    const uint32_t *ch_list;
    int *dep_left;
    int64_t *dep_t;
    Chain *dep_c;
    struct DMsg *dmsg;
    unsigned int *hmsg_n; // [H][nbi] the messages for a host's flows in each bucket
    unsigned int *dmsg_n;
    int nbi, dcap;
    int *dyn, *dyn_n;
    int dyn_cap;
    const struct PairNic *pnic;
    uint16_t *pcnt;
    const int *wh_base, *wh_n;
    const int64_t *wp_off;
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

// DAG (docs/ai/dag.md): a fired dependency of a flow, for its source host: the StartApplication it
// would give (time, chain)
struct DMsg
{
    int64_t ts;
    Chain c;
    uint32_t flow;
};
// a host pair's devices: towards b, those a queue pair of a picks from (GetNicIdxOfQp), and those
// a's receiving queue pair from b picks from (GetNicIdxOfRxQp)
struct PairNic
{
    uint8_t sn, s[3], rn, r[3];
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

// a link's frames arriving in a slot: their count, and the index of the first in s.msg
__host__ __device__ inline int bucket(const Sim &s, int link, int64_t slot, uint32_t *base)
{
    const LinkInfo &l = s.li[link];
    int b = (int)(slot % l.nb);
    long long v = s.msg_cnt[l.coff + b];
    *base = (uint32_t)(l.moff + (uint64_t)b * l.cap);
    return (v >> 16) == slot ? (int)(v & 0xFFFF) : 0;
}

} // namespace rdma

#endif
