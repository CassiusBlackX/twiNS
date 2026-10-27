// TwiNS device code: egress FIFO with RED, host TCP (ns-3 TcpSocketBase semantics),
// switch forwarding. See model.h for the execution model.
#ifndef TWINS_STEP_CUH
#define TWINS_STEP_CUH

#include "model.h"
#include "nsmath.h"
#include "rng.h"

namespace nsm
{

__device__ inline void frame_copy(Frame *d, const Frame *s)
{
    const uint4 *a = reinterpret_cast<const uint4 *>(s);
    uint4 *b = reinterpret_cast<uint4 *>(d);
    constexpr int N = sizeof(Frame) / 16;
    uint4 v[N];
    for (int i = 0; i < N; i++)
        v[i] = a[i];
    for (int i = 0; i < N; i++)
        b[i] = v[i];
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
// codes), then the root, an event scheduled before the simulation runs, as a 32-bit key in
// ns-3's scheduling order: the message writes the scenario schedules in main(), by message (key
// 1 + index), then the application starts, which the nodes schedule as they initialize, by
// host and connection (key 0x8000 + host + 1, then the connection's low 16 bits; host is
// world-local). When the runs overflow the oldest are dropped; the root is kept.
__device__ inline void chain_root(uint16_t *dst, int host_local, int conn)
{
    for (int i = 0; i < 2 * CHAIN_RUNS; i++)
        dst[i] = 0;
    dst[2 * CHAIN_RUNS] = (uint16_t)(0x8000 | (host_local + 1));
    dst[2 * CHAIN_RUNS + 1] = (uint16_t)conn;
}

__device__ inline void chain_root_msg(uint16_t *dst, int msg_local)
{
    for (int i = 0; i < 2 * CHAIN_RUNS; i++)
        dst[i] = 0;
    dst[2 * CHAIN_RUNS] = (uint16_t)(1 + (msg_local >> 16));
    dst[2 * CHAIN_RUNS + 1] = (uint16_t)msg_local;
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
    int kind; // 0 rto, 1 delack, 2 syn (or the deferred SendPendingData of an established
              // connection), 3 the receiver's SYN-ACK retransmission, 4 LastAckTimeout, 5 the
              // receiver's FIN+ACK retransmission
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

// ---- TcpTxBuffer without SACK (Reno emulation), as ns-3 implements it: the sent list is a ring
// of items (start sequence number and TcpTxItem flags; an item ends where the next starts, the
// last at high), with the byte counters m_sackedOut, m_lostOut and m_retrans. Retransmissions
// may merge an item with its successor and split the result (GetTransmittedSegment).
//
// Tx is a connection's view of its rings (pointers and capacity loaded once); head and n are
// the sent list's first slot and length as the operation found them.
struct Tx
{
    const FlowSoA &F;
    int f;
    uint32_t cap;
    uint32_t *st;
    uint8_t *fl;
    uint32_t head, n;
    __device__ Tx(const FlowSoA &F_, int f_) : F(F_), f(f_)
    {
        size_t o = F.it_off[f];
        cap = F.it_cap[f];
        st = F.it_start + o;
        fl = F.it_fl + o;
        head = F.it_head[f];
        n = F.it_n[f];
    }
    // ring slot of item i (head < cap, i <= cap: no division)
    __device__ uint32_t at(uint32_t i) const
    {
        uint32_t x = head + i;
        return x >= cap ? x - cap : x;
    }
    __device__ uint32_t beg(uint32_t i) const { return st[at(i)]; }
    __device__ uint32_t end(uint32_t i) const { return i + 1 < n ? st[at(i + 1)] : F.high[f]; }
    __device__ uint8_t flags(uint32_t i) const { return fl[at(i)]; }
    __device__ void save() const
    {
        F.it_head[f] = head;
        F.it_n[f] = n;
    }
};

__device__ inline bool it_cand(uint8_t fl) { return (fl & (IT_LOST | IT_RETX | IT_SACKED)) == IT_LOST; }

// set item i's flags, keeping the byte counters, the sacked count and the NextSeg cursor
__device__ inline void it_set(const Tx &t, uint32_t i, uint8_t nf)
{
    const FlowSoA &F = t.F;
    const int f = t.f;
    uint32_t k = t.at(i);
    uint8_t of = t.fl[k];
    if (of == nf)
        return;
    uint32_t len = t.end(i) - t.st[k];
    uint8_t d = of ^ nf;
    if (d & IT_SACKED)
    {
        F.sack_b[f] += (nf & IT_SACKED) ? len : 0u - len;
        F.sacked_n[f] += (nf & IT_SACKED) ? 1u : 0u - 1u;
    }
    if (d & IT_LOST)
        F.lost_b[f] += (nf & IT_LOST) ? len : 0u - len;
    if (d & IT_RETX)
        F.rtx_b[f] += (nf & IT_RETX) ? len : 0u - len;
    if (it_cand(of) != it_cand(nf))
    {
        if (it_cand(nf))
        {
            F.cand_n[f]++;
            if (i < F.cand[f])
                F.cand[f] = i;
        }
        else
            F.cand_n[f]--;
    }
    t.fl[k] = nf;
}

// insert an item at index i (start, flags; the byte counters do not change: an item is split,
// or a new one appended before high moves)
__device__ inline bool it_insert(const Sim &s, Tx &t, uint32_t i, uint32_t start, uint8_t fl)
{
    const FlowSoA &F = t.F;
    const int f = t.f;
    if (t.n >= t.cap)
    {
        atomicAdd(&s.ev[14], 1ull); // the sent list is full
        return false;
    }
    for (uint32_t j = t.n; j > i; j--)
    {
        uint32_t a = t.at(j), b = t.at(j - 1);
        t.st[a] = t.st[b];
        t.fl[a] = t.fl[b];
    }
    uint32_t k = t.at(i);
    t.st[k] = start;
    t.fl[k] = fl;
    t.n++;
    F.it_n[f] = t.n;
    if (F.cand[f] >= i && F.cand[f] > 0)
        F.cand[f]++;
    if (it_cand(fl))
    {
        F.cand_n[f]++;
        if (i < F.cand[f])
            F.cand[f] = i;
    }
    return true;
}

// remove the start of item i, merging it into item i - 1 (flags reconciled by the caller)
__device__ __noinline__ void it_remove_start(Tx &t, uint32_t i)
{
    for (uint32_t j = i; j + 1 < t.n; j++)
    {
        uint32_t a = t.at(j), b = t.at(j + 1);
        t.st[a] = t.st[b];
        t.fl[a] = t.fl[b];
    }
    t.n--;
    t.F.it_n[t.f] = t.n;
    if (t.F.cand[t.f] > i)
        t.F.cand[t.f]--;
}

// the first item starting at or after seq (the items are sorted)
__device__ inline uint32_t it_lower(const Tx &t, uint32_t seq)
{
    uint32_t lo = 0, hi = t.n;
    while (lo < hi)
    {
        uint32_t m = (lo + hi) / 2;
        if (t.beg(m) < seq)
            lo = m + 1;
        else
            hi = m;
    }
    return lo;
}

__device__ inline uint32_t tb_sent(const FlowSoA &F, int f) { return F.it_n[f] ? F.high[f] - F.una[f] : 0u; }

// TcpTxBuffer::BytesInFlight = sent - (sacked + lost) + retrans (unsigned, as in ns-3)
__device__ inline uint32_t inflight(const FlowSoA &F, int f)
{
    return tb_sent(F, f) - (F.sack_b[f] + F.lost_b[f]) + F.rtx_b[f];
}

// TcpTxBuffer::AddRenoSack: sack the first unsacked item after the head
__device__ inline void tb_add_reno_sack(const Tx &t)
{
    const FlowSoA &F = t.F;
    uint32_t i = 1 + F.sacked_n[t.f]; // (Reno emulation sacks items 1, 2, .. in order)
    while (i < t.n && (t.flags(i) & IT_SACKED))
        i++;
    if (i < t.n)
    {
        it_set(t, i, t.flags(i) | IT_SACKED);
        F.sack_seen[t.f] = 1;
        F.hisack[t.f] = t.beg(i);
    }
}
__device__ inline void tb_add_reno_sack(const FlowSoA &F, int f) { tb_add_reno_sack(Tx(F, f)); }

// TcpTxBuffer::MarkHeadAsLost
__device__ inline void tb_mark_head_lost(const Tx &t)
{
    if (t.n == 0)
        return;
    it_set(t, 0, (uint8_t)((t.flags(0) & ~(IT_SACKED | IT_RETX)) | IT_LOST));
}
__device__ inline void tb_mark_head_lost(const FlowSoA &F, int f) { tb_mark_head_lost(Tx(F, f)); }

// TcpTxBuffer::ResetRenoSack + SetSentListLost(resetSack = true): everything outstanding is lost
__device__ __noinline__ void tb_all_lost(const FlowSoA &F, int f)
{
    Tx t(F, f);
    for (uint32_t i = 0; i < t.n; i++)
        t.fl[t.at(i)] = IT_LOST;
    F.sack_b[f] = 0;
    F.sacked_n[f] = 0;
    F.rtx_b[f] = 0;
    F.lost_b[f] = tb_sent(F, f);
    F.cand_n[f] = t.n;
    F.cand[f] = 0;
    F.sack_seen[f] = 0;
    F.hisack[f] = 0;
}

// TcpTxBuffer::DiscardUpTo(ack): returns the bytes of the items removed whole (what the rate
// sampler counts as delivered, ProcessAck's currentDelivered)
__device__ inline uint32_t tb_discard(const FlowSoA &F, int f, uint32_t ack)
{
    Tx t(F, f);
    uint32_t delivered = 0, removed = 0;
    while (t.n > 0)
    {
        uint32_t k = t.at(0), b = t.st[k], e = t.end(0);
        if (ack >= e)
        {
            uint8_t fl = t.fl[k];
            if (fl)
                it_set(t, 0, 0); // RemoveFromCounts
            t.head = t.at(1);
            t.n--;
            removed++;
            delivered += e - b;
        }
        else
        {
            if (ack > b)
            {
                // part of the head is acknowledged: fragment it
                uint8_t fl = t.fl[k];
                uint32_t off = ack - b;
                if (fl & IT_SACKED)
                    F.sack_b[f] -= off;
                if (fl & IT_LOST)
                    F.lost_b[f] -= off;
                if (fl & IT_RETX)
                    F.rtx_b[f] -= off;
                t.st[k] = ack;
            }
            break;
        }
    }
    t.save();
    if (removed)
    {
        uint32_t c = F.cand[f];
        F.cand[f] = c > removed ? c - removed : 0;
    }
    F.una[f] = ack; // m_firstByteSeq (beyond high once a FIN is acknowledged)
    if (t.n > 0 && (t.flags(0) & IT_SACKED))
    {
        // a sacked head is our wrong guess: move the sack on and mark the head lost
        it_set(t, 0, (uint8_t)(t.flags(0) & ~IT_SACKED));
        tb_add_reno_sack(t);
        tb_mark_head_lost(t);
    }
    if (F.hisack[f] <= ack)
    {
        F.sack_seen[f] = 0;
        F.hisack[f] = 0;
    }
    return delivered;
}

// TcpTxBuffer::NextSeg without SACK (rules 1 and 2): the first lost item that was neither
// retransmitted nor sacked, below the highest sack; else new data
__device__ inline bool tb_next_seg(const FlowSoA &F, int f, uint32_t *seq)
{
    if (F.cand_n[f] > 0)
    {
        Tx t(F, f);
        for (uint32_t i = F.cand[f]; i < t.n; i++)
        {
            if (!it_cand(t.flags(i)))
                continue;
            F.cand[f] = i;
            uint32_t b = t.beg(i);
            if (!F.sack_seen[f] || b < F.hisack[f])
            {
                *seq = b;
                return true;
            }
            break;
        }
    }
    if (F.tail[f] > F.high[f])
    {
        *seq = F.high[f];
        return true;
    }
    return false;
}

// TcpTxBuffer::IsLost(seq) for seq = SND.UNA: the head, if below the highest sack
__device__ inline bool tb_head_lost(const FlowSoA &F, int f)
{
    if (!F.sack_seen[f] || F.una[f] >= F.hisack[f] || F.it_n[f] == 0)
        return false;
    Tx t(F, f);
    return t.flags(0) & IT_LOST;
}

// TcpTxBuffer::IsRetransmittedDataAcked(x): an unsacked retransmitted item ends at x
__device__ inline bool tb_retx_ends_at(const FlowSoA &F, int f, uint32_t x)
{
    if (F.it_n[f] == 0 || x <= F.una[f] || x > F.high[f])
        return false;
    Tx t(F, f);
    uint32_t i = it_lower(t, x) - 1; // the item containing x - 1
    if (t.end(i) != x)
        return false;
    uint8_t fl = t.flags(i);
    return !(fl & IT_SACKED) && (fl & IT_RETX);
}

// TcpTxBuffer::GetTransmittedSegment(numBytes, seq) for item i (starting at seq): the size to
// resend (merging with an unsacked successor of the same lost state), the list reshaped so that
// one item covers it, marked retransmitted
__device__ __noinline__ uint32_t tb_retransmit(const Sim &s, Tx &t, uint32_t i, uint32_t num)
{
    const FlowSoA &F = t.F;
    const int f = t.f;
    uint32_t len = t.end(i) - t.beg(i);
    uint32_t sz = num;
    if (i + 1 < t.n)
    {
        uint8_t a = t.flags(i), b = t.flags(i + 1);
        uint32_t nlen = t.end(i + 1) - t.beg(i + 1);
        if (!(b & IT_SACKED) && (a & IT_LOST) == (b & IT_LOST))
            sz = sz < len + nlen ? sz : len + nlen;
        else
            sz = sz < len ? sz : len;
    }
    else
        sz = sz < len ? sz : len;
    if (sz > len)
    {
        // MergeItems(i, i + 1): a retransmitted flag that only one of them has is cleared
        uint8_t a = t.flags(i), b = t.flags(i + 1);
        if ((a ^ b) & IT_RETX)
        {
            if (a & IT_RETX)
                it_set(t, i, a &= (uint8_t)~IT_RETX);
            else
                it_set(t, i + 1, b &= (uint8_t)~IT_RETX);
        }
        // the merged item keeps a's flags (equal to b's); counters are bytes, so they hold
        if (it_cand(b))
            F.cand_n[f]--;
        it_remove_start(t, i + 1);
        len = t.end(i) - t.beg(i);
    }
    if (sz < len)
    {
        // SplitItems: the first sz bytes become their own item, with the same flags
        it_insert(s, t, i + 1, t.beg(i) + sz, t.flags(i));
    }
    uint8_t fl = t.flags(i);
    if (!(fl & IT_RETX))
        it_set(t, i, fl | IT_RETX);
    return sz;
}

// TcpTxBuffer::CopyFromSequence(numBytes, seq): the size of the segment at seq and whether it is
// a retransmission; new data becomes a new item at the end of the sent list
__device__ inline uint32_t tb_copy(const Sim &s, int f, uint32_t num, uint32_t seq, bool *retx)
{
    const FlowSoA &F = s.fl;
    for (;;)
    {
        uint32_t avail = F.tail[f] > seq ? F.tail[f] - seq : 0;
        uint32_t sz = num < avail ? num : avail;
        if (sz == 0)
            return 0;
        uint32_t sent_end = F.high[f];
        Tx t(F, f);
        if (sent_end >= seq + sz)
        {
            uint32_t i = it_lower(t, seq);
            if (i >= t.n || t.beg(i) != seq)
            {
                atomicAdd(&s.ev[14], 1ull); // (not an item start: cannot happen)
                return 0;
            }
            *retx = true;
            return tb_retransmit(s, t, i, sz);
        }
        if (sent_end <= seq)
        {
            // GetNewSegment
            if (!it_insert(s, t, t.n, seq, 0))
                return 0;
            F.high[f] = seq + sz;
            *retx = false;
            return sz;
        }
        num = sent_end - seq; // partly sent: resend only the sent part
    }
}

// ---- RTT history (TcpSocketBase::m_history): first transmissions, in order; it shares the
// sent list's capacity
struct Hist
{
    const FlowSoA &F;
    int f;
    uint32_t cap;
    uint32_t *seq, *cnt;
    int64_t *t;
    uint32_t head, n;
    __device__ Hist(const FlowSoA &F_, int f_) : F(F_), f(f_)
    {
        size_t o = F.it_off[f];
        cap = F.it_cap[f];
        seq = F.h_seq + o;
        cnt = F.h_cnt + o;
        t = F.h_t + o;
        head = F.h_head[f];
        n = F.h_n[f];
    }
    __device__ uint32_t at(uint32_t j) const
    {
        uint32_t x = head + j;
        return x >= cap ? x - cap : x;
    }
};

// TcpSocketBase::UpdateRttHistory
__device__ inline void rtt_history(const Sim &s, int f, uint32_t seq, uint32_t sz, bool retx, int64_t now)
{
    const FlowSoA &F = s.fl;
    Hist h(F, f);
    if (!retx)
    {
        if (h.n >= h.cap)
        {
            atomicAdd(&s.ev[14], 1ull); // full
            return;
        }
        uint32_t k = h.at(h.n);
        h.seq[k] = seq;
        h.cnt[k] = sz;
        h.t[k] = now;
        F.h_n[f] = h.n + 1;
        return;
    }
    for (uint32_t j = 0; j < h.n; j++)
    {
        uint32_t k = h.at(j);
        uint32_t c = h.cnt[k] & 0x7FFFFFFFu;
        if (seq >= h.seq[k] && seq < h.seq[k] + c)
        {
            h.cnt[k] = 0x80000000u | (seq + sz - h.seq[k]); // retransmitted; the count may grow
            return;
        }
    }
}

// TcpSocketBase::EstimateRtt (data ACKs): a sample from the oldest entry if this ACK covers it
// and it was not retransmitted; covered entries are removed
__device__ inline void rtt_ack(const Sim &s, int f, uint32_t ack, int64_t now);


__device__ __noinline__ uint32_t cub_ssthresh(const FlowSoA &F, int f);

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

__device__ inline void rtt_ack(const Sim &s, int f, uint32_t ack, int64_t now)
{
    const FlowSoA &F = s.fl;
    if (F.h_n[f] == 0)
        return;
    Hist h(F, f);
    uint32_t k = h.at(0);
    if (!(h.cnt[k] & 0x80000000u) && ack >= h.seq[k] + h.cnt[k])
        rtt_sample(F, f, now - h.t[k]);
    while (h.n > 0)
    {
        k = h.at(0);
        if (h.seq[k] + (h.cnt[k] & 0x7FFFFFFFu) > ack)
            break;
        h.head = h.at(1);
        h.n--;
    }
    F.h_head[f] = h.head;
    F.h_n[f] = h.n;
}

// TcpSocketBase::SendDataPacket(seq, maxSize): returns the bytes sent
__device__ inline uint32_t send_segment(HostCtx &c, int f, uint32_t seq, uint32_t max_size, int64_t now)
{
    const FlowSoA &F = c.s->fl;
    bool retx = false;
    uint32_t len = tb_copy(*c.s, f, max_size, seq, &retx);
    if (len == 0)
        return 0;
#ifdef NS_TRACE
    if (f == NS_TRACE)
        printf("tx %lld %u%s st %d len %u\n", (long long)(now / 1000), seq + 1, retx ? " R" : "", F.cong[f], len);
#endif
    uint8_t flags = F_DATA;
    // the first new segment after an ECN echo carries CWR
    if (!retx && F.ecn_s[f] == E_ECE_RCVD && F.ecn_echo_seq[f] > F.ecn_cwr_seq[f])
    {
        F.ecn_s[f] = E_CWR_SENT;
        F.ecn_cwr_seq[f] = seq;
        flags |= F_CWR;
    }
    // Close() with data pending: the FIN rides on the segment that empties the buffer
    if ((F.fin[f] & FIN_CLOSE) && seq + len == F.tail[f])
    {
        flags |= F_FIN;
        F.fin[f] |= FIN_SENT;
    }
    emit(c, f, flags, seq, 0, (uint16_t)len, (uint16_t)(len + 42), false, now);
    if (retx)
    {
        atomicAdd(&c.s->ev[2], 1ull);
        atomicAdd(&c.s->ev[8 + F.cong[f]], 1ull);
    }
    if (F.rto_at[f] < 0)
    {
        F.rto_at[f] = now + F.rto[f];
        timer_chain(c, c.s->fl.rchain, f, F.rto[f]);
    }
    F.cwnd_limited[f] = F.cwnd[f] < inflight(F, f) + MSS; // m_isCwndLimited
    rtt_history(*c.s, f, seq, len, retx, now);
    if (seq + len > F.high[f])
        F.high[f] = seq + len;
    atomicMax(&c.s->ev[13], (unsigned long long)F.it_n[f]);
    return len;
}

// TcpSocketBase::SendPendingData
__device__ inline void send_pending(HostCtx &c, int f, int64_t now)
{
    const FlowSoA &F = c.s->fl;
    if (F.st[f] != S_EST)
        return;
    for (int guard = 0; guard < 4096; guard++)
    {
        if (F.cong[f] == C_OPEN && (F.fin[f] & (FIN_SENT | FIN_ACKED)) == FIN_SENT)
            return; // FIN_WAIT_1 and OPEN: no data to transmit
        uint32_t pipe = inflight(F, f);
        uint32_t cwnd = F.cwnd[f];
        uint32_t avail = cwnd > pipe ? cwnd - pipe : 0;
        if (avail == 0)
            return;
        uint32_t seq;
        if (!tb_next_seg(F, f, &seq))
            return;
        uint32_t remaining = F.tail[f] - seq; // TcpTxBuffer::SizeFromSequence
        if (avail < MSS && remaining > avail)
            return; // silly window (Nagle is off: TcpSocket::TcpNoDelay defaults to true)
        F.nxt[f] = seq;
        uint32_t len = send_segment(c, f, seq, avail < MSS ? avail : MSS, now);
        if (len == 0)
            return;
        F.nxt[f] = seq + len;
    }
}

// TcpSocketBase::DoRetransmit: the first lost segment (the head without SACK)
__device__ inline void do_retransmit(HostCtx &c, int f, int64_t now)
{
    const FlowSoA &F = c.s->fl;
    uint32_t seq;
    if (!tb_next_seg(F, f, &seq))
        seq = F.una[f];
    if (F.una[f] >= F.tail[f])
        return; // (an empty buffer: only a FIN could be resent)
    F.nxt[f] = seq;
    send_segment(c, f, seq, MSS, now);
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

__device__ __noinline__ uint32_t cub_update(const FlowSoA &F, int f, uint32_t segs, int64_t now)
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

__device__ __noinline__ void cub_pkts_acked(const FlowSoA &F, int f, int64_t now)
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

__device__ __noinline__ void cub_increase(const FlowSoA &F, int f, uint32_t segs, uint32_t ack, int64_t now)
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

__device__ __noinline__ uint32_t cub_ssthresh(const FlowSoA &F, int f)
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
__device__ __noinline__ void cub_loss(const FlowSoA &F, int f, int64_t now)
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
    // no retransmission timer once the buffer is empty, except in FIN_WAIT_1 (the FIN is
    // outstanding until ProcessWait moves on, after this ACK)
    if (ack >= F.tail[f] && !(F.fin[f] & FIN_SENT))
        F.rto_at[f] = -1;
    if (ack > F.nxt[f])
        F.nxt[f] = ack;
}

// TcpSocketBase::EnterRecovery (no SACK, classic recovery)
__device__ __noinline__ void enter_recovery(HostCtx &c, int f, int64_t now)
{
    const FlowSoA &F = c.s->fl;
    tb_add_reno_sack(F, f); // one segment has left the network
    tb_mark_head_lost(F, f);
    F.recover[f] = F.high[f];
    F.recover_active[f] = 1;
    F.cong[f] = C_RECOVERY;
    F.ssthresh[f] = ssthresh_for(c, f, inflight(F, f) + MSS); // the head still counts without SACK
    F.cwnd[f] = F.ssthresh[f];                                 // TcpClassicRecovery::EnterRecovery
    // the head goes out with SendDataPacket directly: m_nextTxSequence is not moved (unlike
    // DoRetransmit), which matters for DCTCP's observation window
    send_segment(c, f, F.una[f], MSS, now);
}

// the application's view: every message whose last byte is now acknowledged is complete
__device__ __noinline__ void messages_acked(HostCtx &c, int f, int64_t now)
{
    const FlowSoA &F = c.s->fl;
    const MsgSoA &M = c.s->mg;
    while (F.m_dn[f] < F.m_wr[f] && F.una[f] >= M.end[F.m_first[f] + F.m_dn[f]])
    {
        M.done[F.m_first[f] + F.m_dn[f]] = now;
        F.m_dn[f]++;
        atomicAdd(c.s->flows_done, 1u);
    }
    if (F.m_dn[f] == F.m_cnt[f] && (!F.fin_on[f] || (F.fin[f] & FIN_ACKED)))
    {
        F.st[f] = S_DONE;
        F.rto_at[f] = -1; // (a later timeout in FIN_WAIT_2 has nothing to do)
    }
}

// TcpSocketBase::ReceivedAck + ProcessAck (no SACK, classic recovery, no limited transmit)
__device__ inline void on_ack(HostCtx &c, int f, uint32_t ack, bool ece, int64_t now)
{
    const FlowSoA &F = c.s->fl;
    if (F.st[f] != S_EST)
        return;
#ifdef NS_TRACE
    if (f == NS_TRACE)
        printf("ack %lld %u cwnd-before %u%s cong %d ssth %u recover %u high %u ecn_s %d inflight %u items %u\n", (long long)(now / 1000),
               ack + 1, F.cwnd[f], ece ? " ECE" : "", F.cong[f], F.ssthresh[f], F.recover[f] + 1, F.high[f] + 1, F.ecn_s[f],
               inflight(F, f), F.it_n[f]);
#endif
    uint32_t old = F.una[f];
    if (ack < old)
        return;
    rtt_ack(*c.s, f, ack, now); // EstimateRtt (DoForwardUp)
    bool advancing = ack > old;
    uint32_t delivered = 0;
    if (advancing)
        delivered = tb_discard(F, f, ack); // TcpTxBuffer::DiscardUpTo; una = ack
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
                tb_add_reno_sack(F, f); // one segment has left the network
            else if (cg == C_DISORDER)
            {
                if (F.dupacks[f] == DUPTHRESH && (old >= F.recover[f] || !F.recover_active[f]))
                    enter_recovery(c, f, now);
                else if (tb_head_lost(F, f))
                    enter_recovery(c, f, now); // IsLost(HighACK)
            }
        }
    }
    if (ack == old && ack == F.high[f])
    {
        // nothing outstanding
    }
    else if (ack == old && ack > F.high[f])
    {
        F.nxt[f] = ack; // the ACK of the FIN again
    }
    else if (ack == old)
    {
        pkts_acked(c, f, 1, ack, now);
    }
    else if (advancing)
    {
        // currentDelivered: the bytes of the items removed, whole segments plus the remainder
        // that ns-3 accumulates in m_bytesAckedNotProcessed
        uint32_t segs = delivered / MSS;
        F.ack_rem[f] += delivered % MSS;
        if (F.ack_rem[f] >= MSS)
        {
            segs++;
            F.ack_rem[f] -= MSS;
        }
        if (!dupack)
            F.dupacks[f] = 0;
        uint8_t cg = F.cong[f];
        if (ack < F.recover[f] && cg == C_RECOVERY)
        {
            // partial ACK: the head is lost; retransmit it unless the item that this ACK plus one
            // segment would complete was retransmitted already
            tb_mark_head_lost(F, f);
            if (!tb_retx_ends_at(F, f, ack + MSS))
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
                segs = (ack - old) / MSS; // recounted, without the remainder
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
    }
    // ProcessWait: the ACK of the FIN moves FIN_WAIT_1 to FIN_WAIT_2
    if ((F.fin[f] & FIN_SENT) && F.it_n[f] == 0 && ack == F.high[f] + 1)
        F.fin[f] |= FIN_ACKED;
    if (advancing)
    {
        messages_acked(c, f, now);
        if (F.st[f] == S_DONE)
            return;
    }
#ifdef NS_TRACE
    if (f == NS_TRACE)
        printf("cwnd-after %lld %u ssth %u cong %d\n", (long long)(now / 1000), F.cwnd[f], F.ssthresh[f], F.cong[f]);
#endif
    send_pending(c, f, now);
}

__device__ __noinline__ void on_rto(HostCtx &c, int f, int64_t now)
{
    const FlowSoA &F = c.s->fl;
    F.rto_at[f] = -1;
    if (F.st[f] != S_EST)
        return;
    if (F.una[f] >= F.tail[f])
    {
        // nothing to retransmit; in FIN_WAIT_1 the FIN alone would be resent
        if ((F.fin[f] & (FIN_SENT | FIN_ACKED)) == FIN_SENT)
            atomicAdd(&c.s->ev[14], 1ull); // (a lone FIN: not modelled, cannot happen)
        return;
    }
    atomicAdd(&c.s->ev[3], 1ull);
    uint32_t before = inflight(F, f);
    F.dupacks[f] = 0;
    tb_all_lost(F, f); // ResetRenoSack + SetSentListLost(resetSack)
    F.recover[f] = F.high[f];
    F.recover_active[f] = 1;
    int64_t r = 2 * F.rto[f];
    F.rto[f] = r < 60000000000000ll ? r : 60000000000000ll;
    F.h_n[f] = 0; // m_history.clear()
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
__device__ __noinline__ void send_synack(HostCtx &c, int f, int64_t now)
{
    const FlowSoA &F = c.s->fl;
    int64_t rto = CONN_TIMEOUT_PS << F.srv_syn_n[f];
    F.srv_syn_n[f]++;
    if (F.r_synack_t[f] < 0)
        F.r_synack_t[f] = now; // UpdateRttHistory for the first SYN-ACK only
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

// TcpRxBuffer::Add for [seq, end): stores the bytes not held yet (false if there are none) and
// advances rcv_nxt over contiguous data and the FIN. The PacketSink reads in-order data at once,
// so the stored blocks are the out-of-order data: disjoint, sorted, beyond rcv_nxt.
__device__ inline bool rx_add(const Sim &s, int f, uint32_t seq, uint32_t end)
{
    const FlowSoA &F = s.fl;
    uint32_t head = seq > F.rcv_nxt[f] ? seq : F.rcv_nxt[f];
    if (head >= end)
        return false;
    uint32_t *bs = F.oo_s + F.oo_off[f], *be = F.oo_e + F.oo_off[f];
    uint32_t n = F.ooo_n[f];
    uint32_t i = 0;
    while (i < n && be[i] < head)
        i++;
    if (i < n && bs[i] <= head && be[i] >= end)
        return false; // all held already
    // the union with the blocks it overlaps or touches
    uint32_t us = head, ue = end, j = i;
    while (j < n && bs[j] <= ue)
    {
        if (bs[j] < us)
            us = bs[j];
        if (be[j] > ue)
            ue = be[j];
        j++;
    }
    if (j == i)
    {
        if (n >= F.oo_cap[f])
        {
            atomicAdd(&s.ev[6], 1ull); // too many out-of-order blocks
            return false;
        }
        for (uint32_t k = n; k > i; k--)
        {
            bs[k] = bs[k - 1];
            be[k] = be[k - 1];
        }
        n++;
    }
    else
    {
        uint32_t gone = j - i - 1;
        for (uint32_t k = j; k < n; k++)
        {
            bs[k - gone] = bs[k];
            be[k - gone] = be[k];
        }
        n -= gone;
    }
    bs[i] = us;
    be[i] = ue;
    if (head == F.rcv_nxt[f])
    {
        // in sequence: the block at i (= 0) is now in order
        F.rcv_nxt[f] = be[0];
        for (uint32_t k = 1; k < n; k++)
        {
            bs[k - 1] = bs[k];
            be[k - 1] = be[k];
        }
        n--;
    }
    F.ooo_n[f] = n;
    if (F.r_fin[f] >= 0 && F.rcv_nxt[f] == (uint32_t)F.r_fin[f])
        F.rcv_nxt[f]++; // the FIN
    return true;
}

__device__ inline bool rx_finished(const FlowSoA &F, int f) { return F.r_fin[f] >= 0 && (uint32_t)F.r_fin[f] < F.rcv_nxt[f]; }

// the receiving socket's FIN+ACK (SendEmptyPacket): it cancels the delayed ACK and, unless
// one is pending, arms its own retransmission after the RTO
__device__ __noinline__ void send_fin_ack(HostCtx &c, int f, int64_t now)
{
    const FlowSoA &F = c.s->fl;
    F.delack_at[f] = -1;
    F.delack_count[f] = 0;
    emit(c, f, F_ACK | F_FIN, 0, F.rcv_nxt[f], 0, CTRL_FRAME, true, now);
    if (F.r_frx_at[f] < 0)
    {
        int64_t est = F.r_srtt[f] >= 0 ? F.r_srtt[f] : INIT_RTT_PS, v = 4 * F.r_rttvar[f];
        int64_t rto = est + (v > CLOCK_GRAN_PS ? v : CLOCK_GRAN_PS);
        rto = rto > MIN_RTO_PS ? rto : MIN_RTO_PS;
        F.r_frx_at[f] = now + rto;
        timer_chain(c, F.schain, f, rto); // (the sender's SendPendingData chain is idle by now)
    }
}

// LastAckTimeout's period: the estimate plus four deviations, without the minimum RTO
__device__ inline int64_t last_rto(const FlowSoA &F, int f)
{
    int64_t est = F.r_srtt[f] >= 0 ? F.r_srtt[f] : INIT_RTT_PS, v = 4 * F.r_rttvar[f];
    return est + (v > CLOCK_GRAN_PS ? v : CLOCK_GRAN_PS);
}

// TcpSocketBase::DoPeerClose: CLOSE_WAIT; the PacketSink's sockets have ShutdownSend set, so
// Close() follows at once: FIN+ACK and LAST_ACK
__device__ __noinline__ void do_peer_close(HostCtx &c, int f, int64_t now)
{
    const FlowSoA &F = c.s->fl;
    F.r_st[f] = R_LAST_ACK;
    send_fin_ack(c, f, now);
    F.r_last_n[f] = DATA_RETRIES;
    int64_t lr = last_rto(F, f);
    F.r_last_at[f] = now + lr;
    timer_chain(c, F.dchain, f, lr); // (no delayed ACK in LAST_ACK)
}

__device__ inline void rcv_closed(const FlowSoA &F, int f)
{
    F.r_st[f] = R_CLOSED; // CloseAndNotify: the endpoint is gone, the timers with it
    F.r_last_at[f] = -1;
    F.r_frx_at[f] = -1;
    F.delack_at[f] = -1;
}

// TcpSocketBase::ReceivedData
__device__ inline void received_data(HostCtx &c, int f, uint32_t seq, uint32_t len, bool fin, int64_t now)
{
    const FlowSoA &F = c.s->fl;
    uint32_t expected = F.rcv_nxt[f];
    if (!rx_add(*c.s, f, seq, seq + len))
    {
        send_ack(c, f, F.rcv_nxt[f], false, false, now); // insert failed: ACK now
        return;
    }
    if (expected < F.rcv_nxt[f] && rx_finished(F, f) && !fin)
    {
        do_peer_close(c, f, now); // the FIN came earlier; its hole is filled
        return;
    }
    bool gap = F.ooo_n[f] > 0;
    bool filled = F.rcv_nxt[f] > expected + len;
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

// a data segment (or FIN) at the receiving socket: DoForwardUp, then ProcessEstablished or, in
// CLOSE_WAIT, ProcessWait
__device__ inline void on_data(HostCtx &c, int f, const Frame &p, int64_t now)
{
    const FlowSoA &F = c.s->fl;
    if (!F.r_up[f] || F.r_st[f] == R_CLOSED)
        return; // (a closed connection's segments reach the listening socket, which ignores them)
    uint32_t seq = p.sa, len = payload_of(p), end = seq + len;
    bool fin = p.flags & F_FIN;
    // TcpSocketBase::IsValidTcpSegment: data entirely below rcv_nxt (OutOfRange; in LAST_ACK
    // anything but the next sequence number; never before ESTABLISHED) is answered with a plain
    // ACK before any ECN processing
    if (len > 0 && F.r_est[f] && (F.r_st[f] == R_LAST_ACK ? seq != F.rcv_nxt[f] : end < F.rcv_nxt[f]))
    {
        send_ack(c, f, F.rcv_nxt[f], false, true, now);
        return;
    }
#ifdef NS_TRACE
    if (f == NS_TRACE)
        printf("srv-rx %lld seq %u len %u flags %d delack_count %d delack_at %lld\n", (long long)now, p.sa + 1, len, p.flags,
               F.delack_count[f], (long long)F.delack_at[f]);
#endif
    rx_ecn(c, f, p, now);
    // DoForwardUp, in ESTABLISHED: the sender has reacted to the echo; stop echoing unless CE
    // just arrived
    if ((p.flags & F_CWR) && F.r_est[f] && F.r_st[f] == R_OPEN && F.ecn_r[f] != E_CE_RCVD)
        F.ecn_r[f] = E_IDLE;
    if (!F.r_est[f])
    {
        // ProcessSynRcvd: data completes the handshake (its ACK was lost); so does a FIN, but only
        // in sequence: one out of sequence is ignored
        if (fin && seq != F.rcv_nxt[f])
            return;
        rcv_established(c, f);
    }
    if (F.r_st[f] == R_LAST_ACK)
    {
        // ProcessLastAck (everything in range is beyond the FIN: not expected)
        if (fin || seq == F.rcv_nxt[f])
            rcv_closed(F, f);
        return;
    }
    if (!fin)
    {
        received_data(c, f, seq, len, false, now); // ReceivedAck -> ReceivedData
        return;
    }
    // TcpSocketBase::PeerClose
    if (seq < F.rcv_nxt[f])
        return;
    F.r_fin[f] = end; // SetFinSequence
    if (F.rcv_nxt[f] == end)
        F.rcv_nxt[f]++;
    if (len)
        received_data(c, f, seq, len, true, now);
    if (!rx_finished(F, f))
        return;
    do_peer_close(c, f, now);
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

// the application hands every due message to the socket (FlowSender::Write); Socket::Send
// schedules SendPendingData one time step (1 ps) later, unless one is pending or the window is
// closed. After the last message the application closes the connection (if it does).
__device__ __noinline__ void app_write(HostCtx &c, int f, int64_t now)
{
    const FlowSoA &F = c.s->fl;
    const MsgSoA &M = c.s->mg;
    if (F.m_wr[f] >= F.m_due[f])
        return;
    while (F.m_wr[f] < F.m_due[f])
    {
        int m = F.m_first[f] + F.m_wr[f];
        M.est[m] = now;
        F.tail[f] = M.end[m];
        F.m_wr[f]++;
    }
    if (F.fin_on[f] && F.m_wr[f] == F.m_cnt[f])
        F.fin[f] |= FIN_CLOSE; // Close() with data pending: m_closeOnEmpty
    uint32_t pipe = inflight(F, f);
    if (F.cwnd[f] > pipe && F.syn_at[f] < 0)
    {
        F.syn_at[f] = now + 1;
        chain_push(F.schain + (size_t)f * CHAIN, CHAIN_STEP, c.echain);
        due_insert(c, now + 1, f, 2);
    }
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
                F.syn_at[f] = -1;
                // the first RTT sample, from the first SYN even if it was retransmitted: ns-3's
                // UpdateRttHistory cannot mark the zero-length SYN entry as retransmitted, so a
                // SYN timeout yields a ~10 ms sample (and a ~30 ms RTO)
                rtt_sample(F, f, now - F.timing_time[f]);
                emit(c, f, F_ACK, 0, 0, 0, CTRL_FRAME, false, now);
                // ConnectionSucceeded (ScheduleNow) lets the application write its due messages
                uint16_t parent[CHAIN];
                for (int i = 0; i < CHAIN; i++)
                    parent[i] = c.echain[i];
                chain_push(c.echain, CHAIN_NOW, parent);
                app_write(c, f, now);
            }
        }
        else if (p.flags & F_FIN)
        {
            // the receiver's FIN+ACK, in FIN_WAIT_1/2 (ProcessWait): ReceivedAck, then the FIN
            // completes the close (TimeWait) and is acknowledged; TIME_WAIT ignores everything
            if (F.fin[f] & FIN_TW)
                return;
            snd_ecn(c, f, p);
            on_ack(c, f, p.sa, p.flags & F_ECE, now);
            F.fin[f] |= FIN_TW;
            emit(c, f, F_ACK, 0, 1, 0, CTRL_FRAME, false, now);
        }
        else
        {
            if (F.fin[f] & FIN_TW)
                return;
            snd_ecn(c, f, p);
            on_ack(c, f, p.sa, p.flags & F_ECE, now);
        }
        return;
    }
    // at the receiving socket: EstimateRtt takes one sample, from the first SYN-ACK to the first
    // segment that acknowledges it
    if (!(p.flags & F_SYN) && F.r_up[f] && F.r_srtt[f] < 0 && F.r_synack_t[f] >= 0)
    {
        int64_t m = now - F.r_synack_t[f];
        F.r_srtt[f] = m;
        F.r_rttvar[f] = m / 2;
    }
    if (false)
    {
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
    else if (p.flags & (F_DATA | F_FIN))
    {
        on_data(c, f, p, now);
    }
    else if (F.r_up[f] && F.r_st[f] != R_CLOSED)
    {
        rx_ecn(c, f, p, now); // the pure ACK that completes the handshake, or the last ACK
        rcv_established(c, f);
        if (F.r_st[f] == R_LAST_ACK)
            rcv_closed(F, f);
    }
}



// the event-order chain of a timer
__device__ inline const uint16_t *due_chain(const FlowSoA &F, const Due &d)
{
    const uint16_t *base = d.kind == 1 || d.kind == 3 || d.kind == 4 ? F.dchain
                           : d.kind == 5 || (d.kind == 2 && F.st[d.flow] == S_EST) ? F.schain
                                                                                    : F.rchain;
    return base + (size_t)d.flow * CHAIN;
}

// a timer and a frame arrival at the same instant: ns-3 runs the one scheduled first
__device__ __noinline__ bool timer_before_arrival(const Sim &s, const FlowSoA &F, const Due &d, const Frame &p)
{
    uint16_t arr[CHAIN];
    chain_push(arr, chain_arrival(p.len), p.chain);
    int r = chain_cmp(due_chain(F, d), arr);
    if (r == 0)
        atomicAdd(&s.ev[25], 1ull);
    return r <= 0; // (undecided: the timer)
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
        int64_t a = F.delack_at[f], b = F.srv_syn_at[f], l = F.r_last_at[f], x = F.r_frx_at[f];
        if (a >= 0 && a < E0 && nd < MAX_DUE)
            due[nd++] = {a, f, 1};
        if (b >= 0 && b < E0 && nd < MAX_DUE)
            due[nd++] = {b, f, 3};
        if (l >= 0 && l < E0 && nd < MAX_DUE)
            due[nd++] = {l, f, 4};
        if (x >= 0 && x < E0 && nd < MAX_DUE)
            due[nd++] = {x, f, 5};
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
            int64_t st = s.mg.start[hs.pend_msg[pn]];
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
        if (now == tp && s.mg.conn[hs.pend_msg[pn]] >= 0 && F.m_first[s.mg.conn[hs.pend_msg[pn]]] != hs.pend_msg[pn])
        {
            // a later message of a connection: FlowSender::SendMessage
            int m = hs.pend_msg[pn++], f = s.mg.conn[m];
            chain_root_msg(c.echain, m - s.mg_first_of_world[h / s.H1]);
            F.m_due[f]++;
            if (F.st[f] == S_EST)
                app_write(c, f, now);
        }
        else if (now == tp)
        {
            int m = hs.pend_msg[pn++], f = s.mg.conn[m];
            chain_root(c.echain, h % s.H1, f); // application starts are scheduled at setup, host by host
            F.m_due[f] = 1;
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
        else if (now == td && (now != tr || timer_before_arrival(s, F, due[id], i2 < n2 ? m2[i2] : m1[i1])))
        {
            Due d = due[id++];
            const uint16_t *tc = due_chain(F, d);
            for (int i = 0; i < CHAIN; i++)
                c.echain[i] = tc[i];
            if (d.kind == 0 && F.rto_at[d.flow] == d.at)
                on_rto(c, d.flow, now);
            else if (d.kind == 1 && F.delack_at[d.flow] == d.at)
                on_delack(c, d.flow, now);
            else if (d.kind == 4 && F.r_last_at[d.flow] == d.at && F.r_st[d.flow] == R_LAST_ACK)
            {
                // LastAckTimeout: resend the FIN+ACK, a limited number of times
                if (F.r_last_n[d.flow] == 0)
                    rcv_closed(F, d.flow);
                else
                {
                    F.r_last_n[d.flow]--;
                    send_fin_ack(c, d.flow, now);
                    int64_t lr = last_rto(F, d.flow);
                    F.r_last_at[d.flow] = now + lr;
                    timer_chain(c, F.dchain, d.flow, lr);
                }
            }
            else if (d.kind == 5 && F.r_frx_at[d.flow] == d.at && F.r_st[d.flow] == R_LAST_ACK)
            {
                F.r_frx_at[d.flow] = -1; // fired: SendEmptyPacket(FIN | ACK) arms the next
                send_fin_ack(c, d.flow, now);
            }
            else if (d.kind == 3 && F.srv_syn_at[d.flow] == d.at && !F.r_est[d.flow])
            {
                F.srv_syn_at[d.flow] = -1; // fired
                send_synack(c, d.flow, now);
            }
            else if (d.kind == 2 && F.syn_at[d.flow] == d.at && F.st[d.flow] == S_EST)
            {
                // SendPendingData, deferred by Socket::Send
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
            if (F.st[f] == S_DONE && F.delack_at[f] < 0 && F.r_last_at[f] < 0 && F.r_frx_at[f] < 0)
                continue;
            hs.act_rcv[(size_t)h * MAX_ACTIVE + k++] = f;
            int64_t a = F.delack_at[f], b = F.srv_syn_at[f], l = F.r_last_at[f], x = F.r_frx_at[f];
            if (a >= 0 && a < next)
                next = a;
            if (b >= 0 && b < next)
                next = b;
            if (l >= 0 && l < next)
                next = l;
            if (x >= 0 && x < next)
                next = x;
        }
        hs.n_rcv[h] = k;
    }
    if (pn < pe)
    {
        int64_t st = s.mg.start[hs.pend_msg[pn]];
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
// the routing epoch at time ts (link failures: an event at ts comes first)
__device__ inline int epoch_of(const Sim &s, int64_t ts)
{
    int e = 0;
    while (e < s.n_fail && ts >= s.fail_t[e])
        e++;
    return e;
}

// up: the index into the equal-cost routes (all upward; a downward route is unique)
__device__ inline int route_up(const Sim &s, int sw, int dst_node, int up, int ep = 0)
{
    int dst = dst_node % s.H1; // within the world
    sw %= s.S1;
    if (s.topo == TOPO_GRAPH)
    {
        int e = s.host_sw[dst];
        if (e == sw)
            return s.host_port[dst];
        size_t r = ((size_t)ep * s.S1 + sw) * s.S1 + e;
        return s.rt_ports[s.rt_off[r] + up];
    }
    if (s.topo == TOPO_LEAFSPINE)
    {
        // leaf: own hosts on ports 0..hpl-1, spine j on port hpl + j; spine: leaf l on port l
        if (sw < s.leaves)
            return dst / s.hpl == sw ? dst % s.hpl : s.hpl + up;
        return dst / s.hpl;
    }
    const int k = s.k, hk = s.hk, E = k * hk;
    int dst_pod = dst / (hk * hk), dst_edge = (dst / hk) % hk;
    if (sw < E)
    {
        int pod = sw / hk, j = sw % hk;
        if (dst_pod == pod && dst_edge == j)
            return dst % hk;
        return hk + up;
    }
    if (sw < 2 * E)
    {
        int pod = (sw - E) / hk;
        if (dst_pod == pod)
            return dst_edge;
        return hk + up;
    }
    return dst_pod;
}

__device__ inline int route(const Sim &s, int sw, const Frame &p, int ep = 0)
{
    if (s.topo == TOPO_GRAPH)
    {
        // per-flow ECMP: the flow hash modulo the number of routes at this switch
        int dst = p.dst_node % s.H1, x = sw % s.S1, e = s.host_sw[dst];
        if (e == x)
            return s.host_port[dst];
        uint32_t h = p.to_sender ? s.fl.hash_ack[p.flow] : s.fl.hash_data[p.flow];
        return route_up(s, sw, p.dst_node, (int)(h % s.rt_n[((size_t)ep * s.S1 + x) * s.S1 + e]), ep);
    }
    return route_up(s, sw, p.dst_node, p.up);
}

// a frame arriving on a port whose interface is down is dropped before routing
__device__ inline bool port_down(const Sim &s, int sw, int q, int ep)
{
    return s.n_fail && s.port_dn[((size_t)ep * s.S1 + sw % s.S1) * s.k + q];
}

// the number of equal-cost routes Ipv4GlobalRouting finds from switch sw towards host dst_node
// (allRoutes): s.ecmp_n upwards, one downwards; 0 for a host attached to sw, whose network
// Ipv4StaticRouting (first in the list) serves, so that global routing draws nothing
__device__ inline int routes_to(const Sim &s, int sw, int dst_node, int ep = 0)
{
    int dst = dst_node % s.H1;
    sw %= s.S1;
    if (s.topo == TOPO_GRAPH)
        return s.host_sw[dst] == sw ? 0 : s.rt_n[((size_t)ep * s.S1 + sw) * s.S1 + s.host_sw[dst]];
    if (s.topo == TOPO_LEAFSPINE)
        return sw < s.leaves ? (dst / s.hpl == sw ? 0 : s.ecmp_n) : 1;
    const int hk = s.hk, E = s.k * hk;
    int dst_pod = dst / (hk * hk), dst_edge = (dst / hk) % hk;
    if (sw < E)
        return dst_pod == sw / hk && dst_edge == sw % hk ? 0 : s.ecmp_n;
    if (sw < 2 * E)
        return dst_pod == (sw - E) / hk ? 1 : s.ecmp_n;
    return 1;
}

struct SwScratch
{
    int *rows;        // [k][k+1] shared: counts / offsets ingress x egress
    int64_t *ts;      // [k * 2 * MAX_TX] (global scratch for this switch) arrival time
    uint32_t *ref;    // [k * 2 * MAX_TX] source: ingress port << 16 | index in the combined input
    int *base;        // [k] shared: region of each egress in ts/ref
    int *cnt;         // [k] shared
    int *in_win;      // [k][3] shared: each ingress port's input window (i2, n2, n1)
    uint8_t *up;      // [k * 2 * MAX_TX] (global) per-packet ECMP: the route drawn for each arrival
    int *mpos;        // [k] shared: merge positions
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

// arrivals at the same instant, as ns-3 orders their Receive events: the longer frame started
// earlier upstream; else by the chains of the upstream transmission starts
__device__ inline bool frame_before(const Sim &s, const Frame &a, const Frame &b)
{
    if (a.len != b.len)
        return a.len > b.len;
    int c = chain_cmp(a.chain, b.chain);
    if (c == 0)
        atomicAdd(&s.ev[25], 1ull); // the recorded histories do not tell: a guess
    return c != 0 ? c < 0 : a.flow < b.flow;
}

// per-packet ECMP: Ipv4GlobalRouting::RouteInput draws GetInteger(0, routes - 1) from the
// switch's random variable for every packet it forwards, in the order the packets arrive at the
// node (all ports merged), whatever the number of routes
__device__ __noinline__ void spray_draws(const Sim &s, const SwScratch &sc, int sw, int64_t t)
{
    const int k = s.k;
    for (int q = 0; q < k; q++)
        sc.mpos[q] = 0;
    nsm_rng::State st = s.sw_rng[sw];
    for (;;)
    {
        int best = -1;
        const Frame *bf = nullptr;
        for (int q = 0; q < k; q++)
        {
            Input in = input_cached(s, sc, sw, q, t);
            if (sc.mpos[q] >= in.count())
                continue;
            const Frame &f = in.at(sc.mpos[q]);
            if (best < 0 || f.ts < bf->ts || (f.ts == bf->ts && frame_before(s, f, *bf)))
            {
                best = q;
                bf = &f;
            }
        }
        if (best < 0)
            break;
        int ep = s.n_fail ? epoch_of(s, bf->ts) : 0;
        if (port_down(s, sw, best, ep))
        {
            sc.mpos[best]++; // dropped on arrival: no lookup
            continue;
        }
        int nr = routes_to(s, sw, bf->dst_node, ep);
        uint32_t up = nr ? nsm_rng::get_integer(st, (uint32_t)nr) : 0;
        sc.up[best * 2 * MAX_TX + sc.mpos[best]] = (uint8_t)up;
        sc.mpos[best]++;
    }
    s.sw_rng[sw] = st;
}

// One slot of switch sw, port q (all ports of the switch call it together).
__device__ inline int switch_step(const Sim &s, int sw, int q, int64_t t, bool active, SwScratch sc)
{
    const int k = s.k;
    const int64_t S0 = t * SLOT_PS, E0 = S0 + SLOT_PS;
    int port = sw * k + q;
    Input in{nullptr, nullptr, 0, 0, 0};
    int *my_row = sc.rows + q * (k + 1);
    const bool spray = s.sw_rng != nullptr;
    auto egress_of = [&](int j) {
        const Frame &p = in.at(j);
        int ep = s.n_fail ? epoch_of(s, p.ts) : 0;
        if (port_down(s, sw, q, ep))
            return -1; // dropped on arrival
        return spray ? route_up(s, sw, p.dst_node, sc.up[q * 2 * MAX_TX + j], ep) : route(s, sw, p, ep);
    };
    if (active)
    {
        in = input_of(s, s.sw_in_link[port], t);
        sc.in_win[3 * q] = in.i2;
        sc.in_win[3 * q + 1] = in.n2;
        sc.in_win[3 * q + 2] = in.n1;
        for (int i = 0; i <= k; i++)
            my_row[i] = 0;
    }
    if (spray)
    {
        __syncthreads();
        if (active && q == 0)
            spray_draws(s, sc, sw, t);
        __syncthreads();
    }
    if (active)
    {
        int n = in.count();
        for (int j = 0; j < n; j++)
        {
            int e = egress_of(j);
            if (e >= 0)
                my_row[e]++;
            else
                atomicAdd(&s.ev[0], 1ull); // dropped on arrival at a down interface
        }
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
            int e = egress_of(j);
            if (e < 0)
                continue;
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
    auto before = [&](uint32_t ra, uint32_t rb) { return frame_before(s, frame_at(ra), frame_at(rb)); };
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
