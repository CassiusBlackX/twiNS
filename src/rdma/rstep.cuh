// TwiNS-RDMA device code: the NIC (RdmaHw, RdmaEgressQueue, QbbNetDevice on a host), the switch
// (SwitchNode, SwitchMmu, BEgressQueue, QbbNetDevice), and the slot kernel. See rdma.h.
#include "rdma.h"

namespace rdma
{

// ------------------------------------------------------------------ ns-3.17 time and rates
// Seconds(x) for 0 <= x: int64x64_t(x) is floor(x) . floor(frac(x) 2^64) in 64.64 fixed point;
// multiplying by 10^9 (int64x64_t::Mul, exact here) and taking the high word (Time(int64x64_t))
// gives floor(x) 10^9 + floor(lo 10^9 / 2^64)
__host__ __device__ __forceinline__ int64_t ns_seconds(double x)
{
    double hi = floor(x);
    uint64_t lo = (uint64_t)((x - hi) * 18446744073709551616.0);
#if defined(__HIP_DEVICE_COMPILE__) || defined(__CUDA_ARCH__)
    uint64_t q = __umul64hi(lo, 1000000000ull);
#else
    uint64_t q = (uint64_t)(((unsigned __int128)lo * 1000000000ull) >> 64);
#endif
    return (int64_t)hi * 1000000000ll + (int64_t)q;
}

// Seconds(DataRate(bps).CalculateTxTime(bytes)): bytes * 8 / bps in double
__host__ __device__ __forceinline__ int64_t tx_ns(uint64_t bps, uint32_t bytes) { return ns_seconds((double)bytes * 8 / (double)bps); }

// tx_ns(link rate, bytes) for frames below TXT_N bytes (main fills it)
constexpr int TXT_N = 4096;
__constant__ int32_t c_txt[TXT_N];
__device__ __forceinline__ int64_t link_tx(const Sim &s, uint32_t bytes) { return bytes < TXT_N ? c_txt[bytes] : tx_ns(s.P.bps, bytes); }

// DataRate(d) from a double (DataRate(uint64_t), truncation)
__device__ __forceinline__ uint64_t rate_of(double d) { return (uint64_t)d; }

// ------------------------------------------------------------------ event order (see Chain)
__host__ __device__ __forceinline__ uint32_t ccode(int64_t lead, int ord)
{
    uint64_t l = (uint64_t)lead + 1;
    if (l > 0x1FFFFFFEull)
        l = 0x1FFFFFFEull;
    return (uint32_t)(l * 8 + (7 - (ord < 7 ? ord : 7)));
}

__device__ TailPool g_tail;

// the chain of an event scheduled with `code` by the event whose chain is par
__host__ __device__ __forceinline__ void cpush(Chain *dst, uint32_t code, const Chain &par)
{
    Chain c;
    if (par.r[0].code == code)
    {
        c = par;
        if (c.r[0].cnt < 0xFFFFFFFFu)
            c.r[0].cnt++;
    }
    else
    {
        c.r[0].code = code;
        c.r[0].cnt = 1;
        for (int i = 1; i < RUNS; i++)
            c.r[i] = par.r[i - 1];
        c.root = par.root;
        c.tail = par.tail;
        c.pad = 0;
        if (par.r[RUNS - 1].code)
        {
#if defined(__HIP_DEVICE_COMPILE__) || defined(__CUDA_ARCH__)
            uint64_t pos = atomicAdd(g_tail.cnt, 1ull) + 1;
            TailNode &n = g_tail.n[(pos - 1) & g_tail.mask];
            n.code = par.r[RUNS - 1].code;
            n.cnt = par.r[RUNS - 1].cnt;
            n.next = par.tail;
            n.pos = pos;
            c.tail = pos;
#else
            c.tail = 0; // (the host builds short chains only)
#endif
        }
    }
    *dst = c;
}

// the runs of a chain, newest first: the inline ones, then its tail nodes
struct RunCursor
{
    const Chain *c;
    int i;          // inline run, or RUNS: in the tail
    uint64_t node;  // the current tail node's pos
    uint32_t code;  // 0: the end
    uint32_t left;  // repetitions left of the current run
    bool lost;      // the tail was overwritten
    // the runs of the chain cpush(code, ch) would give (code 0: of ch itself)
    __device__ void init(const Chain &ch, uint32_t code0 = 0)
    {
        c = &ch;
        i = 0;
        node = 0;
        lost = false;
        if (code0)
        {
            code = code0;
            if (ch.r[0].code == code0)
                left = ch.r[0].cnt + 1; // merged with ch's first run, which is consumed with it
            else
            {
                left = 1;
                i = -1; // ch's runs follow
            }
            return;
        }
        load();
    }
    __device__ void load()
    {
        if (i < RUNS)
        {
            code = c->r[i].code;
            left = c->r[i].cnt;
            if (code || i < RUNS - 1)
                return;
            i = RUNS;
        }
        if (i == RUNS && node == 0)
        {
            node = c->tail;
            i = RUNS + 1;
        }
        if (node == 0)
        {
            code = 0;
            return;
        }
        const TailNode &n = g_tail.n[(node - 1) & g_tail.mask];
        if (n.pos != node)
        {
            lost = true;
            code = 0;
            return;
        }
        code = n.code;
        left = n.cnt;
    }
    __device__ void next()
    {
        if (i < RUNS)
        {
            i++;
            if (i == RUNS)
            {
                node = c->tail;
                i = RUNS + 1;
            }
        }
        else
            node = g_tail.n[(node - 1) & g_tail.mask].next;
        load();
    }
};

// < 0: a runs first, > 0: b first, 0: the chains do not tell; *inexact: the decision rests on a
// chain whose tail was overwritten. Going back from the events, the first differing lead decides
// (the larger: an earlier ancestor). If every lead is equal, the ancestors are the same event from
// the deepest level at which the ordinals differ, and there the smaller ordinal (the sibling
// scheduled first) decides; distinct roots (flows' StartApplications) decide in start order.
__device__ __forceinline__ int ccmp(uint32_t pa, const Chain &a, uint32_t pb, const Chain &b, bool *inexact)
{
    *inexact = false;
    int ord = 0;
    RunCursor A, B;
    A.init(a, pa);
    B.init(b, pb);
    for (;;)
    {
        uint32_t ca = A.code, cb = B.code;
        if (ca == 0 || cb == 0)
        {
            *inexact = (ca == 0 && A.lost) || (cb == 0 && B.lost);
            if (ca == 0 && cb == 0)
            {
                // distinct roots: the lineages never meet, so the ordinals compared above were not
                // siblings'; the flows' starts decide
                if (a.root && b.root && a.root != b.root)
                    return a.root < b.root ? -1 : 1;
                return ord;
            }
            // a complete chain ends at an event scheduled before everything after it
            if (ca == 0)
                return A.lost ? ord : -1;
            return B.lost ? ord : 1;
        }
        if (ca >> 3 != cb >> 3)
            return ca > cb ? -1 : 1;
        if (ca != cb)
            ord = (ca & 7) > (cb & 7) ? -1 : 1;
        uint32_t m = A.left < B.left ? A.left : B.left;
        A.left -= m;
        B.left -= m;
        if (A.left == 0)
            A.next();
        if (B.left == 0)
            B.next();
    }
}

// the first three runs of the chain cpush(code0, ch) (code0 0: of ch itself)
constexpr int PRE = 3;
__device__ __forceinline__ void cprefix(Run *out, uint32_t code0, const Chain &ch)
{
    if (!code0)
    {
        for (int i = 0; i < PRE; i++)
            out[i] = ch.r[i];
    }
    else if (ch.r[0].code == code0)
    {
        out[0].code = code0;
        out[0].cnt = ch.r[0].cnt + 1;
        for (int i = 1; i < PRE; i++)
            out[i] = ch.r[i];
    }
    else
    {
        out[0].code = code0;
        out[0].cnt = 1;
        for (int i = 1; i < PRE; i++)
            out[i] = ch.r[i - 1];
    }
}

// the order of two simultaneous events by the leads in their chains' prefixes: -1, 1, or 0 if
// the prefixes do not decide it (equal leads throughout, or a chain ends; ccmp then decides)
__device__ __forceinline__ int pcmp(const Run *a, const Run *b)
{
    int ia = 0, ib = 0;
    uint32_t ra = a[0].cnt, rb = b[0].cnt;
    while (ia < PRE && ib < PRE)
    {
        uint32_t ca = a[ia].code, cb = b[ib].code;
        if (!ca || !cb)
            return 0;
        if (ca >> 3 != cb >> 3)
            return ca > cb ? -1 : 1;
        uint32_t m = ra < rb ? ra : rb;
        ra -= m;
        rb -= m;
        if (ra == 0 && ++ia < PRE)
            ra = a[ia].cnt;
        if (rb == 0 && ++ib < PRE)
            rb = b[ib].cnt;
    }
    return 0;
}

// a candidate event: does (ta, a) run before (tb, b)? A chain is given as cpush(p, chain) (p 0:
// the chain itself). Undecided ties keep b (the earlier candidate).
__device__ __forceinline__ bool before(const Sim &s, int64_t ta, uint32_t pa, const Chain &a, int64_t tb, uint32_t pb, const Chain &b)
{
    if (ta != tb)
        return ta < tb;
    bool inexact;
    int c = ccmp(pa, a, pb, b, &inexact);
    if (inexact)
        atomicAdd(&s.ev[9], 1ull);
    if (c == 0)
    {
        atomicAdd(&s.ev[8], 1ull);
#ifdef RDMA_TRACE_TIES
        printf("tie %lld: [%u.%u]", (long long)ta, pa ? (pa >> 3) - 1 : 0, pa ? 7 - (pa & 7) : 0);
        for (int i = 0; i < RUNS && a.r[i].code; i++)
            printf(" %u.%u:%u", (a.r[i].code >> 3) - 1, 7 - (a.r[i].code & 7), a.r[i].cnt);
        printf(" root %u%s  | [%u.%u]", a.root, a.tail ? " +tail" : "", pb ? (pb >> 3) - 1 : 0, pb ? 7 - (pb & 7) : 0);
        for (int i = 0; i < RUNS && b.r[i].code; i++)
            printf(" %u.%u:%u", (b.r[i].code >> 3) - 1, 7 - (b.r[i].code & 7), b.r[i].cnt);
        printf(" root %u%s\n", b.root, b.tail ? " +tail" : "");
#endif
    }
    return c < 0;
}

// debugging: -DRDMA_TRACE_NODE=n -DRDMA_TRACE_FROM=ns -DRDMA_TRACE_UNTIL=ns prints the events node n
// runs in that window, with their chains
#ifdef RDMA_TRACE_NODE
__device__ __forceinline__ void trace_event(int node, int64_t now, const char *what, int port, const Chain &c)
{
    if (node != RDMA_TRACE_NODE || now < RDMA_TRACE_FROM || now > RDMA_TRACE_UNTIL)
        return;
    printf("ev %lld node %d %s port %d chain", (long long)now, node, what, port);
    for (int i = 0; i < RUNS && c.r[i].code; i++)
        printf(" %u.%u:%u", (c.r[i].code >> 3) - 1, 7 - (c.r[i].code & 7), c.r[i].cnt);
    printf(" root %u%s\n", c.root, c.tail ? " +tail" : "");
}
#else
__device__ __forceinline__ void trace_event(int, int64_t, const char *, int, const Chain &) {}
#endif

// ------------------------------------------------------------------ frames
__host__ __device__ __forceinline__ uint32_t node_ip(int id) { return 0x0b000001u + (id / 256) * 0x00010000u + (id % 256) * 0x00000100u; }

__device__ __forceinline__ uint32_t data_size(const Params &P, uint32_t payload) { return payload + 48 + P.int_size; }
__device__ __forceinline__ uint32_t ack_size(const Params &P)
{
    int base = 12 + P.int_size, pad = 60 - 14 - 20 - base;
    return (pad > 0 ? pad : 0) + base + 20 + 14;
}
constexpr uint32_t PFC_SIZE = 9 + 20 + 14;

// IntHop
__device__ __forceinline__ uint64_t hop_time(uint64_t h) { return (h >> 3) & 0xFFFFFF; }
__device__ __forceinline__ uint64_t hop_bytes(uint64_t h) { return (h >> 27) & 0xFFFFF; }
__device__ __forceinline__ uint32_t hop_qlen(uint64_t h) { return (uint32_t)(h >> 47); }
__device__ __forceinline__ uint64_t hop_rate(uint64_t h)
{
    const uint64_t v[8] = {25000000000ull, 50000000000ull, 100000000000ull, 200000000000ull, 400000000000ull, 0, 0, 0};
    return v[h & 7];
}
__device__ __forceinline__ uint64_t hop_make(uint64_t time, uint64_t bytes, uint32_t qlen, uint64_t rate, uint32_t multi)
{
    uint64_t lr = rate == 25000000000ull ? 0 : rate == 50000000000ull ? 1 : rate == 100000000000ull ? 2 : rate == 200000000000ull ? 3 : 4;
    uint64_t b = bytes / (128ull * multi), q = qlen / (80u * multi);
    return lr | (time & 0xFFFFFF) << 3 | (b & 0xFFFFF) << 27 | (q & 0x1FFFF) << 47;
}
__device__ __forceinline__ uint64_t hop_bytes_delta(uint64_t a, uint64_t b, uint32_t multi)
{
    uint64_t x = hop_bytes(a), y = hop_bytes(b);
    return (x >= y ? x - y : x + (1u << 20) - y) * 128 * multi;
}
__device__ __forceinline__ uint64_t hop_time_delta(uint64_t a, uint64_t b)
{
    uint64_t x = hop_time(a), y = hop_time(b);
    return x >= y ? x - y : x + (1u << 24) - y;
}

// SwitchNode::EcmpHash (MurmurHash3, 12 bytes)
__device__ __forceinline__ uint32_t ecmp_hash(uint32_t w0, uint32_t w1, uint32_t w2, uint32_t seed)
{
    uint32_t h = seed, w[3] = {w0, w1, w2};
    for (int i = 0; i < 3; i++)
    {
        uint32_t k = w[i];
        k *= 0xcc9e2d51u;
        k = (k << 15) | (k >> 17);
        k *= 0x1b873593u;
        h ^= k;
        h = (h << 13) | (h >> 19);
        h += (h << 2) + 0xe6546b64u;
    }
    h ^= 12;
    h ^= h >> 16;
    h *= 0x85ebca6bu;
    h ^= h >> 13;
    h *= 0xc2b2ae35u;
    h ^= h >> 16;
    return h;
}

__device__ __forceinline__ void trace_tx(const Sim &s, int64_t now, int node, int port, const Pkt &p)
{
    if (!s.tx || now < s.tx_from || now > s.tx_until)
        return;
    unsigned long long i = atomicAdd(s.tx_cnt, 1ull);
    if (i >= s.tx_cap)
        return;
    TxRec &r = s.tx[i];
    r.ts = now;
    r.node = node;
    r.dev = port + 1;
    r.kind = p.kind;
    r.size = p.size;
    r.ecn = p.ecn;
    if (p.kind >= K_PAUSE)
    {
        r.a = p.q;
        r.b = r.sport = r.seq = 0;
    }
    else
    {
        bool data = p.kind == K_DATA;
        r.a = data ? p.src : p.dst;
        r.b = data ? p.dst : p.src;
        r.sport = p.sport;
        r.seq = p.seq;
    }
}

// a link's frames arriving in [t0, t1): those of slot - 2 from t0 on, then those of slot - 1 before t1
struct Arrivals
{
    const LFrame *a, *b;
    int na, nb, i;
    __device__ void init(const Sim &s, int link, int64_t slot, int64_t t0)
    {
        a = s.msg + mi(s, slot - 2, link) * MAXF;
        b = s.msg + mi(s, slot - 1, link) * MAXF;
        na = slot >= 2 ? msg_count(s, slot - 2, link) : 0;
        nb = slot >= 1 ? msg_count(s, slot - 1, link) : 0;
        i = 0;
        while (i < na && a[i].ts < t0)
            i++;
    }
    __device__ const LFrame *peek(int64_t t1) const
    {
        if (i < na)
            return &a[i];
        int j = i - na;
        return j < nb && b[j].ts < t1 ? &b[j] : nullptr;
    }
    __device__ void pop() { i++; }
};

// a frame's arrival: [lead tx + D, its ordinal] + the chain of its start
__device__ __forceinline__ uint32_t arrival_code(const Sim &s, const LFrame &f) { return ccode(link_tx(s, f.p.size) + s.P.delay, f.p.ord); }

// ------------------------------------------------------------------ host: the NIC
struct HCtx
{
    const Sim *s;
    int h;
    bool pre_ok;     // the running ACK's HPCC U and dt were computed ahead
    double pre_U;
    uint64_t pre_dt;
    Host st;
    int64_t now, slot;
    Chain cur; // the running event's chain
    int ord;   // events it has scheduled
    LFrame *out;
    int nout, link;
};

__device__ __forceinline__ uint64_t bytes_left(const SndQP &q) { return q.size >= q.snd_nxt ? q.size - q.snd_nxt : 0; }

// tx_ns(rate, bytes) for a QP, remembering the last one (PktSent and ChangeRate ask for the same
// rate and size in turn)
__device__ __forceinline__ int64_t qp_tx(SndQP &q, uint64_t rate, uint32_t bytes)
{
    if (rate != q.tx_rate || bytes != q.tx_size)
    {
        q.tx_rate = rate;
        q.tx_size = bytes;
        q.tx_val = tx_ns(rate, bytes);
    }
    return q.tx_val;
}

__device__ __forceinline__ bool win_bound(const Sim &s, const SndQP &q)
{
    if (q.win == 0)
        return false;
    uint64_t w = q.win;
    if (s.P.var_win)
    {
        w = (uint64_t)q.win * q.rate / q.max_rate;
        if (w == 0)
            w = 1;
    }
    return q.snd_nxt - q.snd_una >= w;
}

// QbbNetDevice::TransmitStart: TransmitComplete after tx, the peer's Receive after tx + delay
__device__ __forceinline__ void h_transmit(HCtx &c, Pkt p)
{
    const Sim &s = *c.s;
    int64_t tx = link_tx(s, p.size);
    c.st.tc_at = c.now + tx;
    cpush(&s.h_tc[c.h], ccode(tx, c.ord++), c.cur);
    if (c.nout >= MAXF)
    {
        atomicAdd(&s.ev[4], 1ull);
        return;
    }
    LFrame &f = c.out[c.nout++];
    p.ord = (uint8_t)(c.ord < 255 ? c.ord : 255);
    c.ord++;
    f.ts = c.now + tx + s.P.delay;
    f.c = c.cur;
    f.p = p;
    trace_tx(s, c.now, c.h, 0, p);
}

__device__ __forceinline__ void h_schedule_ns(HCtx &c, int64_t at)
{
    c.st.ns_at = at;
    cpush(&c.s->h_ns[c.h], ccode(at - c.now, c.ord++), c.cur);
}

// RdmaEgressQueue::GetNextQindex: -1 the ACK queue, -1024 none, else the QP's index in the group
__device__ __forceinline__ int next_qindex(HCtx &c)
{
    const Sim &s = *c.s;
    int ackq = s.P.ack_high_prio ? 0 : PG;
    if (!(c.st.paused >> ackq & 1) && c.st.ackq_n > 0)
        return -1;
    int *list = s.qps + (size_t)c.h * s.qps_cap;
    int res = -1024, fcount = c.st.qp_n, min_finish = INT_MAX;
    for (int qi = 1; qi <= fcount; qi++)
    {
        int idx = (qi + c.st.rr) % fcount;
        const SndQP &q = s.qp[list[idx]];
        if (!(c.st.paused >> PG & 1) && bytes_left(q) > 0 && !win_bound(s, q))
        {
            if (q.next_avail > c.now)
                continue;
            res = idx;
            break;
        }
        else if (q.snd_una >= q.size)
            min_finish = idx < min_finish ? idx : min_finish;
    }
    if (min_finish < INT_MAX)
    {
        int nxt = min_finish;
        for (int i = min_finish + 1; i < fcount; i++)
            if (!(s.qp[list[i]].snd_una >= s.qp[list[i]].size))
            {
                if (i == res)
                    res = nxt;
                list[nxt++] = list[i];
            }
        c.st.qp_n = nxt;
    }
    return res;
}

// QbbNetDevice::DequeueAndTransmit on a host
__device__ __forceinline__ void h_dequeue(HCtx &c)
{
    const Sim &s = *c.s;
    if (c.st.tc_at >= 0)
        return;
    int idx = next_qindex(c);
    if (idx == -1)
    {
        Pkt p = s.ackq[(size_t)c.h * s.ackq_cap + c.st.ackq_head];
        c.st.ackq_head = (c.st.ackq_head + 1) % s.ackq_cap;
        c.st.ackq_n--;
        h_transmit(c, p);
        return;
    }
    int *list = s.qps + (size_t)c.h * s.qps_cap;
    if (idx >= 0)
    {
        int f = list[idx];
        c.st.rr = idx;
        SndQP &q = s.qp[f];
        // RdmaHw::GetNxtPacket
        uint64_t left = bytes_left(q);
        uint32_t payload = left < s.P.mtu ? (uint32_t)left : s.P.mtu;
        Pkt p = {};
        p.kind = K_DATA;
        p.flow = f;
        p.src = (uint16_t)s.fi[f].src;
        p.dst = (uint16_t)s.fi[f].dst;
        p.sport = (uint16_t)s.fi[f].sport;
        p.seq = (uint32_t)q.snd_nxt;
        p.size = (uint16_t)data_size(s.P, payload);
        q.snd_nxt += payload;
        h_transmit(c, p);
        // RdmaHw::PktSent, UpdateNextAvail
        q.last_pkt = p.size;
        q.next_avail = c.now + qp_tx(q, s.P.rate_bound ? q.rate : q.max_rate, p.size);
        return;
    }
    int64_t t = INT64_MAX;
    for (int i = 0; i < c.st.qp_n; i++)
    {
        const SndQP &q = s.qp[list[i]];
        if (bytes_left(q) == 0)
            continue;
        t = q.next_avail < t ? q.next_avail : t;
    }
    if (c.st.ns_at < 0 && t < INT64_MAX && t > c.now)
        h_schedule_ns(c, t);
}

// RdmaHw::ChangeRate (and QbbNetDevice::UpdateNextAvail)
__device__ __forceinline__ void change_rate(HCtx &c, SndQP &q, uint64_t new_rate)
{
    int64_t st = qp_tx(q, q.rate, q.last_pkt), nt = qp_tx(q, new_rate, q.last_pkt);
    q.next_avail = q.next_avail + nt - st;
    if (c.st.ns_at >= 0 && q.next_avail < c.st.ns_at)
        h_schedule_ns(c, q.next_avail < c.now ? c.now : q.next_avail);
    q.rate = new_rate;
}

// UpdateRateHp's loop over the hops without SAMPLE_FEEDBACK: the largest utilization U (the first
// of equal ones) and its tau, from an ACK's INT hops ph and those the QP last stored, qh
__device__ __forceinline__ void hpcc_hops(const Params &P, const uint64_t *ph, int nhop, const uint64_t *qh, uint64_t max_rate, uint32_t win,
                                          double *Uo, uint64_t *dto)
{
    double U = 0;
    uint64_t dt = 0;
    for (int i = 0; i < nhop; i++)
    {
        uint64_t tau = hop_time_delta(ph[i], qh[i]);
        double duration = (double)tau * 1e-9;
        double tx_rate = (double)(hop_bytes_delta(ph[i], qh[i], P.int_multi) * 8) / duration;
        uint32_t qa = hop_qlen(ph[i]) * 80 * P.int_multi, qb = hop_qlen(qh[i]) * 80 * P.int_multi;
        double lr = (double)hop_rate(ph[i]);
        double u = tx_rate / lr + (double)(qa < qb ? qa : qb) * (double)max_rate / lr / (double)win;
        if (u > U)
        {
            U = u;
            dt = tau;
        }
    }
    *Uo = U;
    *dto = dt;
}

// RdmaHw::UpdateRateHp, one rate for the path (MULTI_RATE 0). pre: U and dt computed ahead
// (hpcc_hops, in the host's prologue)
__device__ __forceinline__ void hpcc_update(HCtx &c, SndQP &q, const Pkt &p, bool fast)
{
    const Params &P = c.s->P;
    uint32_t next_seq = (uint32_t)q.snd_nxt;
    if (q.last_update_seq == 0)
    {
        q.last_update_seq = next_seq;
        for (int i = 0; i < p.nhop && i < MAX_HOP; i++)
            q.hop[i] = p.hop[i];
        return;
    }
    if (p.nhop <= MAX_HOP)
    {
        double U = 0;
        uint64_t dt = 0;
        bool any = false;
        if (c.pre_ok)
        {
            U = c.pre_U;
            dt = c.pre_dt;
            any = p.nhop > 0;
            for (int i = 0; i < p.nhop; i++)
                q.hop[i] = p.hop[i];
        }
        else
            for (int i = 0; i < p.nhop; i++)
        {
            if (P.sample_feedback && hop_qlen(p.hop[i]) == 0 && fast)
                continue;
            any = true;
            uint64_t tau = hop_time_delta(p.hop[i], q.hop[i]);
            double duration = (double)tau * 1e-9;
            double tx_rate = (double)(hop_bytes_delta(p.hop[i], q.hop[i], P.int_multi) * 8) / duration;
            uint32_t qa = hop_qlen(p.hop[i]) * 80 * P.int_multi, qb = hop_qlen(q.hop[i]) * 80 * P.int_multi;
            double lr = (double)hop_rate(p.hop[i]);
            double u = tx_rate / lr + (double)(qa < qb ? qa : qb) * (double)q.max_rate / lr / (double)q.win;
            if (u > U)
            {
                U = u;
                dt = tau;
            }
            q.hop[i] = p.hop[i];
        }
        uint64_t new_rate = 0;
        int32_t new_inc = 0;
        if (any)
        {
            if (dt > q.base_rtt)
                dt = q.base_rtt;
            // GCC contracts this to fma(u, B - dt, U dt) (-march=native)
            q.u = fma(q.u, (double)(q.base_rtt - dt), U * (double)dt) / (double)q.base_rtt;
            double max_c = q.u / P.u_target;
            if (max_c >= 1 || (uint32_t)q.inc_stage >= P.mi_thresh)
            {
                new_rate = rate_of((double)q.cur_rate / max_c) + P.rai;
                new_inc = 0;
            }
            else
            {
                new_rate = q.cur_rate + P.rai;
                new_inc = q.inc_stage + 1;
            }
            if (new_rate < P.min_rate)
                new_rate = P.min_rate;
            if (new_rate > q.max_rate)
                new_rate = q.max_rate;
            change_rate(c, q, new_rate);
            if (!fast)
            {
                q.cur_rate = new_rate;
                q.inc_stage = new_inc;
            }
        }
    }
    if (!fast && next_seq > q.last_update_seq)
        q.last_update_seq = next_seq;
}

// RdmaHw::cnp_received_mlx
__device__ __forceinline__ void dcqcn_cnp(HCtx &c, int f, SndQP &q)
{
    const Sim &s = *c.s;
    q.alpha_cnp = 1;
    q.dec_cnp = 1;
    if (q.first_cnp)
    {
        q.alpha = 1;
        q.alpha_cnp = 0;
        q.t_alpha = c.now + s.P.alpha_int;
        cpush(&s.qc[f].alpha, ccode(s.P.alpha_int, c.ord++), c.cur);
        q.t_dec = c.now + s.P.dec_int + 1;
        cpush(&s.qc[f].dec, ccode(s.P.dec_int + 1, c.ord++), c.cur);
        q.target = q.rate = rate_of((double)q.rate * s.P.rate_first_cnp);
        q.first_cnp = 0;
    }
}

// the DCQCN timers: 0 UpdateAlphaMlx, 1 CheckRateDecreaseMlx, 2 RateIncEventTimerMlx
__device__ __forceinline__ void dcqcn_timer(HCtx &c, int f, int kind)
{
    const Sim &s = *c.s;
    const Params &P = s.P;
    SndQP &q = s.qp[f];
    if (kind == 0)
    {
        if (q.alpha_cnp)
            q.alpha = fma(1 - P.g, q.alpha, P.g); // as GCC contracts it
        else
            q.alpha = (1 - P.g) * q.alpha;
        q.alpha_cnp = 0;
        q.t_alpha = c.now + P.alpha_int;
        cpush(&s.qc[f].alpha, ccode(P.alpha_int, c.ord++), c.cur);
    }
    else if (kind == 1)
    {
        q.t_dec = c.now + P.dec_int;
        cpush(&s.qc[f].dec, ccode(P.dec_int, c.ord++), c.cur);
        if (q.dec_cnp)
        {
            if (P.clamp || q.rp_stage != 0)
                q.target = q.rate;
            uint64_t r = rate_of((double)q.rate * (1 - q.alpha / 2));
            q.rate = r > P.min_rate ? r : P.min_rate;
            q.rp_stage = 0;
            q.dec_cnp = 0;
            q.t_rp = c.now + P.rp_int;
            cpush(&s.qc[f].rp, ccode(P.rp_int, c.ord++), c.cur);
        }
    }
    else
    {
        q.t_rp = c.now + P.rp_int;
        cpush(&s.qc[f].rp, ccode(P.rp_int, c.ord++), c.cur);
        if (q.rp_stage >= P.rpg_threshold)
        {
            q.target += q.rp_stage == P.rpg_threshold ? P.rai : P.rhai;
            if (q.target > P.bps)
                q.target = P.bps;
        }
        q.rate = rate_of((double)q.rate / 2) + rate_of((double)q.target / 2);
        q.rp_stage++;
    }
}

// RdmaHw::ReceiveAck (ACK and NACK)
__device__ __forceinline__ void h_ack(HCtx &c, const Pkt &p)
{
    const Sim &s = *c.s;
    int f = p.flow;
    SndQP &q = s.qp[f];
    if (q.done)
    {
        atomicAdd(&s.ev[10], 1ull); // m_qpMap no longer has it
        return;
    }
    if (p.seq > q.snd_una)
        q.snd_una = p.seq;
    if (q.snd_una >= q.size)
    {
        // QpComplete
        q.t_alpha = q.t_dec = q.t_rp = -1;
        q.done = 1;
        s.fct[f] = c.now - s.fi[f].start;
        atomicAdd(s.flows_done, 1u);
    }
    if (p.kind == K_NACK)
        q.snd_nxt = q.snd_una;
    if (p.cnp && s.P.cc == 1 && !q.done) // (after QpComplete its timers would touch nothing else)
        dcqcn_cnp(c, f, q);
    if (s.P.cc == 3)
    {
        if (p.seq > q.last_update_seq)
            hpcc_update(c, q, p, false);
        else if (s.P.fast_react)
            hpcc_update(c, q, p, true);
    }
    h_dequeue(c);
}

// RdmaHw::ReceiveUdp
__device__ __forceinline__ void h_data(HCtx &c, const Pkt &p)
{
    const Sim &s = *c.s;
    RcvQP &r = s.rx[p.flow];
    uint32_t payload = p.size - 48 - s.P.int_size;
    // data for a flow received whole: if the sender has completed by now, ns-3 has deleted the
    // receiving queue pair, and a new one would answer with a NACK (not modelled; needs a drop)
    if (r.expected >= s.fi[p.flow].size)
        atomicAdd(&s.ev[13], 1ull);
    // ReceiverCheckSeq (m_milestone_rx is reset to the ACK interval on every packet)
    int x;
    uint32_t expected = r.expected;
    if (p.seq == expected)
    {
        r.expected = expected + payload;
        if (r.expected >= s.P.ack_interval)
            x = 1;
        else if (s.P.chunk && r.expected % s.P.chunk == 0)
            x = 1;
        else
            x = 5;
    }
    else if (p.seq > expected)
    {
        if (c.now >= r.nack_timer || r.last_nack != expected)
        {
            r.nack_timer = c.now + s.P.nack_interval;
            r.last_nack = expected;
            x = 2;
        }
        else
            x = 4;
    }
    else
        x = 3;
    if (x != 1 && x != 2)
        return;
    if (x == 2)
        atomicAdd(&s.ev[11], 1ull);
    Pkt a = p;
    a.kind = x == 1 ? K_ACK : K_NACK;
    a.seq = r.expected;
    a.cnp = p.ecn ? 1 : 0;
    a.ecn = 0;
    a.size = (uint16_t)ack_size(s.P);
    if (c.st.ackq_n >= s.ackq_cap)
    {
        atomicAdd(&s.ev[6], 1ull);
        return;
    }
    s.ackq[(size_t)c.h * s.ackq_cap + (c.st.ackq_head + c.st.ackq_n) % s.ackq_cap] = a;
    c.st.ackq_n++;
    h_dequeue(c);
}

// RdmaClient::StartApplication -> RdmaHw::AddQueuePair -> QbbNetDevice::NewQp
__device__ __forceinline__ void h_start(HCtx &c, int f)
{
    const Sim &s = *c.s;
    const FlowInfo &fi = s.fi[f];
    SndQP &q = s.qp[f];
    q = SndQP{};
    q.size = fi.size;
    q.rate = q.max_rate = s.P.bps;
    q.win = s.P.win;
    q.base_rtt = s.P.base_rtt;
    q.alpha = 1;
    q.first_cnp = 1;
    q.target = s.P.bps;
    q.t_alpha = q.t_dec = q.t_rp = -1;
    q.cur_rate = s.P.bps;
    q.u = 1;
    q.next_avail = c.now;
    if (c.st.qp_n >= s.qps_cap)
    {
        atomicAdd(&s.ev[7], 1ull);
        return;
    }
    s.qps[(size_t)c.h * s.qps_cap + c.st.qp_n++] = f;
    h_dequeue(c);
}

struct Due
{
    int64_t at;
    int flow, kind;
};
constexpr int MAX_DUE = 64;

__device__ const Chain &due_chain(const Sim &s, const Due &d)
{
    const QPChains &qc = s.qc[d.flow];
    return d.kind == 0 ? qc.alpha : d.kind == 1 ? qc.dec : qc.rp;
}
__device__ __forceinline__ int64_t due_now(const Sim &s, const Due &d)
{
    const SndQP &q = s.qp[d.flow];
    return d.kind == 0 ? q.t_alpha : d.kind == 1 ? q.t_dec : q.t_rp;
}

// a host's arrival of the slot in LDS: its frame (the index in s.msg for its chain), its arrival's
// code, and for an HPCC ACK the U and dt of UpdateRateHp computed ahead
struct HArr
{
    int64_t ts;
    Pkt p;
    uint32_t acode, frame;
    double U;
    uint64_t dt;
    int pre;
};
constexpr int HPOOL = 64;

// A host's slot, run by the lanes of one wave: they load the slot's arrivals into LDS and compute
// HPCC's per-hop utilizations of its ACKs (with FAST_REACT and without SAMPLE_FEEDBACK an ACK's
// depend only on it and on the hops of the QP's previous ACK, so they need not wait for the
// events); lane 0 then runs the events.
__device__ __forceinline__ bool host_step(const Sim &s, int h, int64_t slot, int64_t *wake, int lane, int lanes, HArr *pool, int *hints)
{
    const int64_t D = s.P.delay, t0 = slot * D, t1 = t0 + D;
    const int link_in = s.in_link[(size_t)h * s.K];
    if (lane == 0)
    {
        // the arrivals: at most the last frame of slot s - 2, the frames of slot s - 1 but perhaps the last
        int an2 = slot >= 2 ? msg_count(s, slot - 2, link_in) : 0, an1 = slot >= 1 ? msg_count(s, slot - 1, link_in) : 0;
        const LFrame *a2 = s.msg + mi(s, slot - 2, link_in) * MAXF, *a1 = s.msg + mi(s, slot - 1, link_in) * MAXF;
        int f2 = an2, n1 = an1;
        while (f2 > 0 && a2[f2 - 1].ts >= t0)
            f2--;
        while (n1 > 0 && a1[n1 - 1].ts >= t1)
            n1--;
        hints[0] = an2 - f2 + n1;
        hints[1] = f2;
        hints[2] = an2;
    }
    __syncthreads();
    const int total = hints[0], f2 = hints[1], an2 = hints[2];
    const bool pooled = total <= HPOOL;
    if (pooled)
        for (int j = lane; j < total; j += lanes)
        {
            int n2 = an2 - f2;
            uint32_t fi = j < n2 ? (uint32_t)(mi(s, slot - 2, link_in) * MAXF + f2 + j) : (uint32_t)(mi(s, slot - 1, link_in) * MAXF + (j - n2));
            const LFrame &f = s.msg[fi];
            HArr &a = pool[j];
            a.ts = f.ts;
            a.p = f.p;
            a.acode = arrival_code(s, f);
            a.frame = fi;
            a.pre = 0;
        }
    __syncthreads();
    // (with FAST_REACT every ACK of a QP calls UpdateRateHp, which stores its hops; without it,
    // only those that end an RTT do)
    if (pooled && s.P.cc == 3 && s.P.fast_react && !s.P.sample_feedback)
        for (int j = lane; j < total; j += lanes)
        {
            HArr &a = pool[j];
            if ((a.p.kind != K_ACK && a.p.kind != K_NACK) || a.p.nhop > MAX_HOP)
                continue;
            // the hops the QP will hold: the previous ACK's of this slot, or the stored ones
            int k = j - 1;
            while (k >= 0 && !(pool[k].p.flow == a.p.flow && (pool[k].p.kind == K_ACK || pool[k].p.kind == K_NACK)))
                k--;
            const SndQP &q = s.qp[a.p.flow];
            hpcc_hops(s.P, a.p.hop, a.p.nhop, k >= 0 ? pool[k].p.hop : q.hop, q.max_rate, q.win, &a.U, &a.dt);
            a.pre = 1;
        }
    __syncthreads();
    if (lane != 0)
        return false;
    HCtx c;
    c.s = &s;
    c.h = h;
    c.st = s.hs[h];
    c.slot = slot;
    c.link = s.out_link[(size_t)h * s.K];
    c.out = s.msg + mi(s, slot, c.link) * MAXF;
    c.nout = 0;
    c.pre_ok = false;
    Arrivals in;
    in.init(s, link_in, slot, t0);
    int ai = 0; // the next pooled arrival
    // the DCQCN timers due in this slot (their intervals are at least a slot, so none is added)
    Due due[MAX_DUE];
    int nd = 0;
    const int *list = s.qps + (size_t)h * s.qps_cap;
    if (s.P.cc == 1)
        for (int i = 0; i < c.st.qp_n; i++)
        {
            const SndQP &q = s.qp[list[i]];
            int64_t t[3] = {q.t_alpha, q.t_dec, q.t_rp};
            for (int k = 0; k < 3; k++)
                if (t[k] >= 0 && t[k] < t1)
                {
                    if (nd == MAX_DUE)
                    {
                        atomicAdd(&s.ev[12], 1ull);
                        continue;
                    }
                    Due d = {t[k], list[i], k};
                    int j = nd++;
                    while (j > 0 && before(s, d.at, 0, due_chain(s, d), due[j - 1].at, 0, due_chain(s, due[j - 1])))
                    {
                        due[j] = due[j - 1];
                        j--;
                    }
                    due[j] = d;
                }
        }
    int di = 0;
    uint32_t acode = 0;
    int64_t ats = 0, sf_t = INT64_MAX;
    int sf = -1, sf_at = -1;
    const LFrame *af = nullptr;
    bool act = false;
    for (;;)
    {
        // candidates: 0 arrival, 1 TransmitComplete, 2 m_nextSend, 3 a DCQCN timer, 4 a flow start
        // (the arrival's and the flow start's times are kept between events)
        if (!af)
        {
            if (pooled)
            {
                if (ai < total)
                {
                    af = &s.msg[pool[ai].frame];
                    acode = pool[ai].acode;
                    ats = pool[ai].ts;
                }
            }
            else
            {
                af = in.peek(t1);
                if (af)
                {
                    acode = arrival_code(s, *af);
                    ats = af->ts;
                }
            }
        }
        while (di < nd && due_now(s, due[di]) != due[di].at)
            di++; // cancelled or moved
        if (sf_at != c.st.next_start)
        {
            sf_at = c.st.next_start;
            sf = sf_at < c.st.start_end ? s.start_list[sf_at] : -1;
            sf_t = sf >= 0 ? s.fi[sf].start : INT64_MAX;
        }
        // a candidate's chain is cpush(code(k), chain(k)). The selection keeps only indices: a
        // version that carried a pointer to the best candidate's chain along gave wrong results at
        // -O2 and above (the pointer was stale at a flow start), and right ones at -O1
        auto code = [&](int k) -> uint32_t { return k == 0 ? acode : 0; };
        auto chain = [&](int k) -> const Chain & {
            return k == 0 ? af->c : k == 1 ? s.h_tc[h] : k == 2 ? s.h_ns[h] : k == 3 ? due_chain(s, due[di]) : s.fi[sf].start_chain;
        };
        int64_t ct[5];
        ct[0] = af ? ats : INT64_MAX;
        ct[1] = c.st.tc_at >= 0 && c.st.tc_at < t1 ? c.st.tc_at : INT64_MAX;
        ct[2] = c.st.ns_at >= 0 && c.st.ns_at < t1 ? c.st.ns_at : INT64_MAX;
        ct[3] = di < nd ? due[di].at : INT64_MAX;
        ct[4] = sf_t < t1 ? sf_t : INT64_MAX;
        int best = -1;
        for (int k = 0; k < 5; k++)
            if (ct[k] != INT64_MAX && (best < 0 || before(s, ct[k], code(k), chain(k), ct[best], code(best), chain(best))))
                best = k;
        if (best < 0)
            break;
        c.now = ct[best];
        if (code(best))
            cpush(&c.cur, code(best), chain(best));
        else
            c.cur = chain(best);
        c.ord = 0;
        trace_event(h, c.now, best == 0 ? "arrival" : best == 1 ? "tc" : best == 2 ? "nextsend" : best == 3 ? "timer" : "start", 0, c.cur);
        if (best == 0)
        {
            Pkt p = af->p;
            if (pooled)
            {
                c.pre_ok = pool[ai].pre;
                c.pre_U = pool[ai].U;
                c.pre_dt = pool[ai].dt;
                ai++;
            }
            else
                in.pop();
            af = nullptr;
            if (p.kind == K_DATA)
                h_data(c, p);
            else if (p.kind == K_ACK || p.kind == K_NACK)
            {
                h_ack(c, p);
                c.pre_ok = false;
            }
            else if (s.P.qbb)
            {
                if (p.kind == K_PAUSE)
                    c.st.paused |= 1u << p.q;
                else
                {
                    c.st.paused &= ~(1u << p.q);
                    h_dequeue(c);
                }
            }
        }
        else if (best == 1)
        {
            c.st.tc_at = -1;
            h_dequeue(c);
        }
        else if (best == 2)
        {
            c.st.ns_at = -1;
            h_dequeue(c);
        }
        else if (best == 3)
        {
            Due d = due[di++];
            dcqcn_timer(c, d.flow, d.kind);
        }
        else
        {
            c.st.next_start++;
            h_start(c, sf);
        }
    }
    if (c.nout)
        s.msg_cnt[mi(s, slot, c.link)] = (long long)slot << 16 | c.nout;
    act = c.nout > 0 || c.st.ackq_n > 0 || c.st.tc_at >= 0;
    // the earliest event without input
    int64_t w = INT64_MAX;
    if (c.st.ns_at >= 0)
        w = c.st.ns_at;
    if (c.st.next_start < c.st.start_end)
    {
        int64_t t = s.fi[s.start_list[c.st.next_start]].start;
        w = t < w ? t : w;
    }
    if (s.P.cc == 1)
        for (int i = 0; i < c.st.qp_n; i++)
        {
            const SndQP &q = s.qp[list[i]];
            int64_t t[3] = {q.t_alpha, q.t_dec, q.t_rp};
            for (int k = 0; k < 3; k++)
                if (t[k] >= 0 && t[k] < w)
                    w = t[k];
        }
    *wake = w;
    s.hs[h] = c.st;
    return act;
}

// ------------------------------------------------------------------ switch
constexpr int KMAX = 64;

// A switch's slot workspace in LDS, per port: its Port, its arrivals of this slot (a range of the
// pool, or, when the pool overflows, a cursor in the frames of slots s - 2 then s - 1), and the
// prefixes of its next arrival's chain and of its pending TransmitComplete's (whose chain is
// tc[port], in global memory). It is kept small: LDS bounds how many switches a CU holds.
struct SwPortLds
{
    Port pt;
    int64_t ats;       // the next arrival's time (INT64_MAX: none)
    int ai, aend;      // pool: the next arrival and the end of the port's range
    int an2, an1, link_in, nout; // the fallback cursor (ai) and its frame counts; link; frames sent
    uint32_t acode;    // the next arrival's code
    Run ap[PRE], tp[PRE]; // the prefixes of the next arrival's chain and of tc's
};

// an arrival of the slot in the pool: its frame's contents, the code and prefix of its arrival's
// chain, where its frame lies (for the chain itself), and its route
struct ArrLds
{
    int64_t ts;
    Pkt p;
    Run ap[PRE];
    uint32_t acode;
    uint32_t frame; // index in s.msg
    int16_t out;    // the egress port (-1: a PFC frame, or no route)
    uint8_t q, in;  // its queue; the ingress port
};
constexpr int POOL = 1024; // arrivals per switch and slot in the pool (global memory)

struct SCtx
{
    const Sim *s;
    int x, node;
    SwPortLds *w; // [deg]
    Chain *tc;    // [deg] the pending TransmitCompletes' chains (s.p_tc)
    ArrLds *pool; // null: this slot uses the fallback
    Switch sw;
    int64_t now, slot;
    // the running event's chain, made on first use: cpush(cur_code, *cur_src) (code 0: a copy)
    Chain cur;
    const Chain *cur_src;
    uint32_t cur_code;
    bool cur_ok;
    int ord;
    // a safe slot (see switch_step): each lane runs one egress port's events; the MMU's ingress
    // counters are summed with LDS atomics, and shared_used is recomputed at the end
    bool safe;
};

__device__ __forceinline__ const Chain &sc_cur(SCtx &c)
{
    if (!c.cur_ok)
    {
        if (c.cur_code)
            cpush(&c.cur, c.cur_code, *c.cur_src);
        else
            c.cur = *c.cur_src;
        c.cur_ok = true;
    }
    return c.cur;
}

// SwitchMmu, priority PG
__device__ __forceinline__ uint32_t pfc_thresh(const SCtx &c)
{
    const Params &P = c.s->P;
    return (P.buffer_size - c.sw.total_hdrm - c.sw.total_rsrv - c.sw.shared_used) >> P.pfc_shift;
}
__device__ __forceinline__ uint32_t shared_used(const Port &p) { return p.ing > RESERVE ? p.ing - RESERVE : 0; }

__device__ __forceinline__ void s_transmit(SCtx &c, int port, Pkt p)
{
    const Sim &s = *c.s;
    int64_t tx = link_tx(s, p.size);
    c.w[port].pt.tc_at = c.now + tx;
    const Chain &cur = sc_cur(c);
    Chain tcc;
    cpush(&tcc, ccode(tx, c.ord++), cur);
    c.tc[port] = tcc;
    cprefix(c.w[port].tp, 0, tcc);
    int link = s.out_link[(size_t)c.node * s.K + port];
    if (c.w[port].nout >= MAXF)
    {
        atomicAdd(&s.ev[4], 1ull);
        return;
    }
    LFrame &f = s.msg[mi(s, c.slot, link) * MAXF + c.w[port].nout++];
    p.ord = (uint8_t)(c.ord < 255 ? c.ord : 255);
    c.ord++;
    f.ts = c.now + tx + s.P.delay;
    f.c = cur;
    f.p = p;
    trace_tx(s, c.now, c.node, port, p);
}

__device__ __forceinline__ void s_enqueue(SCtx &c, int port, int q, const Pkt &p)
{
    const Sim &s = *c.s;
    Port &pt = c.w[port].pt;
    size_t pi = (size_t)c.x * s.K + port;
    if (q == 0)
    {
        if (pt.q0_n >= s.q0_cap)
        {
            atomicAdd(&s.ev[5], 1ull);
            return;
        }
        s.q0[pi * s.q0_cap + (pt.q0_head + pt.q0_n) % s.q0_cap] = p;
        pt.q0_n++;
    }
    else
    {
        if (pt.q3_n >= s.q3_cap)
        {
            atomicAdd(&s.ev[5], 1ull);
            return;
        }
        s.q3[pi * s.q3_cap + (pt.q3_head + pt.q3_n) % s.q3_cap] = p;
        pt.q3_n++;
    }
    pt.bytes_total += p.size;
}

// DequeueAndTransmit when queue 0 has a frame: it goes first, and SwitchNotifyDequeue does nothing
// for queue 0 (its frames are PFC frames, or ACKs with ACK_HIGH_PRIO, so no INT either)
__device__ __forceinline__ void s_start_q0(SCtx &c, int port)
{
    const Sim &s = *c.s;
    Port &pt = c.w[port].pt;
    if (pt.tc_at >= 0)
        return;
    size_t pi = (size_t)c.x * s.K + port;
    Pkt p = s.q0[pi * s.q0_cap + pt.q0_head];
    pt.q0_head = (pt.q0_head + 1) % s.q0_cap;
    pt.q0_n--;
    pt.bytes_total -= p.size;
    pt.tx_bytes += p.size;
    s_transmit(c, port, p);
}

// QbbNetDevice::SendPfc: into queue 0 of the port, then DequeueAndTransmit
__device__ __forceinline__ void s_send_pfc(SCtx &c, int port, int type)
{
    Pkt p = {};
    p.kind = type == 0 ? K_PAUSE : K_RESUME;
    p.q = PG;
    p.size = PFC_SIZE;
    atomicAdd(&c.s->ev[type == 0 ? 2 : 3], 1ull);
    s_enqueue(c, port, 0, p);
    if (c.w[port].pt.q0_n > 0)
        s_start_q0(c, port);
}

__device__ __forceinline__ void s_start_q3(SCtx &c, int port, Pkt p);

// QbbNetDevice::DequeueAndTransmit on a switch (DequeueRR, SwitchNotifyDequeue)
__device__ __forceinline__ void s_dequeue(SCtx &c, int port)
{
    const Sim &s = *c.s;
    const Params &P = s.P;
    Port &pt = c.w[port].pt;
    if (pt.tc_at >= 0)
        return;
    if (pt.q0_n > 0)
    {
        s_start_q0(c, port);
        return;
    }
    if ((pt.paused >> PG & 1) || pt.q3_n == 0)
        return;
    Pkt p = s.q3[((size_t)c.x * s.K + port) * s.q3_cap + pt.q3_head];
    pt.q3_head = (pt.q3_head + 1) % s.q3_cap;
    pt.q3_n--;
    pt.bytes_total -= p.size;
    s_start_q3(c, port, p);
}

// the rest of DequeueAndTransmit for a frame of queue PG, out of the queue (SwitchNotifyDequeue,
// TransmitStart)
__device__ __forceinline__ void s_start_q3(SCtx &c, int port, Pkt p)
{
    const Sim &s = *c.s;
    const Params &P = s.P;
    Port &pt = c.w[port].pt;
    Port &in = c.w[p.inport].pt;
    if (c.safe)
    {
        // no headroom anywhere, no port paused: RemoveFromIngressAdmission takes it from ing
        atomicSub(&in.ing, (uint32_t)p.size);
        pt.egr -= p.size;
    }
    else
    {
        // RemoveFromIngressAdmission, RemoveFromEgressAdmission
        uint32_t from_hdrm = in.hdrm < p.size ? in.hdrm : p.size;
        uint32_t sh = in.ing > RESERVE ? in.ing - RESERVE : 0;
        uint32_t from_shared = p.size - from_hdrm < sh ? p.size - from_hdrm : sh;
        in.hdrm -= from_hdrm;
        in.ing -= p.size - from_hdrm;
        c.sw.shared_used -= from_shared;
        pt.egr -= p.size;
#ifdef RDMA_TRACE_NODE
        if (c.node == RDMA_TRACE_NODE && c.now >= RDMA_TRACE_FROM && c.now <= RDMA_TRACE_UNTIL)
            printf("mmu %lld node %d in %d out %d deq %u ing %u hdrm %u shared %u thresh %u pkt %d>%d %u q3n %d\n", (long long)c.now, c.node, p.inport + 1,
                   port + 1, p.size, in.ing, in.hdrm, c.sw.shared_used, pfc_thresh(c), p.src, p.dst, p.seq, pt.q3_n);
#endif
    }
    if (P.ecn)
    {
        // SwitchMmu::ShouldSendCN
        bool mark = false;
        if (pt.egr > P.kmax)
            mark = true;
        else if (pt.egr > P.kmin)
        {
            double pr = P.pmax * (double)(pt.egr - P.kmin) / (double)(P.kmax - P.kmin);
            mark = nsm_rng::rand_u01(pt.rng) < pr;
        }
        if (mark)
        {
            p.ecn = 3;
            atomicAdd(&s.ev[1], 1ull);
        }
    }
    // CheckAndSendResume (never in a safe slot: no port is paused)
    if (!c.safe && in.mmu_paused)
    {
        uint32_t su = shared_used(in);
        if (in.hdrm == 0 && (su == 0 || su + RESUME_OFFSET <= pfc_thresh(c)))
        {
            s_send_pfc(c, p.inport, 1);
            in.mmu_paused = 0;
        }
    }
    if (p.kind == K_DATA && P.cc == 3 && p.nhop < 0xFFFF)
    {
        p.hop[p.nhop % MAX_HOP] = hop_make((uint64_t)c.now, pt.tx_bytes, pt.bytes_total, P.bps, P.int_multi);
        p.nhop++;
    }
    pt.tx_bytes += p.size;
    s_transmit(c, port, p);
}

// SwitchNode::SendToDev
// the egress port of a data frame or ACK (-1: no route) and its queue
__device__ __forceinline__ int s_route(const Sim &s, int x, const Pkt &p, int *q)
{
    bool data = p.kind == K_DATA;
    int dst = data ? p.dst : p.src;
    const Route &rt = s.rt[(size_t)x * s.H + dst];
    *q = (s.P.ack_high_prio && !data) ? 0 : PG;
    if (rt.n == 0)
        return -1;
    uint32_t w2 = data ? (p.sport | 100u << 16) : (100u | (uint32_t)p.sport << 16);
    uint32_t hsh = ecmp_hash(node_ip(data ? p.src : p.dst), node_ip(dst), w2, (uint32_t)(s.H + x));
    return rt.port[hsh % rt.n];
}

__device__ __forceinline__ void s_forward(SCtx &c, int inport, Pkt p, int out, int q)
{
    const Sim &s = *c.s;
    if (out < 0)
        return;
    if (q != 0 && c.safe)
    {
        // below every threshold for the whole slot: no drop, no headroom, no PAUSE; only ing
        // changes (shared_used follows from it at the end of the slot)
        atomicAdd(&c.w[inport].pt.ing, (uint32_t)p.size);
        c.w[out].pt.egr += p.size;
    }
    else if (q != 0)
    {
        Port &in = c.w[inport].pt;
        uint32_t th = pfc_thresh(c);
        // CheckIngressAdmission
        if (p.size + in.hdrm > c.sw.headroom && p.size + shared_used(in) > th)
        {
            atomicAdd(&s.ev[0], 1ull);
            return;
        }
        // UpdateIngressAdmission, UpdateEgressAdmission
        uint32_t nb = in.ing + p.size;
        if (nb <= RESERVE)
            in.ing += p.size;
        else if (nb - RESERVE > th)
            in.hdrm += p.size;
        else
        {
            in.ing += p.size;
            c.sw.shared_used += p.size < nb - RESERVE ? p.size : nb - RESERVE;
        }
        c.w[out].pt.egr += p.size;
#ifdef RDMA_TRACE_NODE
        if (c.node == RDMA_TRACE_NODE && c.now >= RDMA_TRACE_FROM && c.now <= RDMA_TRACE_UNTIL)
            printf("mmu %lld node %d in %d out %d enq %u ing %u hdrm %u shared %u thresh %u pkt %d>%d %u q3n %d\n", (long long)c.now, c.node, inport + 1,
                   out + 1, p.size, in.ing, in.hdrm, c.sw.shared_used, pfc_thresh(c), p.src, p.dst, p.seq, c.w[out].pt.q3_n);
#endif
        // CheckAndSendPfc
        if (!in.mmu_paused && (in.hdrm > 0 || shared_used(in) >= pfc_thresh(c)))
        {
            s_send_pfc(c, inport, 0);
            in.mmu_paused = 1;
        }
    }
    p.inport = (uint8_t)inport;
    Port &o = c.w[out].pt;
    if (q != 0 && o.tc_at < 0 && o.q0_n == 0 && o.q3_n == 0 && !(o.paused >> PG & 1))
    {
        // an idle port with empty queues: Enqueue and DequeueAndTransmit hand it straight on
        s_start_q3(c, out, p);
        return;
    }
    s_enqueue(c, out, q, p);
    s_dequeue(c, out);
}

__device__ __forceinline__ const LFrame &sw_frame(const Sim &s, const SwPortLds &w, int64_t slot)
{
    return w.ai < w.an2 ? s.msg[mi(s, slot - 2, w.link_in) * MAXF + w.ai] : s.msg[mi(s, slot - 1, w.link_in) * MAXF + (w.ai - w.an2)];
}

// the next arrival of a port in [t0, t1), or none (INT64_MAX)
__device__ __forceinline__ void sw_peek(const SCtx &c, SwPortLds &w, int64_t t1)
{
    const Sim &s = *c.s;
    w.ats = INT64_MAX;
    if (c.pool)
    {
        if (w.ai < w.aend)
        {
            const ArrLds &a = c.pool[w.ai];
            w.ats = a.ts;
            w.acode = a.acode;
            for (int i = 0; i < PRE; i++)
                w.ap[i] = a.ap[i];
        }
        return;
    }
    if (w.ai < w.an2 + w.an1)
    {
        const LFrame &f = sw_frame(s, w, c.slot);
        if (f.ts < t1)
        {
            w.ats = f.ts;
            w.acode = arrival_code(s, f);
            cprefix(w.ap, w.acode, f.c);
        }
    }
}

// the chain of a port's next arrival (without its own code)
__device__ __forceinline__ const Chain &sw_arr_chain(const SCtx &c, const SwPortLds &w)
{
    return c.pool ? c.s->msg[c.pool[w.ai].frame].c : sw_frame(*c.s, w, c.slot).c;
}

// A switch's slot, run by the lanes of one wave. They load its ports, this slot's arrivals (into
// the pool, with their routes) and the heads of its queues into LDS together. Then:
//  - in a safe slot, each lane runs one egress port's events: the arrivals routed to it (or PFC
//    frames arriving at it) and its TransmitCompletes, in their order. A slot is safe when no port
//    is paused by the MMU, none has headroom bytes, and every port stays below the lowest PFC
//    threshold the slot can reach even if all of its arrivals stay: then no MMU decision
//    (drop, headroom, PAUSE, RESUME) can occur, ports interact only through the sums in their
//    ingress counters, and shared_used = sum max(0, ing - reserve) follows at the end;
//  - otherwise lane 0 runs all events in order.
// Last, the lanes store the ports back together.
constexpr int SAFE_LANES = 32; // safe slots need a lane per port
// the LDS int workspace of a switch with K ports, then LK arrival keys: their times in the slot,
// egress ports, and two index lists (uint16, uint8, 2 x uint16)
constexpr int LK = 512;
__host__ __device__ inline size_t sw_ints(int K) { return 6 * (size_t)K + (size_t)K * K + (LK * 7 + 3) / 4; }
__device__ __forceinline__ bool switch_step(const Sim &s, int x, int64_t slot, SwPortLds *w, ArrLds *pool, int *pidx, int lane, int lanes,
                                            int *shared_int)
{
    Chain *tcs = s.p_tc + (size_t)x * s.K;
    const int64_t D = s.P.delay, t0 = slot * D, t1 = t0 + D;
    const int node = s.H + x, deg = s.deg[node];
    Port *gp = s.ps + (size_t)x * s.K;
    // ports, and the bounds of their arrivals: at most the last frame of slot s - 2 (a frame
    // arrives within one transmission of the link's delay), and the frames of slot s - 1 but
    // perhaps the last
    const int K = s.K;
    int *cnt = shared_int, *first = shared_int + K; // per port: arrivals, the first in slot s - 2's list
    uint32_t *abytes = (uint32_t *)(shared_int + 2 * K); // bytes arriving for queue PG
    uint32_t *ing0 = (uint32_t *)(shared_int + 3 * K);   // ing at the slot's start
    int *ocnt = shared_int + 4 * K, *obase = shared_int + 5 * K; // safe: arrivals per egress port
    int *mcur = shared_int + 6 * K;                       // safe: merge cursors [egress][ingress]
    // safe: per arrival its time in the slot and its egress port; pool indices by egress port, in
    // order; the next arrival of its ingress with the same egress
    uint16_t *kts = (uint16_t *)(mcur + K * K), *olist = kts + LK, *onext = olist + LK;
    int8_t *own = (int8_t *)(onext + LK);
    (void)pidx;
    for (int p = lane; p < deg; p += lanes)
    {
        SwPortLds &q = w[p];
        q.pt = gp[p];
        abytes[p] = 0;
        ocnt[p] = 0;
        ing0[p] = q.pt.ing;
        q.link_in = s.in_link[(size_t)node * s.K + p];
        q.an2 = slot >= 2 ? msg_count(s, slot - 2, q.link_in) : 0;
        q.an1 = slot >= 1 ? msg_count(s, slot - 1, q.link_in) : 0;
        q.nout = 0;
        const LFrame *a2 = s.msg + mi(s, slot - 2, q.link_in) * MAXF, *a1 = s.msg + mi(s, slot - 1, q.link_in) * MAXF;
        int f2 = q.an2;
        while (f2 > 0 && a2[f2 - 1].ts >= t0)
            f2--;
        int n1 = q.an1;
        while (n1 > 0 && a1[n1 - 1].ts >= t1)
            n1--;
        first[p] = f2;
        cnt[p] = (q.an2 - f2) + n1;
        if (q.pt.tc_at >= 0)
            cprefix(q.tp, 0, tcs[p]);
    }
    __syncthreads();
    // the pool: ports' arrivals in order, if they fit
    int total = 0;
    for (int p = 0; p < deg; p++)
        total += cnt[p];
    bool pooled = total <= POOL;
    if (pooled)
        for (int j = lane; j < total; j += lanes)
        {
            int p = 0, base = 0;
            while (j - base >= cnt[p])
                base += cnt[p++];
            int k = j - base, an2 = w[p].an2, n2 = an2 - first[p];
            uint32_t fi = k < n2 ? (uint32_t)(mi(s, slot - 2, w[p].link_in) * MAXF + first[p] + k)
                                 : (uint32_t)(mi(s, slot - 1, w[p].link_in) * MAXF + (k - n2));
            const LFrame &f = s.msg[fi];
            ArrLds &a = pool[j];
            a.ts = f.ts;
            a.p = f.p;
            a.acode = arrival_code(s, f);
            cprefix(a.ap, a.acode, f.c);
            a.frame = fi;
            int q = 0;
            a.out = (int16_t)(f.p.kind == K_PAUSE || f.p.kind == K_RESUME ? -1 : s_route(s, x, f.p, &q));
            a.q = (uint8_t)q;
            a.in = (uint8_t)p;
            if (a.out >= 0 && q != 0)
                atomicAdd(&abytes[p], (uint32_t)f.p.size);
            // its egress port in a safe slot: the route's, or its own port for a PFC frame (a
            // frame without a route belongs to none)
            int ow = a.out >= 0 ? a.out : (f.p.kind == K_PAUSE || f.p.kind == K_RESUME ? p : -1);
            if (ow >= 0)
                atomicAdd(&ocnt[ow], 1);
            if (j < LK)
            {
                kts[j] = (uint16_t)(f.ts - t0);
                own[j] = (int8_t)ow;
            }
        }
    __syncthreads();
    // is the slot safe? (every lane decides alike)
    bool safe = pooled && total <= LK && D <= 65535 && deg <= lanes && deg <= SAFE_LANES && !s.serial;
    if (safe)
    {
        const Switch &sw = s.sw[x];
        int64_t amax = sw.shared_used;
        for (int p = 0; p < deg; p++)
        {
            amax += abytes[p];
            if (w[p].pt.hdrm || w[p].pt.mmu_paused)
                safe = false;
        }
        int64_t room = (int64_t)s.P.buffer_size - sw.total_hdrm - sw.total_rsrv - amax;
        int64_t th_min = room > 0 ? room >> s.P.pfc_shift : -1;
        for (int p = 0; p < deg && safe; p++)
        {
            int64_t su = (int64_t)w[p].pt.ing + abytes[p] - RESERVE;
            if ((su > 0 ? su : 0) >= th_min)
                safe = false;
        }
    }
    bool act = false;
    if (safe)
    {
        // each egress port's arrivals (counted above), merged from the ingress ports in order: lane
        // p links its port's arrivals to the next one with the same egress, then lane o merges
        if (lane < deg)
        {
            int p = lane, b = 0;
            for (int q = 0; q < p; q++)
                b += cnt[q];
            for (int o = 0; o < deg; o++)
                mcur[o * K + p] = -1;
            for (int k = b + cnt[p] - 1; k >= b; k--)
            {
                int o = own[k];
                if (o < 0)
                    continue;
                onext[k] = (uint16_t)mcur[o * K + p]; // (-1, the end: 0xFFFF)
                mcur[o * K + p] = k;
            }
        }
        __syncthreads();
        if (lane < deg)
        {
            int o = lane, base = 0;
            for (int p = 0; p < o; p++)
                base += ocnt[p];
            obase[o] = base;
            int *cur = mcur + o * K;
            for (int n = 0; n < ocnt[o]; n++)
            {
                int bp = -1;
                for (int p = 0; p < deg; p++)
                {
                    int k = cur[p];
                    if (k < 0)
                        continue;
                    if (bp < 0)
                    {
                        bp = p;
                        continue;
                    }
                    int kb = cur[bp];
                    if (kts[k] != kts[kb] ? kts[k] < kts[kb] : ({
                            const ArrLds &A = pool[k], &B = pool[kb];
                            int r = pcmp(A.ap, B.ap);
                            r ? r < 0 : before(s, A.ts, A.acode, s.msg[A.frame].c, B.ts, B.acode, s.msg[B.frame].c);
                        }))
                        bp = p;
                }
                olist[base + n] = cur[bp];
                int nx = onext[cur[bp]];
                cur[bp] = nx == 0xFFFF ? -1 : nx;
            }
        }
        if (lane < deg)
        {
            const int o = lane;
            SCtx c;
            c.s = &s;
            c.x = x;
            c.node = node;
            c.sw = s.sw[x];
            c.slot = slot;
            c.w = w;
            c.tc = tcs;
            c.pool = pool;
            c.safe = true;
            int oi = obase[o], oe = obase[o] + ocnt[o];
            SwPortLds &me = w[o];
            for (;;)
            {
                int64_t ta = oi < oe ? t0 + kts[olist[oi]] : INT64_MAX, tc = me.pt.tc_at;
                bool htc = tc >= 0 && tc < t1;
                if (ta == INT64_MAX && !htc)
                    break;
                bool arr;
                if (!htc || ta == INT64_MAX)
                    arr = !htc;
                else if (ta != tc)
                    arr = ta < tc;
                else
                {
                    const ArrLds &a = pool[olist[oi]];
                    int r = pcmp(a.ap, me.tp);
                    arr = r ? r < 0 : before(s, ta, a.acode, s.msg[a.frame].c, tc, 0, tcs[o]);
                }
                c.ord = 0;
                c.cur_ok = false;
                if (!arr)
                {
                    c.now = tc;
                    c.cur_src = &tcs[o];
                    c.cur_code = 0;
                    trace_event(c.node, c.now, "tc", o, sc_cur(c));
                    me.pt.tc_at = -1;
                    s_dequeue(c, o);
                    continue;
                }
                const ArrLds &a = pool[olist[oi++]];
                c.now = ta;
                c.cur_src = &s.msg[a.frame].c;
                c.cur_code = a.acode;
                trace_event(c.node, c.now, "arrival", a.in, sc_cur(c));
                Pkt pk = a.p;
                if (pk.kind == K_PAUSE || pk.kind == K_RESUME)
                {
                    if (s.P.qbb)
                    {
                        if (pk.kind == K_PAUSE)
                            me.pt.paused |= 1u << pk.q;
                        else
                        {
                            me.pt.paused &= ~(1u << pk.q);
                            s_dequeue(c, o);
                        }
                    }
                }
                else
                    s_forward(c, a.in, pk, a.out, a.q);
            }
        }
        __syncthreads();
        if (lane == 0)
        {
            // shared_used = sum over ports of max(0, ing - reserve), which changed with ing only
            Switch sw = s.sw[x];
            for (int p = 0; p < deg; p++)
            {
                uint32_t a = ing0[p] > RESERVE ? ing0[p] - RESERVE : 0, b = w[p].pt.ing > RESERVE ? w[p].pt.ing - RESERVE : 0;
                sw.shared_used += b - a;
            }
            s.sw[x] = sw;
#ifdef RDMA_PROF
            atomicAdd(&s.ev[31], 1ull);
#endif
        }
    }
    else if (lane == 0)
    {
        SCtx c;
        c.s = &s;
        c.x = x;
        c.node = node;
        c.sw = s.sw[x];
        c.slot = slot;
        c.w = w;
        c.tc = tcs;
        c.pool = pooled ? pool : nullptr;
        c.safe = false;
        int base = 0;
        for (int p = 0; p < deg; p++)
        {
            if (pooled)
            {
                w[p].ai = base;
                w[p].aend = base + cnt[p];
                base += cnt[p];
            }
            else
                w[p].ai = first[p] < w[p].an2 ? first[p] : w[p].an2;
            sw_peek(c, w[p], t1);
        }
#ifdef RDMA_PROF
        unsigned long long nev = 0;
        unsigned long long ph[4] = {0, 0, 0, 0};
        long long q0 = clock64();
#endif
        for (;;)
        {
            // the earliest event: a port's next arrival or its TransmitComplete
            int best = -1, bk = 0;
            int64_t bt = INT64_MAX;
            for (int p = 0; p < deg; p++)
            {
                int64_t ta = w[p].ats, tc = w[p].pt.tc_at;
                for (int k = 0; k < 2; k++)
                {
                    int64_t t = k ? tc : ta;
                    if (k ? (tc < 0 || tc >= t1 || tc > bt) : (ta == INT64_MAX || ta > bt))
                        continue;
                    if (t < bt || best < 0)
                    {
                        best = p, bk = k, bt = t;
                        continue;
                    }
                    // a tie: the prefixes, else the whole chains
                    const Run *pa = k ? w[p].tp : w[p].ap, *pb = bk ? w[best].tp : w[best].ap;
                    int r = pcmp(pa, pb);
#ifdef RDMA_PROF
                    atomicAdd(&s.ev[28], 1ull);
                    if (r == 0)
                        atomicAdd(&s.ev[29], 1ull);
#endif
                    if (r == 0)
                    {
                        uint32_t ca = k ? 0 : w[p].acode, cb = bk ? 0 : w[best].acode;
                        const Chain &xa = k ? tcs[p] : sw_arr_chain(c, w[p]);
                        const Chain &xb = bk ? tcs[best] : sw_arr_chain(c, w[best]);
                        r = before(s, t, ca, xa, bt, cb, xb) ? -1 : 1;
                    }
                    if (r < 0)
                        best = p, bk = k;
                }
            }
            if (best < 0)
                break;
            int p = best;
            c.now = bt;
            c.ord = 0;
            c.cur_ok = false;
#ifdef RDMA_PROF
            nev++;
            long long q1 = clock64();
            ph[0] += q1 - q0;
            q0 = q1;
#endif
            if (bk == 1)
            {
                c.cur_src = &tcs[p];
                c.cur_code = 0;
                trace_event(c.node, c.now, "tc", p, sc_cur(c));
                w[p].pt.tc_at = -1;
                s_dequeue(c, p);
#ifdef RDMA_PROF
                q1 = clock64();
                ph[2] += q1 - q0;
                q0 = q1;
#endif
                continue;
            }
            Pkt pk;
            int out, q = PG;
            c.cur_code = w[p].acode;
            if (c.pool)
            {
                const ArrLds &a = c.pool[w[p].ai];
                c.cur_src = &s.msg[a.frame].c;
                pk = a.p;
                out = a.out;
                q = a.q;
            }
            else
            {
                const LFrame &f = sw_frame(s, w[p], slot);
                c.cur_src = &f.c;
                pk = f.p;
                out = pk.kind == K_PAUSE || pk.kind == K_RESUME ? -1 : s_route(s, x, pk, &q);
            }
#ifdef RDMA_PROF
            q1 = clock64();
            ph[1] += q1 - q0;
            q0 = q1;
#endif
            trace_event(c.node, c.now, "arrival", p, sc_cur(c));
            w[p].ai++;
            sw_peek(c, w[p], t1);
            if (pk.kind == K_PAUSE || pk.kind == K_RESUME)
            {
                if (s.P.qbb)
                {
                    if (pk.kind == K_PAUSE)
                        w[p].pt.paused |= 1u << pk.q;
                    else
                    {
                        w[p].pt.paused &= ~(1u << pk.q);
                        s_dequeue(c, p);
                    }
                }
            }
            else
                s_forward(c, p, pk, out, q);
#ifdef RDMA_PROF
            q1 = clock64();
            ph[3] += q1 - q0;
            q0 = q1;
#endif
        }
        s.sw[x] = c.sw;
#ifdef RDMA_PROF
        for (int i = 0; i < 4; i++)
            atomicAdd(&s.ev[24 + i], ph[i]);
        atomicAdd(&s.ev[20], nev);
        atomicMax(&s.ev[22], nev);
        if (!pooled)
            atomicAdd(&s.ev[30], 1ull);
#endif
    }
    __syncthreads();
    for (int p = lane; p < deg; p += lanes)
    {
        if (w[p].nout)
            s.msg_cnt[mi(s, slot, s.out_link[(size_t)node * s.K + p])] = (long long)slot << 16 | w[p].nout;
        if (w[p].nout > 0 || w[p].pt.q0_n > 0 || w[p].pt.q3_n > 0 || w[p].pt.tc_at >= 0)
            act = true;
        gp[p] = w[p].pt;
    }
    return act;
}

// ------------------------------------------------------------------ the slot kernel
// The end of a block's slot: its activity and wake-up time; the last block of the slot (of either
// kernel) decides the next slot.
__device__ __forceinline__ void slot_end(const Sim &s, int64_t slot, int blk_act, unsigned long long blk_wake)
{
    if (blk_act)
        atomicAdd(&s.activity[0], 1u);
    if (blk_wake != ~0ull)
        atomicMin(s.next_wake, blk_wake);
    __threadfence();
    unsigned int ticket = atomicAdd(s.done_blocks, 1u);
    if (ticket != (unsigned int)(s.H + s.S) - 1)
        return;
    __threadfence();
    unsigned int fd = atomicAdd(s.flows_done, 0u);
    if (fd >= (unsigned int)s.F || slot + 1 >= s.max_slots)
        *s.finished = 1;
    // idle skip: nothing sent in this slot or the previous one (so nothing arrives in the
    // next) and nothing queued: go on at the slot of the earliest host event
    unsigned int cur = atomicAdd(&s.activity[0], 0u), prev = s.activity[1];
    unsigned long long nw = atomicAdd(s.next_wake, 0ull);
    int64_t next = slot + 1;
    if (cur == 0 && prev == 0 && nw != ~0ull)
    {
        int64_t ws = (int64_t)nw / s.P.delay;
        if (ws > next)
            next = ws < s.max_slots ? ws : s.max_slots;
    }
#ifdef RDMA_PROF
    s.ev[18] += s.ev[16];
    s.ev[19] += s.ev[17];
    s.ev[23] += s.ev[16] > s.ev[17] ? s.ev[16] : s.ev[17];
    s.ev[16] = s.ev[17] = 0;
    s.ev[21] += s.ev[22];
    s.ev[22] = 0;
#endif
    s.activity[1] = cur;
    s.activity[0] = 0;
    *s.next_wake = ~0ull;
    *s.slot = next;
    *s.done_blocks = 0;
    __threadfence();
}

// A block (one wave) per host and per switch. A block's lanes load its slot's inputs together,
// then the first lane runs its events (entities sharing a wave would diverge and run in turn).
// Their LDS is kept small: it bounds how many blocks a CU holds.
__device__ __forceinline__ void host_block(const Sim &s, int h, char *smem)
{
    int64_t slot = *s.slot;
    int lane = threadIdx.x, lanes = blockDim.x;
#ifdef RDMA_PROF
    long long p0 = clock64();
#endif
    int64_t w = INT64_MAX;
    HArr *pool = reinterpret_cast<HArr *>(smem);
    int *hints = reinterpret_cast<int *>(smem + HPOOL * sizeof(HArr));
    bool act = host_step(s, h, slot, &w, lane, lanes, pool, hints);
#ifdef RDMA_PROF
    if (lane == 0)
        atomicMax(&s.ev[16], (unsigned long long)(clock64() - p0));
#endif
    if (lane == 0)
        slot_end(s, slot, act, w != INT64_MAX ? (unsigned long long)w : ~0ull);
}

__device__ __forceinline__ void switch_block(const Sim &s, int x, char *smem)
{
    int64_t slot = *s.slot;
    int lane = threadIdx.x, lanes = blockDim.x;
#ifdef RDMA_PROF
    long long p0 = clock64();
#endif
    // a switch's workspace: its ports and ints in LDS, its arrival pool and its index lists in
    // global memory
    SwPortLds *w = reinterpret_cast<SwPortLds *>(smem);
    int *ints = reinterpret_cast<int *>(smem + (size_t)s.K * sizeof(SwPortLds));
    ArrLds *pool = s.apool + (size_t)x * POOL;
    int *pidx = s.aidx + (size_t)x * 2 * POOL;
    __shared__ int blk_act;
    if (lane == 0)
        blk_act = 0;
    __syncthreads();
    if (switch_step(s, x, slot, w, pool, pidx, lane, lanes, ints))
        blk_act = 1; // (any lane: the ports are spread over them)
    __syncthreads();
#ifdef RDMA_PROF
    if (lane == 0)
        atomicMax(&s.ev[17], (unsigned long long)(clock64() - p0));
#endif
    if (lane == 0)
        slot_end(s, slot, blk_act, ~0ull);
}

// the slot kernel: blocks 0 .. H - 1 are the hosts, then the switches
__global__ void rdma_slot(Sim s)
{
    extern __shared__ __align__(16) char smem[];
    if (*s.finished)
        return;
    if (blockIdx.x < s.H)
        host_block(s, blockIdx.x, smem);
    else
        switch_block(s, blockIdx.x - s.H, smem);
}

} // namespace rdma
