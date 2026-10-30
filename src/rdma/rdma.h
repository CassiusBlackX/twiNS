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

// Unsigned 64-bit division by a divisor fixed at setup (libdivide's round-up method, exact for
// every numerator): one multiply-high instead of the GPU's division routine (about 100
// instructions, and ~370 cycles, at every use)
struct FDiv
{
    uint64_t m;      // the magic number (0: d is a power of two)
    uint64_t d;
    uint32_t sh, add;
};
inline FDiv fdiv_make(uint64_t d)
{
    FDiv f = {0, d, 0, 0};
    int l = 63 - __builtin_clzll(d);
    if ((d & (d - 1)) == 0)
    {
        f.sh = l;
        return f;
    }
    unsigned __int128 num = (unsigned __int128)1 << (64 + l);
    uint64_t pm = (uint64_t)(num / d), rem = (uint64_t)(num % d);
    if (d - rem < (1ull << l))
        f.sh = l;
    else
    {
        pm += pm;
        uint64_t tr = rem + rem;
        if (tr >= d || tr < rem)
            pm += 1;
        f.sh = l;
        f.add = 1;
    }
    f.m = pm + 1;
    return f;
}
__host__ __device__ __forceinline__ uint64_t fdiv(uint64_t m, uint32_t sh, uint32_t add, uint64_t n)
{
#if defined(__HIP_DEVICE_COMPILE__) || defined(__CUDA_ARCH__)
    uint64_t q = m ? __umul64hi(n, m) : n;
#else
    uint64_t q = m ? (uint64_t)(((unsigned __int128)n * m) >> 64) : n;
#endif
    if (add)
        q = ((n - q) >> 1) + q;
    return q >> sh;
}
__host__ __device__ __forceinline__ uint64_t fdiv(const FDiv &f, uint64_t n) { return fdiv(f.m, f.sh, f.add, n); }
__host__ __device__ __forceinline__ uint64_t fmod(const FDiv &f, uint64_t n) { return n - fdiv(f, n) * f.d; }
// n / d for a divisor known only at run time: a double estimate, then exact integer steps (one or
// two for a divisor of 2^11 or more, as rates are)
__host__ __device__ __forceinline__ uint64_t udiv64(uint64_t n, uint64_t d)
{
    double e = (double)n / (double)d;
    uint64_t q = e >= 18446744073709551615.0 ? ~0ull : (uint64_t)e;
    // r = n - q d: the steps while it is negative or at least d (as a 128-bit value: q d may wrap)
#if defined(__HIP_DEVICE_COMPILE__) || defined(__CUDA_ARCH__)
    uint64_t hi = __umul64hi(q, d);
#else
    uint64_t hi = (uint64_t)(((unsigned __int128)q * d) >> 64);
#endif
    uint64_t lo = q * d;
    while (hi || lo > n) // q d > n
    {
        q--;
        hi -= lo < d;
        lo -= d;
    }
    while (n - lo >= d)
    {
        q++;
        lo += d;
    }
    return q;
}

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
// encoded): code = (lead + 1) << CODE_ORD_BITS | CODE_ORD_MAX - ordinal, where the ordinal counts
// the events its parent had scheduled before it (a tick of astra-sim's can start many flows; larger
// ordinals tie, which is counted as undecided). Two simultaneous events compare by their first differing lead, the
// larger first (an earlier ancestor); if all leads are equal, by the ordinals at the deepest level
// where they differ (siblings in the order they were scheduled; see ccmp). A chain ends at a
// flow's StartApplication (see the host), which records its own lineage and a root key, the
// flow's place in the start order. When the runs overflow, the oldest spills into a TailNode.
// hash: of the leads pushed since the root's StartApplication; anc: the hashes of its ancestors
// NANC levels down (see ccmp: comparisons decided by the leads are remembered by these hashes)
constexpr int CODE_ORD_BITS = 5;
constexpr uint32_t CODE_ORD_MAX = (1u << CODE_ORD_BITS) - 1;
constexpr uint64_t CODE_LEAD_MAX = (1ull << (32 - CODE_ORD_BITS)) - 2;
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
    uint8_t fast;       // between GPUs of one server (over NVLink: see Sim::h_srv)
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
    long long ob_last;  // its out link's last bucket filled (OutBucket::last)
    int64_t due_min;    // DCQCN: at most its queue pairs' earliest timer (INT64_MAX: none); a slot
                        // before it scans none of them
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
    long long ob_last;    // its out link's last bucket filled (OutBucket::last)
    nsm_rng::State rng;   // its ECN marking stream
    uint64_t tc_key;      // wide switches: its TransmitComplete's place (see Switch::evseq)
};

struct Switch
{
    uint32_t shared_used; // SwitchMmu::shared_used_bytes
    uint32_t total_hdrm, total_rsrv;
    uint32_t max_shift;   // the largest pfc_a_shift of its ports (the lowest PFC threshold)
    // wide switches number the events of their slots run in order (not safe); a TransmitComplete's
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
    int32_t link_in, link_out;
    int32_t in_delay, out_delay;
    uint32_t kmin, kmax;
    uint32_t in_coff, in_moff;
    uint16_t in_nb, in_cap;
    FDiv in_fnb;          // in_nb
};

// a block's entity (BlockInfo): a host (base: nic_off[h], link0: its first in-link) or a switch
// (base: pbase[x])
struct BlockInfo
{
    int e, deg, base, link0;
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
    FDiv fnb;       // nb
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
    // per block, its entity's static data in one load: rdma_slot's blocks (hostsm, hosts1,
    // switches_s) and rdma_slot_big's
    const struct BlockInfo *bi_s, *bi_b;
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
    struct SwPortLds *swws; // [ns_b][kb] the wide switches' ports, when they exceed LDS (else none)
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
    FDiv fpend, fD, fnbi; // pend_n, the slot (P.delay), nbi
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
    // windows: a block runs k slots in a launch when nothing can reach its entity within them (the
    // shortest delay of its in-links that can carry frames is at least k slots); an entity whose
    // window is ahead of the launch's slot sits it out. A GPU's NVLink in-link counts only while
    // its server has NVLink queue pairs running, or starting within the window.
    int64_t *hz;                  // [H + S] an entity's first slot not run (when ahead)
    // ranks ahead (rdma_rank): a wide switch's simultaneous arrivals in event order, rank_ahead
    // slots before it runs them: [frames] slot << 20 | rank (among those of its time)
    int64_t *frank;
    const uint8_t *rk_ok;         // [S] its in-links are long enough (all of slot + rank_ahead filed)
    int rank_ahead;               // (0: off)
    int rank_every;               // a ranking every this many launches, of as many slots
    const uint8_t *wk, *wq;       // [H + S] its window; a GPU's when its server is quiet
    const int *h_srv;             // [H] a GPU's server: its NVSwitch's index (-1: not a GPU)
    int *srv_fact;                // [S] a server's running NVLink queue pairs
    int any_win;                  // some entity's window can exceed a slot
    // global windows (--gwin): every entity runs gk slots per launch. gk_static bounds them by the
    // in-links (without NVLink) and send_lat; a server whose NVLink is quiet bounds them by its
    // next NVLink flow's start (gk_hz: the servers' least horizon, in slots; 0: one is busy)
    int gwin, gk_static;
    int *gk;
    unsigned long long *gk_hz;
    const int *srv_rs_off;        // [S + 1] a server's NVLink flows of fixed start, by start:
    const int64_t *srv_rs;        //   their start slots
    struct DMsg *dmsg;
    unsigned int *hhead;  // [H][nbi] a host's messages in each bucket: a list (index + 1; 0: none)
    int *srv_fpend;       // [S] a server's NVLink flows fired and not started yet
    // traced DAGs (docs/ai/dag.md): each cause's actions by ordinal (causes: 2 flow + kind, then
    // 2F + timer), timers' delays, and the chains and times of the timers fired
    const int *act_off;
    const struct DAct *act;
    const int64_t *tdelay;
    Chain *tchain;
    int64_t *ttime;
    int *tnext;           // (the timers waiting to fire their actions, in dag_fire)
    int traced;
    unsigned int *dmsg_n;
    int nbi, dcap;
    int *dyn, *dyn_n;
    int dyn_cap;
    const struct PairNic *pnic;
    uint16_t *pcnt;
    const int *wh_base, *wh_n;
    const int64_t *wp_off;
#ifdef TWINS_ASTRA
    // the closed loop with astra-sim (src/astra/closed.h)
    struct ClCtl *cl;
    struct CEvH *cev_h;
    Chain *cev_c;
    unsigned long long cev_mask;
    const struct CAct *cact;
    struct CSort *cs;
    int cl_span;
#endif
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
    uint32_t next; // the host's next one in the bucket (index + 1; 0: none)
};
// a traced DAG's action: a flow installed (SendFlow) or a timer scheduled (DACT_TIMER | timer), the
// ord-th of its cause's callback
struct DAct
{
    uint32_t child, ord;
};
constexpr uint32_t DACT_TIMER = 1u << 31;
#ifdef TWINS_ASTRA
// The closed loop with astra-sim on the host (src/astra/closed.h). A completion (a QpComplete or a
// SendComplete) is recorded for the host; its chain stays in cev_c for the actions of its callback.
struct CEvH
{
    int64_t t;
    int32_t flow;
    uint32_t kind; // 0 QpComplete, 1 SendComplete
    uint32_t base; // ordinals its event had used
    uint32_t code; // its chain's first code (the lead: its order among timers of its time)
};
// an action of a callback, for closed_apply: a flow installed or a timer scheduled (DACT_TIMER | its
// place in tchain), from a completion (its place in cev_c) or a timer (DACT_TIMER | its place)
struct CAct
{
    uint32_t child, parent, ord, pad;
    int64_t t, delay;
};
struct ClCtl
{
    unsigned long long head;  // completions recorded
    unsigned long long seen;  // of them, those the host has read
    unsigned long long floor; // the oldest it still needs
    int64_t limit;            // launches run no slot from here on
    int64_t cap;              // the slot of the host's next event (INT64_MAX: none)
    int idle;                 // nothing on the way, nothing due, nothing for the host
    int cmp;                  // closed_cmp's result
    int k;                    // the next launch's slots (the static bound clamped by the horizons)
    int pad;
};
// a completion among those of its time: rank among [g0, g1)
struct CSort
{
    uint32_t j;
    int g0, g1, rank;
};
#endif
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
    int b = (int)fmod(l.fnb, slot);
    long long v = s.msg_cnt[l.coff + b];
    *base = (uint32_t)(l.moff + (uint64_t)b * l.cap);
    return (v >> 16) == slot ? (int)(v & 0xFFFF) : 0;
}

} // namespace rdma

#endif
