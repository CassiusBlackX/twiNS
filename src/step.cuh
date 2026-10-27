// TwiNS device code: egress FIFO with RED, host TCP (ns-3 TcpSocketBase semantics),
// switch forwarding. See model.h for the execution model.
#ifndef TWINS_STEP_CUH
#define TWINS_STEP_CUH

#include "model.h"
#include "nsmath.h"

namespace nsm
{

__device__ inline void frame_copy(Frame *d, const Frame *s)
{
    const uint4 *a = reinterpret_cast<const uint4 *>(s);
    uint4 *b = reinterpret_cast<uint4 *>(d);
    uint4 x = a[0], y = a[1], z = a[2], w = a[3];
    b[0] = x;
    b[1] = y;
    b[2] = z;
    b[3] = w;
}

// ------------------------------------------------------------------ ns-3 event order
// Codes of the lead from a parent event to its child, increasing with the lead:
//   ScheduleNow (CompleteFork, ConnectionSucceeded) 1
//   Socket::Send's TimeStep(1) deferral          2
//   TransmitComplete of a frame of len bytes     len             (lead len * 80 ps)
//   arrival of a frame of len bytes              2048 + len      (lead 1 us + len * 80 ps)
//   host timers and other events                 4096 + lead/16 ns, saturating
__device__ inline uint16_t chain_tc(int len) { return (uint16_t)len; }
__device__ inline uint16_t chain_arrival(int len) { return (uint16_t)(2048 + len); }
__device__ inline uint16_t chain_lead(int64_t lead_ps)
{
    int64_t c = 4096 + lead_ps / 16000;
    return (uint16_t)(c < 0xFFFE ? c : 0xFFFE);
}
constexpr uint16_t CHAIN_NOW = 1, CHAIN_STEP = 2;

// A chain: the leads from an event back towards its root, run-length encoded as up to
// CHAIN_RUNS (code, count) pairs (back-to-back transmissions form long runs of TransmitComplete
// codes), then the root: the application start that began it, ordered as ns-3 schedules them
// at setup (by source host, then flow; stored as host + 1 and the flow's low 16 bits). When
// the runs overflow the oldest are dropped; the root is kept.
__device__ inline void chain_root(uint16_t *dst, int host, int flow)
{
    for (int i = 0; i < 2 * CHAIN_RUNS; i++)
        dst[i] = 0;
    dst[2 * CHAIN_RUNS] = (uint16_t)(host + 1);
    dst[2 * CHAIN_RUNS + 1] = (uint16_t)flow;
}

__device__ inline void chain_push(uint16_t *dst, uint16_t code, const uint16_t *parent)
{
#ifdef NS_NO_CHAIN
    return;
#endif
    if (parent && parent[0] == code)
    {
        for (int i = 0; i < CHAIN; i++)
            dst[i] = parent[i];
        if (dst[1] < 0xFFFF)
            dst[1]++;
        return;
    }
    for (int i = 2 * CHAIN_RUNS - 1; i >= 2; i--)
        dst[i] = parent ? parent[i - 2] : 0;
    dst[0] = code;
    dst[1] = 1;
    dst[2 * CHAIN_RUNS] = parent ? parent[2 * CHAIN_RUNS] : 0;
    dst[2 * CHAIN_RUNS + 1] = parent ? parent[2 * CHAIN_RUNS + 1] : 0;
}

// Order of two events with the same time by their chains: < 0 if a runs first, > 0 if b does,
// 0 if the chains do not tell. Larger leads (earlier parents) come first. A chain with fewer
// than CHAIN_RUNS runs is complete: after its last run comes its root, an application start
// scheduled at setup, which precedes any other parent. Where both end together, or a full
// (possibly truncated) chain ends, the roots decide.
__device__ inline int chain_cmp(const uint16_t *a, const uint16_t *b)
{
    int ia = 0, ib = 0;
    uint32_t ra = a[1], rb = b[1];
    for (;;)
    {
        uint16_t ca = a[2 * ia], cb = b[2 * ib];
        if (ca == 0 && cb != 0 && a[2 * CHAIN_RUNS] != 0)
            return -1; // a reached its root
        if (cb == 0 && ca != 0 && b[2 * CHAIN_RUNS] != 0)
            return 1;
        if (ca == 0 || cb == 0)
            break;
        if (ca != cb)
            return ca > cb ? -1 : 1;
        uint32_t m = ra < rb ? ra : rb;
        ra -= m;
        rb -= m;
        if (ra == 0)
        {
            if (++ia == CHAIN_RUNS)
                break;
            ra = a[2 * ia + 1];
        }
        if (rb == 0)
        {
            if (++ib == CHAIN_RUNS)
                break;
            rb = b[2 * ib + 1];
        }
    }
    uint32_t xa = (uint32_t)a[2 * CHAIN_RUNS] << 16 | a[2 * CHAIN_RUNS + 1];
    uint32_t xb = (uint32_t)b[2 * CHAIN_RUNS] << 16 | b[2 * CHAIN_RUNS + 1];
    if (xa == 0 || xb == 0 || xa == xb)
        return 0;
    return xa < xb ? -1 : 1;
}

__device__ inline uint32_t payload_of(const Frame &p) { return (p.flags & F_DATA) ? p.len - 42u : 0u; }

// ------------------------------------------------------------------ egress FIFO
// Frames waiting for the link (not yet started); the link is a point-to-point device with
// a one-packet device queue under a RED queue disc (ns-3's structure). Transmissions that
// start in the current slot are written to the link's message for this slot.
struct Fifo
{
    Frame *ring;
    int head, size;
    int64_t link_free;  // end of the transmission in progress
    int64_t last_start; // start of the transmission in progress
    int64_t woke = -1;  // a ScheduleNow'd event runs at this instant (see enqueue)
    int64_t idle_start = -1; // the last start at an idle link (this slot)
    uint16_t *last_chain; // [CHAIN] chain of the transmission in progress (persistent)
    int id = -1;        // host h, or H + switch port (tracing)
    int red_k = RED_K, qcap = QDISC_CAP;
    Frame *out;        // this slot's message
    int sent;
    int64_t slot_end;
    const Sim *s;

    // start every frame whose transmission begins at or before `until` (and in this slot)
    __device__ __forceinline__ void advance(int64_t until)
    {
        while (size > 0 && sent < MAX_TX)
        {
            Frame &f = ring[head];
            int64_t start = f.ts > link_free ? f.ts : link_free;
            if (start > until || start >= slot_end)
                return;
            int64_t end = start + (int64_t)f.len * PS_PER_BYTE;
            Frame o = f;
            // the event that starts this transmission: the frame's own enqueue on an idle link
            // (o.chain is already the enqueue event's chain), else the TransmitComplete of the
            // previous frame, scheduled when that one started
            if (f.ts <= link_free && link_free > last_start) // (a link that never sent is idle)
            {
                uint16_t prev[CHAIN];
                for (int i = 0; i < CHAIN; i++)
                    prev[i] = last_chain[i];
                chain_push(o.chain, chain_tc((int)((link_free - last_start) / PS_PER_BYTE)), prev);
            }
            else
            {
                idle_start = start;
            }
            for (int i = 0; i < CHAIN; i++)
                last_chain[i] = o.chain[i];
            last_start = start;
            o.ts = end + PROP_PS;
            frame_copy(&out[sent], &o);
            sent++;
            link_free = end;
            head = (head + 1) % s->ring;
            size--;
        }
        if (size > 0 && sent >= MAX_TX)
        {
            atomicAdd(&s->ev[4], 1ull);
#ifdef NS_TRACE_CAP
            int acks = 0, data = 0;
            for (int i = 0; i < sent; i++)
                (out[i].flags & F_DATA) ? data++ : acks++;
            printf("tx-cap: data %d ctrl %d first %lld last %lld slot_end %lld link_free %lld next_ts %lld waiting %d\n", data, acks,
                   (long long)out[0].ts, (long long)out[sent - 1].ts, (long long)slot_end, (long long)link_free,
                   (long long)ring[head].ts, size);
#endif
        }
    }

    // RED (MinTh = MaxTh = K, QW = 1, not gentle) on the queue disc length at enqueue
    // time, which excludes the packet in ns-3's one-packet device queue; then the 200-packet
    // limit. The device queue is refilled by a Wake event that ns-3 schedules (ScheduleNow)
    // when a packet leaves it, i.e. after every event already pending at that instant: an
    // arrival at the instant a transmission starts sees the device queue empty, and an
    // arrival at the instant one ends runs before TransmitComplete and sees it full.
    __device__ bool enqueue(const Frame &f, int64_t now)
    {
        advance(now);
        // A ScheduleNow'd event (woke == now) runs after the Wakes of starts at idle links in
        // earlier events at this instant, but before the Wake of a start at TransmitComplete
        // (TransmitComplete runs after the arrivals that scheduled such events).
        bool refilled = last_start < now || (woke == now && idle_start == now);
        int nq = (size > 0 && link_free > now && refilled) ? size - 1 : size;
        Frame g = f;
        g.ts = now;
        if (nq >= red_k && nq > 1)
        {
            if (g.flags & F_ECT)
            {
                g.flags |= F_CE;
                atomicAdd(&s->ev[1], 1ull);
            }
            else
            {
                atomicAdd(&s->ev[0], 1ull);
                return false;
            }
        }
        if (nq >= qcap)
        {
            atomicAdd(&s->ev[0], 1ull);
            return false;
        }
        if (s->qtrace && now < s->qtrace_until && now >= s->qtrace_from)
        {
            unsigned long long i = atomicAdd(s->qtrace_cnt, 1ull);
            if (i < s->qtrace_n)
            {
                QRec &r = s->qtrace[i];
                r.ts = now;
                r.node = id < s->H ? id : s->H + (id - s->H) / s->k;
                r.dev = id < s->H ? 1 : (id - s->H) % s->k + 1;
                r.src = g.to_sender ? s->fl.dst[g.flow] : s->fl.src[g.flow];
                r.dst = g.to_sender ? s->fl.src[g.flow] : s->fl.dst[g.flow];
                r.sa = g.sa;
                r.len = g.len;
                r.qlen = (uint16_t)(nq + 1);
                r.flags = g.flags;
            }
        }
        if (size >= s->ring)
        {
            atomicAdd(&s->ev[19], 1ull); // cannot happen when ring > qcap + 1
            return false;
        }
        frame_copy(&ring[(head + size) % s->ring], &g);
        size++;
#ifdef NS_TRACE
        {
            // node / device numbering of the ns-3 scenario
            int node = id < s->H ? id : s->H + (id - s->H) / s->k, dev = id < s->H ? 1 : (id - s->H) % s->k + 1;
#ifdef NS_TRACE_ALL
            bool mine = g.flow == NS_TRACE;
#else
            bool mine = g.flow == NS_TRACE && (g.flags & F_DATA) && !g.to_sender;
#endif
#ifdef NS_TRACE_NODE
            bool here = node == NS_TRACE_NODE && dev == NS_TRACE_DEV;
#else
            bool here = false;
#endif
            if (mine || here)
            {
                printf("enq %lld node %d dev %d flags %d flow %u seq %u qlen-after %d ecn %d len %d chain", (long long)now, node, dev,
                       g.flags, g.flow, g.sa + 1, nq + 1, (g.flags & F_CE) ? 3 : 2, g.len);
                for (int i = 0; i < 2 * CHAIN_RUNS; i += 2)
                    printf(" %d:%d", g.chain[i], g.chain[i + 1]);
                printf(" root %d/%d", g.chain[2 * CHAIN_RUNS] - 1, g.chain[2 * CHAIN_RUNS + 1]);
                printf("\n");
            }
        }
#endif
        return true;
    }
};

// ------------------------------------------------------------------ host TCP
struct Due
{
    int64_t at;
    int flow;
    int kind; // 0 rto, 1 delack, 2 syn (or the deferred first send of an established flow),
              // 3 the receiver's SYN-ACK retransmission
};

struct HostCtx
{
    const Sim *s;
    Fifo *nic;
    int h;
    int dctcp, cubic; // the host's TCP
    uint16_t echain[CHAIN]; // ns-3 event-order chain of the event being processed
    Due *due;               // timers due in this slot, sorted; [*id, *nd) are pending
    int *nd, *id;
    int64_t slot_end;
};

// a timer set during the slot, due within it: insert after the pending ones due no later
__device__ inline void due_insert(HostCtx &c, int64_t at, int f, int kind)
{
    if (at >= c.slot_end)
        return; // collected at the start of its slot
    int n = *c.nd;
    if (n >= MAX_DUE)
    {
        atomicAdd(&c.s->ev[7], 1ull);
        return;
    }
    int j = n;
    while (j > *c.id && c.due[j - 1].at > at)
    {
        c.due[j] = c.due[j - 1];
        j--;
    }
    c.due[j] = {at, f, kind};
    *c.nd = n + 1;
}

// a timer scheduled by the current event: its chain is [lead] + the event's chain
__device__ inline void timer_chain(HostCtx &c, uint16_t *base, int f, int64_t lead)
{
    chain_push(base + (size_t)f * CHAIN, chain_lead(lead), c.echain);
}

__device__ inline void emit(HostCtx &c, int f, uint8_t flags, uint32_t seq, uint32_t ack, uint16_t payload, uint16_t len,
                            bool to_sender, int64_t now)
{
    const FlowSoA &F = c.s->fl;
    Frame fr;
    fr.ts = now;
    fr.flow = (uint32_t)f;
    fr.sa = (flags & F_DATA) ? seq : ack;
    fr.len = len;
    fr.dst_node = (uint16_t)((to_sender ? F.src[f] : F.dst[f]) % c.s->H); // world-local ids == global (one world)
    fr.flags = flags | (c.dctcp ? F_ECT : 0);
    fr.to_sender = to_sender ? 1 : 0;
    fr.up = (uint16_t)((to_sender ? F.hash_ack[f] : F.hash_data[f]) % (uint32_t)c.s->ecmp_n); // ECMP choice
    for (int i = 0; i < CHAIN; i++)
        fr.chain[i] = c.echain[i];
    c.nic->enqueue(fr, now);
}

// receiver: pure ACK with ECE according to the receiver's ECN state (SendEmptyPacket)
__device__ inline void send_ack(HostCtx &c, int f, uint32_t ack, bool force_ece, bool no_ece, int64_t now)
{
    const FlowSoA &F = c.s->fl;
    bool ece = false;
    if (force_ece)
        ece = true;
    else if (!no_ece && (F.ecn_r[f] == E_CE_RCVD || F.ecn_r[f] == E_SENDING_ECE))
    {
        ece = true;
        F.ecn_r[f] = E_SENDING_ECE;
    }
#ifdef NS_TRACE
    if (f == NS_TRACE)
        printf("srv-tx %lld ack %u%s ecn_r %d ce_state %d reserved %d\n", (long long)now, ack + 1, ece ? " ECE" : "", F.ecn_r[f],
               F.ce_state[f], F.reserved[f]);
#endif
    // SendEmptyPacket(ACK) cancels the delayed ACK
    F.delack_at[f] = -1;
    F.delack_count[f] = 0;
    emit(c, f, F_ACK | (ece ? F_ECE : 0), 0, ack, 0, CTRL_FRAME, true, now);
}

// ---- TcpTxBuffer scoreboard without SACK (Reno emulation), in segments of the sent list
// (index 0 = the head at una). Under the operations ns-3 applies without SACK the flags keep
// a compact shape:
//   sacked  : the block [1, 1 + sacked)       (AddRenoSack sacks the first unsacked after the head)
//   lost    : the prefix [0, lost_n)          (MarkHeadAsLost, SetSentListLost)
//   retrans : the prefix [0, rtx_n), except the head when !head_rx (MarkHeadAsLost clears it)
// All segments are MSS long (flow sizes are MSS multiples).
__device__ inline uint32_t sb_items(const FlowSoA &F, int f)
{
    return (F.high[f] - F.una[f] + MSS - 1) / MSS;
}

__device__ inline uint32_t sb_retrans(const FlowSoA &F, int f)
{
    uint32_t r = F.rtx_n[f];
    return r == 0 ? 0 : r - (F.head_rx[f] ? 0 : 1);
}

// TcpTxBuffer::BytesInFlight = sent - (sacked + lost) + retrans
__device__ inline uint32_t inflight(const FlowSoA &F, int f)
{
    uint32_t sent = F.high[f] - F.una[f];
    uint32_t left = (F.sacked[f] + F.lost_n[f] - sb_retrans(F, f)) * MSS;
    return sent > left ? sent - left : 0;
}

__device__ inline void sb_add_reno_sack(const FlowSoA &F, int f)
{
    if (1 + F.sacked[f] < sb_items(F, f))
        F.sacked[f]++;
}

__device__ inline void sb_mark_head_lost(const FlowSoA &F, int f)
{
    if (sb_items(F, f) == 0)
        return;
    if (F.rtx_n[f] >= 1 && F.head_rx[f])
    {
        F.head_rx[f] = 0;
        if (F.rtx_n[f] == 1)
            F.rtx_n[f] = 0;
    }
    if (F.lost_n[f] < 1)
        F.lost_n[f] = 1;
}

// TcpTxBuffer::DiscardUpTo: drop m acknowledged segments (una already advanced). A head that
// was (wrongly guessed) sacked is unsacked, the sack moves to the next segment and the head is
// marked lost.
__device__ inline void sb_discard(const FlowSoA &F, int f, uint32_t m)
{
    if (m == 0)
        return;
    F.lost_n[f] = F.lost_n[f] > m ? F.lost_n[f] - m : 0;
    if (F.rtx_n[f] > m)
    {
        F.rtx_n[f] -= m;
        F.head_rx[f] = 1;
    }
    else
    {
        F.rtx_n[f] = 0;
        F.head_rx[f] = 0;
    }
    uint32_t n = F.sacked[f];
    if (n > 0 && m <= n)
    {
        F.sacked[f] = n - m;
        sb_add_reno_sack(F, f);
        sb_mark_head_lost(F, f);
    }
    else
    {
        F.sacked[f] = 0;
    }
}

// TcpTxBuffer::NextSeg (no SACK, so no rule 3): the first lost, not retransmitted, not sacked
// segment below the highest sack, else new data. Returns 0xFFFFFFFF if there is none.
__device__ inline uint32_t sb_next_seg(const FlowSoA &F, int f)
{
    uint32_t L = F.lost_n[f], R = F.rtx_n[f], n = F.sacked[f], una = F.una[f];
    if (L >= 1 && !(R >= 1 && F.head_rx[f]))
        return una;
    if (n == 0)
    {
        uint32_t i = R > 1 ? R : 1;
        if (i < L)
            return una + i * MSS;
    }
    if (F.high[f] < F.size[f])
        return F.high[f];
    return 0xFFFFFFFFu;
}

// a retransmission of segment seq sets its retrans flag (TcpTxBuffer::GetTransmittedSegment)
__device__ inline void sb_mark_retx(const FlowSoA &F, int f, uint32_t seq)
{
    uint32_t i = (seq - F.una[f]) / MSS;
    if (i == 0)
    {
        if (F.rtx_n[f] == 0)
            F.rtx_n[f] = 1;
        F.head_rx[f] = 1;
    }
    else
    {
        if (F.rtx_n[f] == 0)
            F.head_rx[f] = 0;
        if (i + 1 > F.rtx_n[f])
            F.rtx_n[f] = i + 1;
    }
}

__device__ inline uint32_t cub_ssthresh(const FlowSoA &F, int f);

__device__ inline uint32_t ssthresh_for(HostCtx &c, int f, uint32_t bytes_in_flight)
{
    const FlowSoA &F = c.s->fl;
    if (c.cubic)
        return cub_ssthresh(F, f);
    if (c.dctcp)
        return (uint32_t)((1.0 - F.alpha[f] / 2.0) * F.cwnd[f]); // TcpDctcp::GetSsThresh
    uint32_t half = bytes_in_flight / 2;                           // TcpNewReno::GetSsThresh
    return half > 2 * MSS ? half : 2 * MSS;
}

// RttMeanDeviation::Measurement (integer Jacobson/Karels update, alpha 1/8, beta 1/4, in ps) and
// the RTO of RFC 6298 as TcpSocketBase computes it
__device__ inline void rtt_sample(const FlowSoA &F, int f, int64_t m)
{
    int64_t est = F.srtt[f], var = F.rttvar[f];
    if (est < 0)
    {
        est = m;
        var = m / 2;
    }
    else
    {
        int64_t delta = m - est;
        est = ((est << 3) + delta) >> 3;
        if (delta < 0)
            delta = -delta;
        var = ((var << 2) + delta - var) >> 2;
    }
    F.srtt[f] = est;
    F.rttvar[f] = var;
    int64_t v = 4 * var;
    int64_t rto = est + (v > CLOCK_GRAN_PS ? v : CLOCK_GRAN_PS);
    F.rto[f] = rto > MIN_RTO_PS ? rto : MIN_RTO_PS;
}

__device__ inline void send_segment(HostCtx &c, int f, uint32_t seq, int64_t now)
{
    const FlowSoA &F = c.s->fl;
    uint32_t len = F.size[f] - seq < MSS ? F.size[f] - seq : MSS;
    bool retx = seq < F.high[f];
#ifdef NS_TRACE
    if (f == NS_TRACE)
        printf("tx %lld %u%s st %d\n", (long long)(now / 1000), seq + 1, retx ? " R" : "", F.cong[f]);
#endif
    // SendDataPacket: the first new segment after an ECN echo carries CWR
    uint8_t cwr = 0;
    if (!retx && F.ecn_s[f] == E_ECE_RCVD && F.ecn_echo_seq[f] > F.ecn_cwr_seq[f])
    {
        F.ecn_s[f] = E_CWR_SENT;
        F.ecn_cwr_seq[f] = seq;
        cwr = F_CWR;
    }
    emit(c, f, F_DATA | cwr, seq, 0, (uint16_t)len, (uint16_t)(len + 42), false, now);
    if (retx)
        sb_mark_retx(F, f, seq);
    if (retx)
    {
        atomicAdd(&c.s->ev[2], 1ull);
        atomicAdd(&c.s->ev[8 + F.cong[f]], 1ull);
    }
    if (seq + len > F.high[f])
        F.high[f] = seq + len;
    atomicMax(&c.s->ev[13], (unsigned long long)((F.high[f] - F.una[f] + MSS - 1) / MSS));
    F.cwnd_limited[f] = F.cwnd[f] < inflight(F, f) + MSS; // SendDataPacket: m_isCwndLimited
    if (F.rto_at[f] < 0)
    {
        F.rto_at[f] = now + F.rto[f];
        timer_chain(c, c.s->fl.rchain, f, F.rto[f]);
    }
    // RTT history (TcpSocketBase::UpdateRttHistory): first transmissions are logged, a
    // retransmission marks its entry (here: invalidates the tag)
    uint32_t sg = seq / MSS;
    size_t ri = c.s->fl.hist_off[f] + sg % c.s->fl.hist_n[f];
    if (!retx)
    {
        c.s->fl.hist_t[ri] = now;
        c.s->fl.hist_tag[ri] = sg + 1;
    }
    else if (c.s->fl.hist_tag[ri] == sg + 1)
    {
        c.s->fl.hist_tag[ri] = 0;
    }
}

// TcpSocketBase::SendPendingData
__device__ inline void send_pending(HostCtx &c, int f, int64_t now)
{
    const FlowSoA &F = c.s->fl;
    if (F.st[f] != S_EST)
        return;
    uint32_t size = F.size[f];
    for (int guard = 0; guard < 4096; guard++)
    {
        uint32_t pipe = inflight(F, f);
        uint32_t cwnd = F.cwnd[f];
        uint32_t avail = cwnd > pipe ? cwnd - pipe : 0;
        if (avail == 0)
            return;
        uint32_t seq = sb_next_seg(F, f);
        if (seq == 0xFFFFFFFFu)
            return;
        uint32_t remaining = size - seq;
        if (avail < MSS && remaining > avail)
            return;
        send_segment(c, f, seq, now);
        uint32_t len = remaining < MSS ? remaining : MSS;
        F.nxt[f] = seq + len;
    }
}

// TcpSocketBase::DoRetransmit: the first lost segment (the head without SACK)
__device__ inline void do_retransmit(HostCtx &c, int f, int64_t now)
{
    const FlowSoA &F = c.s->fl;
    uint32_t seq = sb_next_seg(F, f);
    if (seq == 0xFFFFFFFFu || seq >= F.high[f])
        seq = F.una[f];
    F.nxt[f] = seq;
    send_segment(c, f, seq, now);
}

// ---- TcpCubic (tcp-cubic.cc), defaults: C 0.4, beta 0.7, fast convergence, TCP friendliness,
// HyStart (both detectors, low window 16, 8 samples, ack delta 2 ms, delay clamp [4, 1000] ms),
// CubicDelta 10 ms, CntClamp 20. Times in ps as ns-3's Time; Time::Min() is INT64_MIN.
constexpr double CUB_C = 0.4, CUB_BETA = 0.7;
constexpr int64_t CUB_ACK_DELTA = 2000000000, CUB_DELAY_MIN = 4000000000, CUB_DELAY_MAX = 1000000000000,
                  CUB_DELTA = 10000000000;

// double -> uint32_t as GCC compiles it on x86-64 (cvttsd2si into 64 bits, low half)
__device__ inline uint32_t cub_u32(double d)
{
    return (uint32_t)(uint64_t)(int64_t)d;
}

__device__ inline void cub_hystart_reset(const FlowSoA &F, int f, int64_t now)
{
    F.cub_roundstart[f] = F.cub_lastack[f] = now;
    F.cub_endseq[f] = F.st[f] == S_EST ? F.high[f] + 1 : 0; // m_highTxMark
    F.cub_currrtt[f] = INT64_MIN;
    F.cub_samples[f] = 0;
}

__device__ inline uint32_t cub_update(const FlowSoA &F, int f, uint32_t segs, int64_t now)
{
    uint32_t seg_cwnd = F.cwnd[f] / MSS;
    F.cub_ackcnt[f] += segs;
    if (F.cub_epoch[f] == INT64_MIN)
    {
        F.cub_epoch[f] = now;
        F.cub_ackcnt[f] = segs;
        F.cub_tcpcwnd[f] = seg_cwnd;
        if (F.cub_lastmax[f] <= seg_cwnd)
        {
            F.cub_k[f] = 0.0;
            F.cub_origin[f] = seg_cwnd;
        }
        else
        {
            F.cub_k[f] = nsm_pow((double)(F.cub_lastmax[f] - seg_cwnd) / CUB_C, 1 / 3.);
            F.cub_origin[f] = F.cub_lastmax[f];
        }
    }
    int64_t t = (int64_t)((uint64_t)now + (uint64_t)F.cub_delaymin[f] - (uint64_t)F.cub_epoch[f]);
    double ts = nsm_seconds(t), k = F.cub_k[f];
    double offs = ts < k ? k - ts : ts - k;
    uint32_t delta = cub_u32(CUB_C * nsm_pow(offs, 3));
    uint32_t target = ts < k ? F.cub_origin[f] - delta : F.cub_origin[f] + delta;
    uint32_t cnt = target > seg_cwnd ? seg_cwnd / (target - seg_cwnd) : 100 * seg_cwnd;
    if (F.cub_lastmax[f] == 0 && cnt > 20)
        cnt = 20;
    // TCP friendliness
    uint32_t scale = cub_u32(8 * (1024 + CUB_BETA * 1024) / 3 / (1024 - CUB_BETA * 1024));
    delta = (seg_cwnd * scale) >> 3;
    while (F.cub_ackcnt[f] > delta)
    {
        F.cub_ackcnt[f] -= delta;
        F.cub_tcpcwnd[f]++;
    }
    if (F.cub_tcpcwnd[f] > seg_cwnd)
    {
        uint32_t max_cnt = seg_cwnd / (F.cub_tcpcwnd[f] - seg_cwnd);
        if (cnt > max_cnt)
            cnt = max_cnt;
    }
    return cnt > 2 ? cnt : 2;
}

__device__ inline void cub_pkts_acked(const FlowSoA &F, int f, int64_t now)
{
    if (F.cub_epoch[f] != INT64_MIN && now - F.cub_epoch[f] < CUB_DELTA)
        return; // delay samples right after fast recovery are discarded
    int64_t rtt = F.srtt[f]; // ns-3 passes tcb->m_srtt
    if (F.cub_delaymin[f] == INT64_MIN || F.cub_delaymin[f] > rtt)
        F.cub_delaymin[f] = rtt;
    uint32_t cwnd = F.cwnd[f];
    if (!(cwnd <= F.ssthresh[f] && cwnd >= 16 * MSS) || F.cub_found[f])
        return;
    // HystartUpdate
    if (now - F.cub_lastack[f] <= CUB_ACK_DELTA)
    {
        F.cub_lastack[f] = now;
        if (now - F.cub_roundstart[f] > F.cub_delaymin[f])
            F.cub_found[f] = 1; // ack train
    }
    if (F.cub_samples[f] < 8)
    {
        if (F.cub_currrtt[f] == INT64_MIN || F.cub_currrtt[f] > rtt)
            F.cub_currrtt[f] = rtt;
        F.cub_samples[f]++;
    }
    else
    {
        int64_t dm = F.cub_delaymin[f];
        int64_t thresh = dm > CUB_DELAY_MAX ? CUB_DELAY_MAX : (dm < CUB_DELAY_MIN ? CUB_DELAY_MIN : dm);
        if (F.cub_currrtt[f] > dm + thresh)
            F.cub_found[f] = 1; // delay increase
    }
    if (F.cub_found[f])
        F.ssthresh[f] = cwnd;
}

__device__ inline void cub_increase(const FlowSoA &F, int f, uint32_t segs, uint32_t ack, int64_t now)
{
    if (!F.cwnd_limited[f])
        return;
    if (F.cwnd[f] < F.ssthresh[f])
    {
        if (ack + 1 > F.cub_endseq[f]) // m_lastAckedSeq > m_endSeq
            cub_hystart_reset(F, f, now);
        F.cwnd[f] += segs * MSS; // byte counting
        segs = 0;
    }
    if (F.cwnd[f] >= F.ssthresh[f] && segs > 0)
    {
        F.cub_cnt[f] += segs;
        uint32_t cnt = cub_update(F, f, segs, now);
        if (F.cub_cnt[f] >= cnt)
        {
            F.cwnd[f] += MSS;
            F.cub_cnt[f] -= cnt;
        }
    }
}

__device__ inline uint32_t cub_ssthresh(const FlowSoA &F, int f)
{
    uint32_t seg_cwnd = F.cwnd[f] / MSS;
    if (seg_cwnd < F.cub_lastmax[f])
        F.cub_lastmax[f] = cub_u32((seg_cwnd * (1 + CUB_BETA)) / 2); // fast convergence
    else
        F.cub_lastmax[f] = seg_cwnd;
    F.cub_epoch[f] = INT64_MIN;
    uint32_t th = cub_u32(seg_cwnd * CUB_BETA);
    return (th > 2 ? th : 2) * MSS;
}

// CongestionStateSet(CA_LOSS): CubicReset and HystartReset
__device__ inline void cub_loss(const FlowSoA &F, int f, int64_t now)
{
    F.cub_origin[f] = 0;
    F.cub_k[f] = 0;
    F.cub_ackcnt[f] = 0;
    F.cub_tcpcwnd[f] = 0;
    F.cub_delaymin[f] = INT64_MIN;
    F.cub_found[f] = 0;
    cub_hystart_reset(F, f, now);
}

__device__ inline void pkts_acked(HostCtx &c, int f, uint32_t segs, uint32_t ack, int64_t now)
{
    if (c.cubic)
    {
        cub_pkts_acked(c.s->fl, f, now);
        return;
    }
    if (!c.dctcp)
        return;
    const FlowSoA &F = c.s->fl;
    F.acked_total[f] += segs * MSS;
    if (F.ecn_s[f] == E_ECE_RCVD)
        F.acked_ecn[f] += segs * MSS;
    if (!F.next_seq_flag[f])
    {
        F.next_seq[f] = F.nxt[f];
        F.next_seq_flag[f] = 1;
    }
    if (ack >= F.next_seq[f])
    {
        double m = F.acked_total[f] > 0 ? (double)F.acked_ecn[f] / F.acked_total[f] : 0.0;
        // ns-3's optimized build (-march=native) contracts this into fma((1 - g), alpha, g * F)
        F.alpha[f] = __builtin_fma(1.0 - DCTCP_G, F.alpha[f], DCTCP_G * m);
#ifdef NS_TRACE
        if (f == NS_TRACE)
            printf("alpha %lld %u %u %a\n", (long long)(now / 1000), F.acked_ecn[f], F.acked_total[f], F.alpha[f]);
#endif
        F.next_seq[f] = F.nxt[f];
        F.acked_total[f] = 0;
        F.acked_ecn[f] = 0;
    }
}

__device__ inline void increase_window(HostCtx &c, int f, uint32_t segs, uint32_t ack, int64_t now)
{
    const FlowSoA &F = c.s->fl;
    if (c.cubic)
    {
        cub_increase(F, f, segs, ack, now);
        return;
    }
    uint32_t cwnd = F.cwnd[f], ssth = F.ssthresh[f];
    if (c.dctcp)
    {
        // TcpLinuxReno
        if (cwnd < ssth)
        {
            if (segs >= 1)
            {
                uint64_t n = (uint64_t)cwnd + (uint64_t)segs * MSS;
                F.cwnd[f] = (uint32_t)(n < ssth ? n : ssth);
            }
        }
        else
        {
            uint32_t w = cwnd / MSS;
            if (w == 0)
                w = 1;
            uint32_t cnt = F.cwnd_cnt[f];
            if (cnt >= w)
            {
                cnt = 0;
                cwnd += MSS;
            }
            cnt += segs;
            if (cnt >= w)
            {
                uint32_t delta = cnt / w;
                cnt -= delta * w;
                cwnd += delta * MSS;
            }
            F.cwnd[f] = cwnd;
            F.cwnd_cnt[f] = cnt;
        }
        return;
    }
    // TcpNewReno
    if (cwnd < ssth)
    {
        if (segs >= 1)
        {
            cwnd += MSS;
            segs -= 1;
        }
    }
    if (cwnd >= ssth && segs > 0)
    {
        double adder = (double)MSS * MSS / cwnd;
        if (adder < 1.0)
            adder = 1.0;
        cwnd += (uint32_t)adder;
    }
    F.cwnd[f] = cwnd;
}

// NewAck: restart the retransmission timer. ns-3 cancels it only when the tx buffer is empty,
// so it stays armed while unsent data remains (the application buffers the whole flow); that is
// how a flow whose cwnd fell below one segment gets going again
__device__ inline void new_ack(HostCtx &c, int f, uint32_t ack, bool reset_rto, int64_t now)
{
    const FlowSoA &F = c.s->fl;
    if (reset_rto)
    {
        // RFC 6298 2.4: the RTO is recomputed from the estimator, which also clears any backoff
        // (before the first sample the estimator holds InitialEstimation, 100 us)
        int64_t est = F.srtt[f] >= 0 ? F.srtt[f] : INIT_RTT_PS, v = 4 * F.rttvar[f];
        int64_t rto = est + (v > CLOCK_GRAN_PS ? v : CLOCK_GRAN_PS);
        F.rto[f] = rto > MIN_RTO_PS ? rto : MIN_RTO_PS;
        F.rto_at[f] = now + F.rto[f];
        timer_chain(c, c.s->fl.rchain, f, F.rto[f]);
    }
    if (ack >= F.size[f])
        F.rto_at[f] = -1;
    if (ack > F.nxt[f])
        F.nxt[f] = ack;
}

// TcpSocketBase::EnterRecovery (no SACK, classic recovery)
__device__ inline void enter_recovery(HostCtx &c, int f, int64_t now)
{
    const FlowSoA &F = c.s->fl;
    sb_add_reno_sack(F, f); // one segment has left the network
    sb_mark_head_lost(F, f);
    F.recover[f] = F.high[f];
    F.recover_active[f] = 1;
    F.cong[f] = C_RECOVERY;
    F.ssthresh[f] = ssthresh_for(c, f, inflight(F, f) + MSS); // the head still counts without SACK
    F.cwnd[f] = F.ssthresh[f];                                 // TcpClassicRecovery::EnterRecovery
    // the head goes out with SendDataPacket directly: m_nextTxSequence is not moved (unlike
    // DoRetransmit), which matters for DCTCP's observation window
    send_segment(c, f, F.una[f], now);
}

__device__ inline void flow_done(HostCtx &c, int f, int64_t now)
{
    const FlowSoA &F = c.s->fl;
    if (F.st[f] == S_DONE)
        return;
    F.st[f] = S_DONE;
    F.done[f] = now;
    F.rto_at[f] = -1;
    atomicAdd(c.s->flows_done, 1u);
}

// TcpSocketBase::ReceivedAck + ProcessAck (no SACK, classic recovery, no limited transmit)
__device__ inline void on_ack(HostCtx &c, int f, uint32_t ack, bool ece, int64_t now)
{
    const FlowSoA &F = c.s->fl;
    if (F.st[f] != S_EST)
        return;
#ifdef NS_TRACE
    if (f == NS_TRACE)
        printf("ack %lld %u cwnd-before %u%s cong %d ssth %u recover %u high %u ecn_s %d echo %lld cwrseq %lld\n", (long long)(now / 1000),
               ack + 1, F.cwnd[f], ece ? " ECE" : "", F.cong[f], F.ssthresh[f], F.recover[f] + 1, F.high[f] + 1, F.ecn_s[f],
               (long long)F.ecn_echo_seq[f] + 1, (long long)F.ecn_cwr_seq[f] + 1);
#endif
    uint32_t old = F.una[f];
    if (ack < old)
        return;
    // TcpSocketBase::EstimateRtt: a sample from the oldest history entry if this ACK covers it
    // and it was not retransmitted. The entries are the first transmissions since the last RTO
    // (which clears the history), acknowledged entries are removed.
    {
        uint32_t lo = old > F.hist_lo[f] ? old : F.hist_lo[f];
        uint32_t sg = lo / MSS;
        if (lo < F.high[f] && ack >= lo + MSS || (lo < F.high[f] && ack >= F.size[f]))
        {
            size_t ri = c.s->fl.hist_off[f] + sg % c.s->fl.hist_n[f];
            if (c.s->fl.hist_tag[ri] == sg + 1)
                rtt_sample(F, f, now - c.s->fl.hist_t[ri]);
            else if (lo >= F.hist_lo[f] && F.high[f] - lo > c.s->fl.hist_n[f] * MSS)
                atomicAdd(&c.s->ev[14], 1ull); // entry overwritten: more segments in flight than entries
        }
    }
    bool advancing = ack > old;
    if (advancing)
    {
        F.una[f] = ack;
        sb_discard(F, f, (ack - old + MSS - 1) / MSS); // TcpTxBuffer::DiscardUpTo
    }
    if (F.cong[f] == C_CWR && ack > F.recover[f])
    {
        F.cong[f] = C_OPEN;
        F.cwnd[f] = F.ssthresh[f];
    }
    if (advancing && c.dctcp && ece)
    {
        if (F.ecn_echo_seq[f] < (int64_t)ack)
        {
            F.ecn_echo_seq[f] = ack;
            F.ecn_s[f] = E_ECE_RCVD;
            if (F.cong[f] != C_CWR)
            {
                // EnterCwr
                F.ssthresh[f] = ssthresh_for(c, f, inflight(F, f));
                F.cwnd[f] = F.ssthresh[f];
                F.cong[f] = C_CWR;
                F.recover[f] = F.high[f];
            }
        }
    }
    else if (F.ecn_s[f] == E_ECE_RCVD && !ece)
    {
        F.ecn_s[f] = E_IDLE;
    }

    // ProcessAck
    bool dupack = ack == old && ack < F.high[f];
    uint32_t old_dup = F.dupacks[f];
    if (dupack)
    {
        uint8_t cg = F.cong[f];
        if (cg != C_LOSS)
        {
            if (cg != C_RECOVERY)
                F.dupacks[f] = ++old_dup, old_dup = F.dupacks[f] - 1;
            if (cg == C_OPEN)
                F.cong[f] = cg = C_DISORDER;
            if (cg == C_RECOVERY)
                sb_add_reno_sack(F, f); // one segment has left the network
            else if (cg == C_DISORDER)
            {
                if (F.dupacks[f] == DUPTHRESH && (old >= F.recover[f] || !F.recover_active[f]))
                    enter_recovery(c, f, now);
                else if (F.sacked[f] > 0 && F.lost_n[f] >= 1)
                    enter_recovery(c, f, now); // IsLost(HighACK): a sack above a lost head
            }
        }
    }
    if (ack == old && ack == F.high[f])
    {
        // nothing outstanding
    }
    else if (ack == old)
    {
        pkts_acked(c, f, 1, ack, now);
    }
    else if (advancing)
    {
        uint32_t segs = (ack - old) / MSS;
        if ((ack - old) % MSS)
            segs++; // the final partial segment counts as one (sizes are MSS multiples)
        if (!dupack)
            F.dupacks[f] = 0;
        uint8_t cg = F.cong[f];
        if (ack < F.recover[f] && cg == C_RECOVERY)
        {
            // partial ACK: the head is lost; retransmit it (MarkHeadAsLost has cleared its
            // retransmitted flag, so IsRetransmittedDataAcked never holds without SACK)
            sb_mark_head_lost(F, f);
            do_retransmit(c, f, now);
            pkts_acked(c, f, 1, ack, now);
            new_ack(c, f, ack, F.first_partial[f], now);
            F.first_partial[f] = 0;
        }
        else if (ack < F.recover[f] && cg == C_LOSS)
        {
            pkts_acked(c, f, segs, ack, now);
            increase_window(c, f, segs, ack, now);
            new_ack(c, f, ack, true, now);
        }
        else if (cg == C_CWR)
        {
            pkts_acked(c, f, segs, ack, now);
            new_ack(c, f, ack, true, now);
        }
        else
        {
            bool exited = false;
            if (cg == C_OPEN)
            {
                pkts_acked(c, f, segs, ack, now);
            }
            else if (cg == C_DISORDER)
            {
                if (segs >= old_dup)
                    pkts_acked(c, f, segs - old_dup, ack, now);
                if (!dupack)
                    F.cong[f] = C_OPEN;
            }
            else if (cg == C_RECOVERY)
            {
                F.first_partial[f] = 1;
                pkts_acked(c, f, segs, ack, now);
                F.cong[f] = C_OPEN;
                exited = true;
                F.dupacks[f] = 0; // the (guessed) sacks stay on the scoreboard
            }
            else if (cg == C_LOSS)
            {
                // only the segments above the recovery point count, for PktsAcked and for
                // the window increase below
                F.first_partial[f] = 1;
                segs = (ack - F.recover[f]) / MSS;
                pkts_acked(c, f, segs, ack, now);
                F.cong[f] = C_OPEN;
            }
            if (ack >= F.recover[f])
                F.recover_active[f] = 0;
            if (exited)
            {
                new_ack(c, f, ack, true, now);
                F.cwnd[f] = F.ssthresh[f];
            }
            if (F.cong[f] == C_OPEN)
            {
                increase_window(c, f, segs, ack, now);
                new_ack(c, f, ack, true, now);
            }
        }
        if (F.una[f] >= F.size[f])
        {
            flow_done(c, f, now);
            return;
        }
    }
#ifdef NS_TRACE
    if (f == NS_TRACE)
        printf("cwnd-after %lld %u ssth %u cong %d\n", (long long)(now / 1000), F.cwnd[f], F.ssthresh[f], F.cong[f]);
#endif
    send_pending(c, f, now);
}

__device__ inline void on_rto(HostCtx &c, int f, int64_t now)
{
    const FlowSoA &F = c.s->fl;
    F.rto_at[f] = -1;
    if (F.st[f] != S_EST || F.una[f] >= F.size[f])
        return;
    atomicAdd(&c.s->ev[3], 1ull);
    uint32_t before = inflight(F, f);
    F.dupacks[f] = 0;
    // ResetRenoSack + SetSentListLost(resetSack): everything outstanding is lost
    F.sacked[f] = 0;
    F.lost_n[f] = sb_items(F, f);
    F.rtx_n[f] = 0;
    F.head_rx[f] = 0;
    F.recover[f] = F.high[f];
    F.recover_active[f] = 1;
    int64_t r = 2 * F.rto[f];
    F.rto[f] = r < 60000000000000ll ? r : 60000000000000ll;
    F.hist_lo[f] = F.high[f]; // m_history.clear()
    // the condition (state != LOSS || !IsHeadRetransmitted) always holds after SetSentListLost
    F.ssthresh[f] = ssthresh_for(c, f, before);
    if (c.cubic)
        cub_loss(F, f, now);
    F.cong[f] = C_LOSS;
    F.cwnd[f] = MSS;
    send_pending(c, f, now);
}

// SendEmptyPacket(SYN|ACK) at the receiver: every SYN-ACK backs off the connection timeout;
// a retransmission is scheduled unless one is pending (cancelled on ESTABLISHED)
__device__ inline void send_synack(HostCtx &c, int f, int64_t now)
{
    const FlowSoA &F = c.s->fl;
    int64_t rto = CONN_TIMEOUT_PS << F.srv_syn_n[f];
    F.srv_syn_n[f]++;
    emit(c, f, F_SYN | F_ACK, 0, 0, 0, SYN_FRAME, true, now);
    if (F.srv_syn_at[f] < 0)
    {
        F.srv_syn_at[f] = now + rto;
        timer_chain(c, F.dchain, f, rto);
    }
}

__device__ inline void rcv_established(HostCtx &c, int f)
{
    const FlowSoA &F = c.s->fl;
    if (!F.r_est[f])
    {
        F.r_est[f] = 1;
        F.srv_syn_at[f] = -1; // m_retxEvent.Cancel()
    }
}

// DCTCP receiver CE state machine (TcpDctcp::CeState0to1 / CeState1to0)
__device__ inline void ce_state(HostCtx &c, int f, bool ce_now, int64_t now)
{
    const FlowSoA &F = c.s->fl;
    if (ce_now)
    {
        if (!F.ce_state[f] && F.reserved[f] && F.prior_flag[f])
            send_ack(c, f, F.prior_rcv_nxt[f], false, true, now); // previous ACK without ECE
        F.ce_state[f] = 1;
        F.ecn_r[f] = E_CE_RCVD;
    }
    else
    {
        if (F.ce_state[f] && F.reserved[f] && F.prior_flag[f])
            send_ack(c, f, F.prior_rcv_nxt[f], true, false, now); // previous ACK with ECE
        F.ce_state[f] = 0;
        if (F.ecn_r[f] == E_CE_RCVD || F.ecn_r[f] == E_SENDING_ECE)
            F.ecn_r[f] = E_IDLE;
    }
    F.prior_flag[f] = 1;
    F.prior_rcv_nxt[f] = F.rcv_nxt[f];
}

// TcpSocketBase::ReceivedData (+ the ECN processing that precedes it)
// TcpSocketBase::ForwardUp at the receiving socket: CE is valid only above the highest CE
// sequence number seen. The pure ACK of the handshake carries the sequence number of the first
// data segment, so a CE mark on it hides a CE mark on that segment.
__device__ inline void rx_ecn(HostCtx &c, int f, const Frame &p, int64_t now)
{
    const FlowSoA &F = c.s->fl;
    if (!c.dctcp)
        return;
    bool ce = p.flags & F_CE;
    if (ce && F.ecn_ce_seq[f] < (int64_t)p.sa)
    {
        F.ecn_ce_seq[f] = p.sa;
        F.ecn_r[f] = E_CE_RCVD;
        ce_state(c, f, true, now);
    }
    else if (p.flags & F_ECT)
    {
        ce_state(c, f, false, now);
    }
}

// the same at the sending socket, for ACKs: the first CE-marked ACK (the receiver's sequence
// number stays 1) sets the sender's ECN state to CE_RCVD, later ECT ACKs reset it to IDLE
// (TcpDctcp::CeState0to1 / CeState1to0; the sender never has a delayed ACK reserved)
__device__ inline void snd_ecn(HostCtx &c, int f, const Frame &p)
{
    const FlowSoA &F = c.s->fl;
    if (!c.dctcp)
        return;
    if ((p.flags & F_CE) && !(F.s_ce[f] & 1))
    {
        F.s_ce[f] = 3;
        F.ecn_s[f] = E_CE_RCVD;
    }
    else if (p.flags & F_ECT)
    {
        F.s_ce[f] &= ~2;
        if (F.ecn_s[f] == E_CE_RCVD || F.ecn_s[f] == E_SENDING_ECE)
            F.ecn_s[f] = E_IDLE;
    }
}

__device__ inline void on_data(HostCtx &c, int f, const Frame &p, int64_t now)
{
    const FlowSoA &F = c.s->fl;
    if (!F.r_up[f])
        return;
    rcv_established(c, f); // (data completes the handshake if its ACK was lost)
    // TcpSocketBase::IsValidTcpSegment: data entirely below rcv_nxt (OutOfRange) is answered
    // with a plain ACK before any ECN processing
    if (p.sa + payload_of(p) < F.rcv_nxt[f])
    {
        send_ack(c, f, F.rcv_nxt[f], false, true, now);
        return;
    }
#ifdef NS_TRACE
    if (f == NS_TRACE)
        printf("srv-rx %lld seq %u len %u flags %d delack_count %d delack_at %lld\n", (long long)now, p.sa + 1, payload_of(p), p.flags,
               F.delack_count[f], (long long)F.delack_at[f]);
#endif
    rx_ecn(c, f, p, now);
    // DoForwardUp: the sender has reacted to the echo; stop echoing unless CE just arrived
    if ((p.flags & F_CWR) && F.ecn_r[f] != E_CE_RCVD)
        F.ecn_r[f] = E_IDLE;
    uint32_t seq = p.sa, end = p.sa + payload_of(p);
    uint32_t expected = F.rcv_nxt[f];
    if (end <= expected)
    {
        send_ack(c, f, F.rcv_nxt[f], false, false, now); // duplicate: insert fails, ACK now
        return;
    }
    // out-of-order data: a bitmap ring of segments beyond rcv_nxt (segments are MSS-aligned)
    const uint32_t OW = c.s->fl.ooo_w[f];
    uint32_t *bm = c.s->fl.ooo_bm + c.s->fl.ooo_off[f];
    uint32_t n = F.ooo_n[f];
    if (seq <= expected)
    {
        uint32_t nxt = end, size = F.size[f];
        while (n > 0)
        {
            uint32_t sg = nxt / MSS;
            uint32_t &w = bm[(sg / 32) % OW];
            uint32_t bit = 1u << (sg % 32);
            if (!(w & bit))
                break;
            w &= ~bit;
            n--;
            nxt = nxt + MSS < size ? nxt + MSS : size;
        }
        F.rcv_nxt[f] = nxt;
    }
    else
    {
        uint32_t sg = seq / MSS;
        if (sg - expected / MSS < OW * 32)
        {
            uint32_t &w = bm[(sg / 32) % OW];
            uint32_t bit = 1u << (sg % 32);
            if (!(w & bit))
            {
                w |= bit;
                n++;
            }
        }
        else
        {
            atomicAdd(&c.s->ev[6], 1ull);
        }
    }
    F.ooo_n[f] = n;
    bool gap = n > 0;
    bool filled = F.rcv_nxt[f] > expected + payload_of(p);
    if (gap || filled)
    {
        F.reserved[f] = 0; // CA_EVENT_NON_DELAYED_ACK
        send_ack(c, f, F.rcv_nxt[f], false, false, now);
        return;
    }
    uint32_t cnt = F.delack_count[f] + 1u;
    if (cnt >= DELACK_COUNT)
    {
        F.reserved[f] = 0;
        send_ack(c, f, F.rcv_nxt[f], false, false, now);
    }
    else
    {
        F.delack_count[f] = cnt;
        F.reserved[f] = 1; // CA_EVENT_DELAYED_ACK
        if (F.delack_at[f] < 0)
        {
            F.delack_at[f] = now + DELACK_PS;
            timer_chain(c, c.s->fl.dchain, f, DELACK_PS);
        }
    }
}

__device__ inline void on_delack(HostCtx &c, int f, int64_t now)
{
    const FlowSoA &F = c.s->fl;
    F.delack_at[f] = -1;
    F.delack_count[f] = 0;
    F.reserved[f] = 1;
    send_ack(c, f, F.rcv_nxt[f], false, false, now);
}

__device__ inline void add_active(const Sim &s, int *list, int *n, int f, int h)
{
    int k = n[h];
    if (k >= MAX_ACTIVE)
    {
        atomicAdd(&s.ev[5], 1ull);
        return;
    }
    list[(size_t)h * MAX_ACTIVE + k] = f;
    n[h] = k + 1;
}

__device__ inline void on_frame(HostCtx &c, const Frame &p, int64_t now)
{
    const Sim &s = *c.s;
    const FlowSoA &F = s.fl;
    int f = (int)p.flow;
    if (p.to_sender)
    {
        if (p.flags & F_SYN)
        {
            // SYN-ACK: established; ACK it and start sending
            if (F.st[f] == S_SYN_SENT)
            {
                F.st[f] = S_EST;
                F.est[f] = now;
                F.syn_at[f] = -1;
                // the first RTT sample, from the first SYN even if it was retransmitted: ns-3's
                // UpdateRttHistory cannot mark the zero-length SYN entry as retransmitted, so a
                // SYN timeout yields a ~10 ms sample (and a ~30 ms RTO)
                rtt_sample(F, f, now - F.timing_time[f]);
                emit(c, f, F_ACK, 0, 0, 0, CTRL_FRAME, false, now);
                // ConnectionSucceeded (ScheduleNow) lets the application write; Socket::Send
                // schedules SendPendingData one time step (1 ps) later
                F.syn_at[f] = now + 1;
                due_insert(c, now + 1, f, 2);
            }
        }
        else
        {
            snd_ecn(c, f, p);
            on_ack(c, f, p.sa, p.flags & F_ECE, now);
        }
    }
    else if (p.flags & F_SYN)
    {
        if (!F.r_up[f])
        {
            F.r_up[f] = 1;
            // ns-3 enters ESTABLISHED with the delayed-ACK count at its maximum, so the first
            // data segment is acknowledged at once
            F.delack_count[f] = DELACK_COUNT;
            add_active(s, s.hs.act_rcv, s.hs.n_rcv, f, c.h);
        }
        if (F.r_est[f])
            return; // ESTABLISHED: a duplicate SYN
        // the SYN-ACK is sent from CompleteFork, which the SYN's arrival schedules (ScheduleNow)
        c.nic->woke = now;
        uint16_t parent[CHAIN];
        for (int i = 0; i < CHAIN; i++)
            parent[i] = c.echain[i];
        chain_push(c.echain, CHAIN_NOW, parent);
        send_synack(c, f, now);
    }
    else if (p.flags & F_DATA)
    {
        on_data(c, f, p, now);
    }
    else if (F.r_up[f])
    {
        rx_ecn(c, f, p, now); // the pure ACK that completes the handshake
        rcv_established(c, f);
    }
}



// One slot of host h: all of its events in [slot start, slot end) in timestamp order.
// Returns 1 if the host had anything to do.
// one slot of host h; returns 1 if it sent or holds frames; *wake: the next slot it must run
__device__ inline int host_step(const Sim &s, int h, int64_t t, Due *due, int64_t *wake)
{
    const int64_t S0 = t * SLOT_PS, E0 = S0 + SLOT_PS;
    const HostSoA &hs = s.hs;
    const FlowSoA &F = s.fl;
    // received frames: the tail of slot t-2's message arriving from S0 on, then the head
    // of slot t-1's (same link, so in arrival order)
    int link = s.host_in_link[h];
    const Frame *m2 = nullptr, *m1 = nullptr;
    int n2 = 0, n1 = 0, i2 = 0;
    if (t >= 2)
    {
        n2 = msg_count(s, t - 2, link);
        m2 = s.msg + (size_t)mi(s, t - 2, link) * MAX_TX;
        while (i2 < n2 && m2[i2].ts < S0)
            i2++;
    }
    if (t >= 1)
    {
        n1 = msg_count(s, t - 1, link);
        m1 = s.msg + (size_t)mi(s, t - 1, link) * MAX_TX;
    }
    int i1 = 0;
    bool input = i2 < n2 || (n1 > 0 && m1[0].ts < E0);
    int out_slot = (int)mi(s, t, h);
    if (!input && t < hs.wake[h] && hs.q_size[h] == 0)
    {
        *wake = hs.wake[h];
        return 0; // (no message: the ring entry keeps an older slot's tag and reads as empty)
    }
    Fifo nic;
    nic.ring = hs.ring + (size_t)h * s.ring;
    nic.head = hs.q_head[h];
    nic.size = hs.q_size[h];
    nic.link_free = hs.link_free[h];
    nic.last_chain = hs.last_chain + (size_t)h * CHAIN;
    nic.id = h;
    nic.last_start = hs.last_start[h];
    nic.out = s.msg + (size_t)out_slot * MAX_TX;
    nic.sent = 0;
    nic.slot_end = E0;
    nic.s = &s;
    nic.red_k = hs.red_k[h];
    nic.qcap = hs.qcap[h];
    HostCtx c{&s, &nic, h, hs.cc[h] == CC_DCTCP, hs.cc[h] == CC_CUBIC, {}, due, nullptr, nullptr, E0};

    // timers due in this slot (new timers are always set >= 10 us ahead, so none can become
    // due during the slot; cancelled or moved ones are skipped when their turn comes)
    int nd = 0;
    int ns = hs.n_snd[h], nr = hs.n_rcv[h];
    for (int i = 0; i < ns; i++)
    {
        int f = hs.act_snd[(size_t)h * MAX_ACTIVE + i];
        int64_t a = F.rto_at[f], b = F.syn_at[f];
        if (a >= 0 && a < E0 && nd < MAX_DUE)
            due[nd++] = {a, f, 0};
        if (b >= 0 && b < E0 && nd < MAX_DUE)
            due[nd++] = {b, f, 2};
    }
    for (int i = 0; i < nr; i++)
    {
        int f = hs.act_rcv[(size_t)h * MAX_ACTIVE + i];
        int64_t a = F.delack_at[f], b = F.srv_syn_at[f];
        if (a >= 0 && a < E0 && nd < MAX_DUE)
            due[nd++] = {a, f, 1};
        if (b >= 0 && b < E0 && nd < MAX_DUE)
            due[nd++] = {b, f, 3};
    }
    if (nd >= MAX_DUE)
        atomicAdd(&s.ev[7], 1ull);
    for (int i = 1; i < nd; i++) // insertion sort by time
    {
        Due x = due[i];
        int j = i - 1;
        while (j >= 0 && due[j].at > x.at)
        {
            due[j + 1] = due[j];
            j--;
        }
        due[j + 1] = x;
    }
    int id = 0;
    c.nd = &nd;
    c.id = &id;
    int pn = hs.pend_next[h], pe = hs.pend_end[h];

    for (;;)
    {
        int64_t tr = INT64_MAX, td = INT64_MAX, tp = INT64_MAX;
        if (i2 < n2)
            tr = m2[i2].ts;
        else if (i1 < n1 && m1[i1].ts < E0)
            tr = m1[i1].ts;
        if (id < nd)
            td = due[id].at;
        if (pn < pe)
        {
            int64_t st = F.start[hs.pend_flow[pn]];
            if (st < E0)
                tp = st;
        }
        int64_t now = tr < td ? tr : td;
        now = now < tp ? now : tp;
        if (now == INT64_MAX)
            break;
        if (now < S0)
            now = S0; // a flow start or timer from a skipped slot range (not expected)
        nic.advance(now);
        if (now == tp)
        {
            int f = hs.pend_flow[pn];
            chain_root(c.echain, h, f); // application starts are scheduled at setup, host by host
            pn++;
            add_active(s, hs.act_snd, hs.n_snd, f, h);
            F.st[f] = S_SYN_SENT;
            if (c.cubic)
                cub_hystart_reset(F, f, now); // TcpCubic::Init when the socket is created
            F.rto[f] = CONN_TIMEOUT_PS; // TcpSocketBase::SendEmptyPacket(SYN)
            F.syn_at[f] = now + CONN_TIMEOUT_PS;
            timer_chain(c, F.rchain, f, CONN_TIMEOUT_PS);
            F.timing_time[f] = now;
            emit(c, f, F_SYN, 0, 0, 0, SYN_FRAME, false, now);
        }
        else if (now == td)
        {
            Due d = due[id++];
            if (d.kind == 2 && F.st[d.flow] == S_EST)
                chain_push(c.echain, CHAIN_STEP, nullptr); // (its parent does not matter, see below)
            else
            {
                const uint16_t *tc = (d.kind == 1 || d.kind == 3 ? F.dchain : F.rchain) + (size_t)d.flow * CHAIN;
                for (int i = 0; i < CHAIN; i++)
                    c.echain[i] = tc[i];
            }
            if (d.kind == 0 && F.rto_at[d.flow] == d.at)
                on_rto(c, d.flow, now);
            else if (d.kind == 1 && F.delack_at[d.flow] == d.at)
                on_delack(c, d.flow, now);
            else if (d.kind == 3 && F.srv_syn_at[d.flow] == d.at && !F.r_est[d.flow])
            {
                F.srv_syn_at[d.flow] = -1; // fired
                send_synack(c, d.flow, now);
            }
            else if (d.kind == 2 && F.syn_at[d.flow] == d.at && F.st[d.flow] == S_EST)
            {
                // the first data: SendPendingData, deferred by Socket::Send. The burst starts
                // behind the handshake ACK (a TransmitComplete), so this event's own place in
                // the event order does not matter.
                F.syn_at[d.flow] = -1;
                send_pending(c, d.flow, now);
            }
            else if (d.kind == 2 && F.syn_at[d.flow] == d.at && F.st[d.flow] == S_SYN_SENT)
            {
                // SYN retransmission with exponential backoff of the connection timeout
                int tries = ++F.syn_tries[d.flow];
                F.rto[d.flow] = CONN_TIMEOUT_PS << tries;
                F.syn_at[d.flow] = now + F.rto[d.flow];
                timer_chain(c, F.rchain, d.flow, F.rto[d.flow]);
                emit(c, d.flow, F_SYN, 0, 0, 0, SYN_FRAME, false, now);
            }
        }
        else
        {
            Frame p = i2 < n2 ? m2[i2++] : m1[i1++];
            chain_push(c.echain, chain_arrival(p.len), p.chain);
            on_frame(c, p, now);
        }
    }
    nic.advance(E0 - 1);
    msg_set(s, t, h, nic.sent);
    hs.q_head[h] = nic.head;
    hs.q_size[h] = nic.size;
    hs.link_free[h] = nic.link_free;
    hs.last_start[h] = nic.last_start;
    hs.pend_next[h] = pn;

    // drop finished flows from the active lists; compute the next wake-up
    int64_t next = INT64_MAX;
    {
        int k = 0;
        for (int i = 0; i < ns; i++)
        {
            int f = hs.act_snd[(size_t)h * MAX_ACTIVE + i];
            if (F.st[f] == S_DONE)
                continue;
            hs.act_snd[(size_t)h * MAX_ACTIVE + k++] = f;
            int64_t a = F.rto_at[f], b = F.syn_at[f];
            if (a >= 0 && a < next)
                next = a;
            if (b >= 0 && b < next)
                next = b;
        }
        // flows added during this slot sit after index ns
        for (int i = ns; i < hs.n_snd[h]; i++)
        {
            int f = hs.act_snd[(size_t)h * MAX_ACTIVE + i];
            if (F.st[f] == S_DONE)
                continue;
            hs.act_snd[(size_t)h * MAX_ACTIVE + k++] = f;
            int64_t a = F.rto_at[f], b = F.syn_at[f];
            if (a >= 0 && a < next)
                next = a;
            if (b >= 0 && b < next)
                next = b;
        }
        hs.n_snd[h] = k;
    }
    {
        int k = 0;
        int nr2 = hs.n_rcv[h];
        for (int i = 0; i < nr2; i++)
        {
            int f = hs.act_rcv[(size_t)h * MAX_ACTIVE + i];
            if (F.rcv_nxt[f] >= F.size[f] && F.delack_at[f] < 0 && F.st[f] == S_DONE)
                continue;
            hs.act_rcv[(size_t)h * MAX_ACTIVE + k++] = f;
            int64_t a = F.delack_at[f], b = F.srv_syn_at[f];
            if (a >= 0 && a < next)
                next = a;
            if (b >= 0 && b < next)
                next = b;
        }
        hs.n_rcv[h] = k;
    }
    if (pn < pe)
    {
        int64_t st = F.start[hs.pend_flow[pn]];
        if (st < next)
            next = st;
    }
    hs.wake[h] = nic.size > 0 ? t + 1 : (next == INT64_MAX ? INT64_MAX : next / SLOT_PS);
    *wake = hs.wake[h];
    return (nic.sent > 0 || nic.size > 0) ? 1 : 0;
}

// ------------------------------------------------------------------ switches
__device__ inline uint32_t mix32(uint32_t x)
{
    x ^= x >> 16;
    x *= 0x7feb352du;
    x ^= x >> 15;
    x *= 0x846ca68bu;
    x ^= x >> 16;
    return x;
}

// Output port at switch sw (FatTree: edges, aggregations, cores) for a frame to host dst.
// Upward ports follow ns-3's per-flow ECMP: route index = flow hash % (k/2), the same hash at
// every switch (Ipv4GlobalRouting uses one 5-tuple hash), equal-cost routes in interface order.
__device__ inline int route(const Sim &s, int sw, const Frame &p)
{
    int dst = p.dst_node % s.H1; // within the world
    sw %= s.S1;
    if (s.topo == TOPO_LEAFSPINE)
    {
        // leaf: own hosts on ports 0..hpl-1, spine j on port hpl + j; spine: leaf l on port l
        if (sw < s.leaves)
            return dst / s.hpl == sw ? dst % s.hpl : s.hpl + p.up;
        return dst / s.hpl;
    }
    const int k = s.k, hk = s.hk, E = k * hk;
    int dst_pod = dst / (hk * hk), dst_edge = (dst / hk) % hk;
    if (sw < E)
    {
        int pod = sw / hk, j = sw % hk;
        if (dst_pod == pod && dst_edge == j)
            return dst % hk;
        return hk + p.up;
    }
    if (sw < 2 * E)
    {
        int pod = (sw - E) / hk;
        if (dst_pod == pod)
            return dst_edge;
        return hk + p.up;
    }
    return dst_pod;
}

struct SwScratch
{
    int *rows;        // [k][k+1] shared: counts / offsets ingress x egress
    int64_t *ts;      // [k * 2 * MAX_TX] (global scratch for this switch) arrival time
    uint32_t *ref;    // [k * 2 * MAX_TX] source: ingress port << 16 | index in the combined input
    int *base;        // [k] shared: region of each egress in ts/ref
    int *cnt;         // [k] shared
    int *in_win;      // [k][3] shared: each ingress port's input window (i2, n2, n1)
};

// The ingress frames of port q this slot: tail of msg[t-2] (ts >= S0), head of msg[t-1] (ts < E0).
struct Input
{
    const Frame *m2, *m1;
    int i2, n2, n1;
    __device__ int count() const { return (n2 - i2) + n1; }
    __device__ const Frame &at(int j) const { return j < n2 - i2 ? m2[i2 + j] : m1[j - (n2 - i2)]; }
};

__device__ inline Input input_of(const Sim &s, int link, int64_t t)
{
    const int64_t S0 = t * SLOT_PS, E0 = S0 + SLOT_PS;
    Input in{nullptr, nullptr, 0, 0, 0};
    if (link < 0)
        return in; // an unused port
    if (t >= 2)
    {
        in.n2 = msg_count(s, t - 2, link);
        in.m2 = s.msg + (size_t)mi(s, t - 2, link) * MAX_TX;
        while (in.i2 < in.n2 && in.m2[in.i2].ts < S0)
            in.i2++;
    }
    if (t >= 1)
    {
        int n = msg_count(s, t - 1, link);
        in.m1 = s.msg + (size_t)mi(s, t - 1, link) * MAX_TX;
        while (in.n1 < n && in.m1[in.n1].ts < E0)
            in.n1++;
    }
    return in;
}

// the input of ingress port iq of the switch, from the window cached in shared memory
__device__ inline Input input_cached(const Sim &s, const SwScratch &sc, int sw, int iq, int64_t t)
{
    int link = s.sw_in_link[sw * s.k + iq];
    if (link < 0)
        link = 0; // (an unused port has an empty window; any link gives valid pointers)
    Input in;
    in.i2 = sc.in_win[3 * iq];
    in.n2 = sc.in_win[3 * iq + 1];
    in.n1 = sc.in_win[3 * iq + 2];
    in.m2 = t >= 2 ? s.msg + (size_t)mi(s, t - 2, link) * MAX_TX : nullptr;
    in.m1 = t >= 1 ? s.msg + (size_t)mi(s, t - 1, link) * MAX_TX : nullptr;
    return in;
}

// One slot of switch sw, port q (all ports of the switch call it together).
__device__ inline int switch_step(const Sim &s, int sw, int q, int64_t t, bool active, SwScratch sc)
{
    const int k = s.k;
    const int64_t S0 = t * SLOT_PS, E0 = S0 + SLOT_PS;
    int port = sw * k + q;
    Input in{nullptr, nullptr, 0, 0, 0};
    int *my_row = sc.rows + q * (k + 1);
    if (active)
    {
        in = input_of(s, s.sw_in_link[port], t);
        sc.in_win[3 * q] = in.i2;
        sc.in_win[3 * q + 1] = in.n2;
        sc.in_win[3 * q + 2] = in.n1;
        for (int i = 0; i <= k; i++)
            my_row[i] = 0;
        int n = in.count();
        for (int j = 0; j < n; j++)
            my_row[route(s, sw, in.at(j))]++;
    }
    __syncthreads();
    if (active)
    {
        int off = 0;
        for (int j = 0; j < k; j++)
        {
            int c = sc.rows[j * (k + 1) + q];
            sc.rows[j * (k + 1) + q] = off;
            off += c;
        }
        sc.cnt[q] = off;
    }
    __syncthreads();
    if (active)
    {
        int b = 0;
        for (int j = 0; j < q; j++)
            b += sc.cnt[j];
        sc.base[q] = b;
    }
    __syncthreads();
    if (active)
    {
        int n = in.count();
        for (int j = 0; j < n; j++)
        {
            const Frame &p = in.at(j);
            int e = route(s, sw, p);
            int r = my_row[e]++;
            int at = sc.base[e] + r;
            sc.ts[at] = p.ts;
            sc.ref[at] = (uint32_t)(q << 16 | j);
        }
    }
    __syncthreads();
    if (!active)
        return 0;
    // egress q: arrivals in time order (ties as above), then the FIFO
    int n = sc.cnt[q], b0 = sc.base[q];
    int64_t *ts = sc.ts + b0;
    uint32_t *ref = sc.ref + b0;
    // ties: ns-3 runs simultaneous events in scheduling order; an arrival event's chain is
    // [arrival(len)] + the upstream transmission start's chain
    auto frame_at = [&](uint32_t r) -> const Frame & {
        Input src = input_cached(s, sc, sw, (int)(r >> 16), t);
        return src.at(r & 0xFFFF);
    };
    auto before = [&](uint32_t ra, uint32_t rb) {
        const Frame &a = frame_at(ra), &b = frame_at(rb);
        if (a.len != b.len)
            return a.len > b.len; // the longer frame started earlier upstream
        int c = chain_cmp(a.chain, b.chain);
        return c != 0 ? c < 0 : a.flow < b.flow;
    };
    for (int i = 1; i < n; i++)
    {
        int64_t x = ts[i];
        uint32_t r = ref[i];
        int j = i - 1;
        while (j >= 0 && (ts[j] > x || (ts[j] == x && before(r, ref[j]))))
        {
            ts[j + 1] = ts[j];
            ref[j + 1] = ref[j];
            j--;
        }
        ts[j + 1] = x;
        ref[j + 1] = r;
    }
    Fifo eg;
    eg.ring = s.ps.ring + (size_t)port * s.ring;
    eg.head = s.ps.q_head[port];
    eg.size = s.ps.q_size[port];
    eg.link_free = s.ps.link_free[port];
    eg.red_k = s.ps.red_k[port];
    eg.qcap = s.ps.qcap[port];
    eg.last_chain = s.ps.last_chain + (size_t)port * CHAIN;
    eg.id = s.H + port;
    eg.last_start = s.ps.last_start[port];
    int out_link = s.H + port;
    eg.out = s.msg + (size_t)mi(s, t, out_link) * MAX_TX;
    eg.sent = 0;
    eg.slot_end = E0;
    eg.s = &s;
    for (int i = 0; i < n; i++)
    {
        Frame p = frame_at(ref[i]);
        uint16_t up[CHAIN];
        for (int i = 0; i < CHAIN; i++)
            up[i] = p.chain[i];
        chain_push(p.chain, chain_arrival(p.len), up); // queued: the arrival event's chain
        eg.enqueue(p, p.ts);
    }
    eg.advance(E0 - 1);
    msg_set(s, t, out_link, eg.sent);
    s.ps.q_head[port] = eg.head;
    s.ps.q_size[port] = eg.size;
    s.ps.link_free[port] = eg.link_free;
    s.ps.last_start[port] = eg.last_start;
    return (eg.sent > 0 || eg.size > 0) ? 1 : 0;
}

} // namespace nsm

#endif
