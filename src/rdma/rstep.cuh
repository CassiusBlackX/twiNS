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
// ns-3.36's DataRate::CalculateBytesTxTime: Seconds(bytes * 8) / bps, an integer division
__host__ __device__ __forceinline__ int64_t tx_ns36(uint64_t bps, uint32_t bytes) { return (int64_t)udiv64((uint64_t)bytes * 8000000000ull, bps); }
__host__ __device__ __forceinline__ int64_t tx_ns(int simai, uint64_t bps, uint32_t bytes) { return simai ? tx_ns36(bps, bytes) : tx_ns(bps, bytes); }

// tx_ns(a link's rate, bytes) for frames below TXT_N bytes: c_txt for rate class 0 (the only one
// with uniform links), s.txt for all (main fills them)
constexpr int TXT_N = 9216;
__constant__ int32_t c_txt[TXT_N];
__device__ __forceinline__ int64_t link_tx(const Sim &s, int link, uint32_t bytes)
{
    if (s.R == 1 && bytes < TXT_N)
        return c_txt[bytes];
    const LinkInfo &l = s.li[link];
    if (bytes >= TXT_N)
        return tx_ns(s.P.simai, l.bps, bytes);
    return s.txt[(size_t)l.rc * TXT_N + bytes];
}

// A sender's place in a link's ring: the bucket it fills (a link's frames arrive in order, so its
// buckets follow one another) and the frames in it. The count goes to s.msg_cnt when the sender
// moves on to the next bucket and at the end of its slot (flush). The link's ring and rate are
// copied in when the sender's slot starts (link_open), and the sender, the bucket counts' only
// writer, keeps the last it wrote (last: slot << 16 | frames, kept between slots in Host and Port
// ob_last): a frame needs no global load to find its place. txs/txv: its last two tx times.
struct OutBucket
{
    int64_t lo, hi; // its slot's times (hi = 0: none)
    uint32_t base;  // its first frame in s.msg
    uint32_t cnt;   // its count in s.msg_cnt
    uint16_t n, cap;
    int link;
    uint32_t coff, rc;
    uint16_t nb;
    uint8_t nb_sh, nb_add; // nb's FDiv, packed (a larger OutBucket put the frames in scratch)
    uint64_t nb_m;
    uint64_t moff, bps;
    long long last;
    uint32_t txs[2];
    int32_t txv[2];
};
__device__ __forceinline__ void link_open(OutBucket &o, int link, const LinkInfo &l, long long last)
{
    o.hi = 0;
    o.link = link;
    o.coff = l.coff;
    o.rc = l.rc;
    o.nb = l.nb;
    o.nb_m = l.fnb.m;
    o.nb_sh = (uint8_t)l.fnb.sh;
    o.nb_add = (uint8_t)l.fnb.add;
    o.cap = l.cap;
    o.moff = l.moff;
    o.bps = l.bps;
    o.last = last;
    o.txs[0] = o.txs[1] = 0;
}
// link_tx for the sender's link
__device__ __forceinline__ int64_t link_txo(const Sim &s, OutBucket &o, uint32_t bytes)
{
    if (s.R == 1 && bytes < TXT_N)
        return c_txt[bytes];
    if (bytes >= TXT_N)
        return tx_ns(s.P.simai, o.bps, bytes);
    if (o.txs[0] == bytes)
        return o.txv[0];
    if (o.txs[1] == bytes)
        return o.txv[1];
    int32_t t = s.txt[(size_t)o.rc * TXT_N + bytes];
    o.txs[1] = o.txs[0], o.txv[1] = o.txv[0];
    o.txs[0] = bytes, o.txv[0] = t;
    return t;
}
__device__ __forceinline__ void link_flush(const Sim &s, OutBucket &o)
{
    if (o.hi == 0)
        return;
    o.last = (long long)fdiv(s.fD, o.lo) << 16 | o.n;
    s.msg_cnt[o.cnt] = o.last;
    o.hi = 0;
}
// a frame for the link, arriving at ts: a place in the bucket of its arrival slot (null: the
// bucket is full, counted as an overflow)
__device__ __forceinline__ LFrame *link_push(const Sim &s, int64_t ts, OutBucket &o)
{
    if (ts >= o.hi)
    {
        link_flush(s, o);
        int64_t a = (int64_t)fdiv(s.fD, ts);
        int b = (int)(a - fdiv(o.nb_m, o.nb_sh, o.nb_add, a) * o.nb);
        o.cnt = o.coff + b;
        o.lo = a * s.P.delay;
        o.hi = o.lo + s.P.delay;
        o.base = (uint32_t)(o.moff + (uint64_t)b * o.cap);
        o.n = (o.last >> 16) == a ? (uint16_t)(o.last & 0xFFFF) : 0;
        s.pend[fmod(s.fpend, a)] = 1;
#ifdef RDMA_PROF
        if (s.any_win && s.hz[s.link_dst[o.link]] > a)
            atomicAdd(&s.ev[33], 1ull); // its receiver has run the slot (a bug: reported; profiling builds)
#endif
    }
    if (o.n >= o.cap)
    {
        atomicAdd(&s.ev[4], 1ull);
        return nullptr;
    }
    return s.msg + o.base + o.n++;
}

// (a + b) % n for a ring's index a and an offset b, both nonnegative (a division's routine
// otherwise; a ring's head is below n and its count at most n: one step)
__device__ __forceinline__ int ring_add(int a, int b, int n)
{
    int x = a + b;
    return x >= n ? x - n : x;
}

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
// remembered comparisons (see ccmp): key | result, open addressing
struct Memo
{
    unsigned long long *t;
    uint64_t mask;
};
__device__ Memo g_memo;
#ifdef RDMA_PROF
__device__ unsigned long long g_prof[8]; // ccmp calls, walks, walk steps, memo hits, inserts
#endif

// a chain's hash after one more code: of its lead only (splitmix64's finalizer). Ordinals decide
// only between chains of one root (see ccmp)
__host__ __device__ __forceinline__ uint64_t chash(uint64_t h, uint32_t code)
{
    uint64_t z = h + 0x9e3779b97f4a7c15ull * ((code >> 3) + 1ull);
    z = (z ^ (z >> 30)) * 0xbf58476d1ce4e5b9ull;
    z = (z ^ (z >> 27)) * 0x94d049bb133111ebull;
    return z ^ (z >> 31);
}

// the chain of an event scheduled with `code` by the event whose chain is par
__host__ __device__ __forceinline__ void cpush(Chain *dst, uint32_t code, const Chain &par)
{
    Chain c;
    if (par.r[0].code == code)
    {
        c = par;
        if (c.r[0].cnt < 0xFFFFFFFFu)
            c.r[0].cnt++;
        c.hash = chash(par.hash, code);
    }
    else
    {
        c.r[0].code = code;
        c.r[0].cnt = 1;
        for (int i = 1; i < RUNS; i++)
            c.r[i] = par.r[i - 1];
        c.root = par.root;
        c.tail = par.tail;
        c.hash = chash(par.hash, code);
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
    c.anc[0] = par.hash;
    for (int i = 1; i < NANC; i++)
        c.anc[i] = par.anc[i - 1];
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
    __device__ __forceinline__ void init(const Chain &ch, uint32_t code0 = 0)
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
    __device__ __forceinline__ void load()
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
    __device__ __forceinline__ void next()
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

// Comparisons decided by the leads (a first differing lead, or one chain ending first) depend only
// on the two chains' leads, so they are remembered by their lead hashes. In lockstep traffic (as
// in collectives) two ports see the same pair of lineages slot after slot, each extended by the
// same leads: after its equal-lead top elements a comparison is one made before. ccmp looks the pair
// up at depths 0 .. NANC (the ancestors' hashes), walks only if none is known, and remembers what
// the walk found for the depths whose top leads were equal. (A memo key is 64 bits of the two
// hashes: a collision, at about 2^-64 per lookup, would decide a tie wrongly.)
__device__ __forceinline__ uint64_t memo_key(uint64_t ha, uint64_t hb)
{
    uint64_t k = chash(ha * 0x9e3779b97f4a7c15ull ^ hb, 0x5bd1e995u) & ~3ull;
    return k ? k : 4;
}
// 0: unknown, else -1 or 1 (a runs first)
__device__ __noinline__ int memo_get(uint64_t ha, uint64_t hb)
{
    uint64_t k = memo_key(ha, hb);
    for (int i = 0; i < 4; i++)
    {
        unsigned long long v = g_memo.t[(k + i) & g_memo.mask];
        if (v == 0)
            return 0;
        if ((v & ~3ull) == k)
            return (v & 3) == 1 ? -1 : 1;
    }
    return 0;
}
__device__ __forceinline__ void memo_put(uint64_t ha, uint64_t hb, int r)
{
    uint64_t k = memo_key(ha, hb);
    unsigned long long v = k | (r < 0 ? 1 : 2);
    for (int i = 0; i < 4; i++)
    {
        unsigned long long *p = &g_memo.t[(k + i) & g_memo.mask];
        unsigned long long old = atomicCAS(p, 0ull, v);
        if (old == 0 || (old & ~3ull) == k)
            return;
    }
}

// < 0: a runs first, > 0: b first, 0: the chains do not tell; *inexact: the decision rests on a
// chain whose tail was overwritten. Going back from the events, the first differing lead decides
// (the larger: an earlier ancestor). If every lead is equal, the ancestors are the same event from
// the deepest level at which the ordinals differ, and there the smaller ordinal (the sibling
// scheduled first) decides; distinct roots (flows' StartApplications) decide in start order.
// (*by_lead: decided by a lead or by one chain ending first, which depend on the leads only)
// (inline_only: give up, returning 2, where either chain goes on into its tail)
__device__ __forceinline__ int ccmp_walk(uint32_t pa, const Chain &a, uint32_t pb, const Chain &b, bool *inexact, bool *by_lead = nullptr,
                                         bool inline_only = false)
{
    *inexact = false;
    if (by_lead)
        *by_lead = false;
    int ord = 0;
    RunCursor A, B;
    A.init(a, pa);
    B.init(b, pb);
    auto by_leads = [&](int r) {
        if (by_lead)
            *by_lead = true;
        return r;
    };
    for (;;)
    {
        if (inline_only && (A.i > RUNS || B.i > RUNS))
            return 2;
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
                return A.lost ? ord : by_leads(-1);
            return B.lost ? ord : by_leads(1);
        }
        if (ca >> 3 != cb >> 3)
            return by_leads(ca > cb ? -1 : 1);
        if (ca != cb)
            ord = (ca & 7) > (cb & 7) ? -1 : 1;
        // at the same tail node, at the same place in it: the rest is one lineage (the chains met at
        // a common ancestor), so the deepest ordinal difference is one seen above
        if (A.i > RUNS && B.i > RUNS && A.node == B.node && A.left == B.left)
            return ord;
        uint32_t m = A.left < B.left ? A.left : B.left;
        A.left -= m;
        B.left -= m;
        if (A.left == 0)
            A.next();
        if (B.left == 0)
            B.next();
    }
}

// ccmp_walk, sped up: chains of distinct roots with equal leads above their roots (equal hashes:
// lineages in lockstep, whose walk would go all the way down) compare as their roots' start chains
// (there are their first differing lead, and the end where the roots decide; ordinals decide only
// below a common root); comparisons decided by leads come from the memo when known
__device__ __forceinline__ int ccmp(const Sim &s, uint32_t pa, const Chain &a, uint32_t pb, const Chain &b, bool *inexact)
{
#ifdef RDMA_PROF
    atomicAdd(&g_prof[0], 1ull);
#endif
    // the hashes of the chains compared (a virtual push pa on a) and of their ancestors
    uint64_t ha[NANC + 1], hb[NANC + 1];
    ha[0] = pa ? chash(a.hash, pa) : a.hash;
    hb[0] = pb ? chash(b.hash, pb) : b.hash;
    for (int i = 1; i <= NANC; i++)
    {
        ha[i] = pa ? (i == 1 ? a.hash : a.anc[i - 2]) : a.anc[i - 1];
        hb[i] = pb ? (i == 1 ? b.hash : b.anc[i - 2]) : b.anc[i - 1];
    }
    if (a.root && b.root && a.root != b.root && ha[0] == hb[0])
    {
        *inexact = false;
        return s.root_rank[a.root - 1] < s.root_rank[b.root - 1] ? -1 : 1;
    }
    // the top elements' leads, one at a time: depth j is usable when the top j leads are equal
    RunCursor A, B;
    A.init(a, pa);
    B.init(b, pb);
    int eq = 0; // top elements with equal leads (at most NANC)
    for (int j = 0;; j++)
    {
        int m = memo_get(ha[j], hb[j]);
        if (m)
        {
#ifdef RDMA_PROF
            atomicAdd(&g_prof[3], 1ull);
#endif
            *inexact = false;
            return m;
        }
        if (j == NANC || A.code == 0 || B.code == 0 || (A.code >> 3) != (B.code >> 3))
            break;
        eq = j + 1;
        if (--A.left == 0)
            A.next();
        if (--B.left == 0)
            B.next();
    }
    bool by_lead;
    int r = ccmp_walk(pa, a, pb, b, inexact, &by_lead);
#ifdef RDMA_PROF
    atomicAdd(&g_prof[1], 1ull);
#endif
    // a decision by the leads: not the end of both chains (roots, ordinals), nor a lost tail
    if (by_lead && !*inexact)
    {
        for (int j = 0; j <= eq; j++)
            memo_put(ha[j], hb[j], r);
#ifdef RDMA_PROF
        atomicAdd(&g_prof[4], 1ull);
#endif
    }
    return r;
}

// the first three runs of the chain cpush(code0, ch) (code0 0: of ch itself)
constexpr int PRE = 3;
#ifndef RANK_SORT
#define RANK_SORT 0
#endif
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

// a tie by what a switch keeps in LDS: chains of distinct roots with equal lead hashes compare as
// their roots (see ccmp), else the prefixes; 0: undecided
__device__ __forceinline__ int qcmp(const Sim &s, const Run *pa, uint64_t ha, uint32_t ra, const Run *pb, uint64_t hb, uint32_t rb)
{
    if (ra && rb && ra != rb && ha == hb)
        return s.root_rank[ra - 1] < s.root_rank[rb - 1] ? -1 : 1;
    return pcmp(pa, pb);
}

// a candidate event: does (ta, a) run before (tb, b)? A chain is given as cpush(p, chain) (p 0:
// the chain itself). Undecided ties keep b (the earlier candidate).
__device__ __forceinline__ bool before(const Sim &s, int64_t ta, uint32_t pa, const Chain &a, int64_t tb, uint32_t pb, const Chain &b)
{
    if (ta != tb)
        return ta < tb;
    bool inexact;
    int c = ccmp(s, pa, a, pb, b, &inexact);
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

// ns-3's Hash32: MurmurHash3_x86_32 with seed 0x8BADF00D (hash-murmur3.cc)
__host__ __device__ __forceinline__ uint32_t ns3_hash32(const uint8_t *key, int len)
{
    const uint32_t c1 = 0xcc9e2d51, c2 = 0x1b873593;
    uint32_t h = 0x8BADF00D;
    int nblocks = len / 4;
    for (int i = 0; i < nblocks; i++)
    {
        uint32_t k = (uint32_t)key[4 * i] | (uint32_t)key[4 * i + 1] << 8 | (uint32_t)key[4 * i + 2] << 16 | (uint32_t)key[4 * i + 3] << 24;
        k *= c1;
        k = (k << 15) | (k >> 17);
        k *= c2;
        h ^= k;
        h = (h << 13) | (h >> 19);
        h = h * 5 + 0xe6546b64;
    }
    const uint8_t *tail = key + 4 * nblocks;
    uint32_t k = 0;
    switch (len & 3)
    {
    case 3:
        k ^= tail[2] << 16;
        [[fallthrough]];
    case 2:
        k ^= tail[1] << 8;
        [[fallthrough]];
    case 1:
        k ^= tail[0];
        k *= c1;
        k = (k << 15) | (k >> 17);
        k *= c2;
        h ^= k;
    }
    h ^= (uint32_t)len;
    h ^= h >> 16;
    h *= 0x85ebca6b;
    h ^= h >> 13;
    h *= 0xc2b2ae35;
    h ^= h >> 16;
    return h;
}
// a queue pair's pick among n devices: Hash32 of its (sip, dip, sport, dport)
__host__ __device__ __forceinline__ uint32_t qp_pick(uint32_t sip, uint32_t dip, uint16_t sport, uint16_t dport, uint32_t n)
{
    uint8_t b[12] = {(uint8_t)sip, (uint8_t)(sip >> 8), (uint8_t)(sip >> 16), (uint8_t)(sip >> 24),
                     (uint8_t)dip, (uint8_t)(dip >> 8), (uint8_t)(dip >> 16), (uint8_t)(dip >> 24),
                     (uint8_t)sport, (uint8_t)(sport >> 8), (uint8_t)dport, (uint8_t)(dport >> 8)};
    return ns3_hash32(b, 12) % n;
}

// ------------------------------------------------------------------ DAG
// A DAG message for flow f's source host: an installing event (SendFlow) at tI with chain E and
// ordinal ord, so its StartApplication at tI + send_lat with chain E (0, ord) (send_lat, 0). It is
// filed in the slot send_lat after the firing event (at tE: the host learns of it before it can
// start; a traced DAG's timers can put tI later).
__device__ __noinline__ void dag_msg(const Sim &s, int f, int64_t tE, int64_t tI, const Chain &E, int ord)
{
    int64_t ts = tI + s.send_lat;
    int64_t a = (int64_t)fdiv(s.fD, tE + s.send_lat);
    int b = (int)fmod(s.fnbi, a);
    unsigned int n = atomicAdd(&s.dmsg_n[b], 1u);
    if (n >= (unsigned)s.dcap)
    {
        atomicAdd(&s.ev[14], 1ull);
        return;
    }
    DMsg &m = s.dmsg[(size_t)b * s.dcap + n];
    Chain x;
    cpush(&x, ccode(0, ord), E);
    cpush(&m.c, ccode(s.send_lat, 0), x);
    m.ts = ts;
    m.flow = f;
    const FlowInfo &cf = s.fi[f];
    m.next = atomicExch(&s.hhead[(size_t)cf.src * s.nbi + b], n + 1);
    if (cf.fast)
        atomicAdd(&s.srv_fpend[s.h_srv[cf.src]], 1); // (no quiet windows in its server until it starts)
    if (s.any_win && s.hz[cf.src] > a)
        atomicAdd(&s.ev[33], 1ull);
    s.pend[fmod(s.fpend, a)] = 1;
}

// Flow g's dependency of kind k (0 received: its QpComplete; 1 sent: its first SendComplete) fired
// in an event at tE with chain E; base: the ordinals the event had used. Each child of that kind
// is installed in it (SendFlow), the i-th with ordinal base + i (its index among the event's
// children of that kind, in id order). A traced DAG's cause has actions instead: flows installed
// and timers scheduled, by their ordinals; a timer fires its own (timers are expanded here: they
// touch nothing else). Returns the ordinals used (the event's later ones follow them).
__device__ __noinline__ int dag_fire(const Sim &s, int g, int k, int64_t tE, const Chain &E, int base)
{
    int i = 0;
    for (int j = s.ch_off[g]; j < s.ch_off[g + 1]; j++)
    {
        uint32_t v = s.ch_list[j];
        if ((int)(v & 1) != k)
            continue;
        dag_msg(s, v >> 1, tE, tE, E, base + i);
        i++;
    }
    if (!s.traced)
        return i;
    // the timers fired wait in a list through s.tnext (no local array, no pointer to E: either
    // would put the host's context in scratch); each fires its actions from its global chain
    int used = 0, head = -1;
    const int cause = 2 * g + k;
    for (int j = s.act_off[cause]; j < s.act_off[cause + 1]; j++)
    {
        DAct a = s.act[j];
        used = (int)a.ord + 1; // (by ordinal)
        if (a.child & DACT_TIMER)
        {
            int t = a.child & ~DACT_TIMER;
            s.ttime[t] = tE + s.tdelay[t];
            cpush(&s.tchain[t], ccode(s.tdelay[t], base + a.ord), E);
            s.tnext[t] = head;
            head = t;
        }
        else
            dag_msg(s, a.child, tE, tE, E, base + a.ord);
    }
    while (head >= 0)
    {
        int t = head;
        head = s.tnext[t];
        for (int j = s.act_off[2 * s.F + t]; j < s.act_off[2 * s.F + t + 1]; j++)
        {
            DAct a = s.act[j];
            if (a.child & DACT_TIMER)
            {
                int u = a.child & ~DACT_TIMER;
                s.ttime[u] = s.ttime[t] + s.tdelay[u];
                cpush(&s.tchain[u], ccode(s.tdelay[u], a.ord), s.tchain[t]); // (a timer's callback is its event's first)
                s.tnext[u] = head;
                head = u;
            }
            else
                dag_msg(s, a.child, tE, s.ttime[t], s.tchain[t], a.ord);
        }
    }
    return i + used;
}

// ------------------------------------------------------------------ frames
__host__ __device__ __forceinline__ uint32_t node_ip(int id) { return 0x0b000001u + (id / 256) * 0x00010000u + (id % 256) * 0x00000100u; }

__device__ __forceinline__ uint32_t data_hdr(const Params &P) { return 48 + P.seq_extra + P.int_size; }
__device__ __forceinline__ uint32_t data_size(const Params &P, uint32_t payload) { return payload + data_hdr(P); }
__device__ __forceinline__ uint32_t ack_size(const Params &P)
{
    int base = 12 + P.seq_extra + P.int_size, pad = 60 - 14 - 20 - base;
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
    uint64_t b = multi == 1 ? bytes >> 7 : udiv64(bytes, 128ull * multi), q = multi == 1 ? qlen / 80u : qlen / (80u * multi);
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

// a link's frames arriving in the slot, in order
struct Arrivals
{
    const LFrame *a;
    int na, i;
    __device__ __forceinline__ void init(const Sim &s, int link, int64_t slot) // (a call would put it in scratch)
    {
        uint32_t base;
        na = bucket(s, link, slot, &base);
        a = s.msg + base;
        i = 0;
    }
    __device__ __forceinline__ const LFrame *peek() const { return i < na ? &a[i] : nullptr; }
    __device__ __forceinline__ void pop() { i++; }
};

// a frame's arrival over a link of the given delay: [lead tx + delay, its ordinal] + the chain of
// its start
__device__ __forceinline__ uint32_t arrival_code(const Sim &s, int link, int64_t delay, const LFrame &f)
{
    return ccode(link_tx(s, link, f.p.size) + delay, f.p.ord);
}

// ------------------------------------------------------------------ host: the NICs
// A chain's quick form for ties (qcmp): its first runs, lead hash and root
struct QPre
{
    Run r[PRE];
    uint64_t h;
    uint32_t root;
};
__device__ __forceinline__ void qpre_of(QPre &q, uint32_t code0, const Chain &ch)
{
    cprefix(q.r, code0, ch);
    q.h = code0 ? chash(ch.hash, code0) : ch.hash;
    q.root = ch.root;
}

// A host has a device per link (a GPU: an NVLink port and one or two NICs). Each has its own
// QbbNetDevice and RdmaEgressQueue state (Host, its TransmitComplete and m_nextSend chains, its ACK
// queue and queue-pair group: the per-NIC arrays, at s.nic_off[h] + device); RdmaHw (queue pairs)
// is shared. The host's flow starts live in its first device's Host.
constexpr int MAXD = 4;
// a host's devices' TransmitComplete, m_nextSend and next arrival's chains in quick form (lane 0's,
// made when a tie needs them; hq_ok bits: d, MAXD + d, 2 MAXD + d)
__shared__ QPre hq_tc[MAXD], hq_ns[MAXD], hq_ar[MAXD];
__shared__ unsigned hq_ok;
template <int ND>
struct HCtx
{
    const Sim *s;
    const Params *P; // its world's
    int h;
    bool pre_ok;     // the running ACK's HPCC U and dt were computed ahead
    double pre_U;
    uint64_t pre_dt;
    int64_t now, slot;
    Chain cur; // the running event's chain
    int ord;   // events it has scheduled
    int nout;  // frames it sent this slot
    int nd, nic0;
    int d; // the current device (hsel)
    Host stv[ND];
    int lk[ND];
    int64_t ldl[ND];
    OutBucket obv[ND];
    // the current device's index (0 for ND 1: constant, so the state stays in registers), NIC
    // index, state, out link and its delay, out bucket
    __device__ __forceinline__ int cd() const { return ND == 1 ? 0 : d; }
    __device__ __forceinline__ int nic() const { return nic0 + cd(); }
    __device__ __forceinline__ Host &st() { return stv[cd()]; }
    __device__ __forceinline__ int link() const { return lk[cd()]; }
    __device__ __forceinline__ int64_t ldelay() const { return ldl[cd()]; }
    __device__ __forceinline__ OutBucket &ob() { return obv[cd()]; }
};

template <int ND>
__device__ __forceinline__ void hsel(HCtx<ND> &c, int d)
{
    c.d = ND == 1 ? 0 : d;
}

__device__ __forceinline__ uint64_t bytes_left(const SndQP &q) { return q.size >= q.snd_nxt ? q.size - q.snd_nxt : 0; }

// tx_ns(rate, bytes) for a QP, remembering the last one (PktSent and ChangeRate ask for the same
// rate and size in turn)
__device__ __forceinline__ int64_t qp_tx(const Params &P, SndQP &q, uint64_t rate, uint32_t bytes)
{
    if (rate != q.tx_rate || bytes != q.tx_size)
    {
        q.tx_rate = rate;
        q.tx_size = bytes;
        q.tx_val = tx_ns(P.simai, rate, bytes);
    }
    return q.tx_val;
}

__device__ __forceinline__ bool win_bound(const Params &P, const SndQP &q)
{
    if (q.win == 0)
        return false;
    uint64_t w = q.win;
    if (P.var_win)
    {
        w = udiv64((uint64_t)q.win * q.rate, q.max_rate);
        if (w == 0)
            w = 1;
    }
    return q.snd_nxt - q.snd_una >= w;
}

// QbbNetDevice::TransmitStart: TransmitComplete after tx, the peer's Receive after tx + delay
template <int ND>
__device__ __forceinline__ void h_transmit(HCtx<ND> &c, Pkt p)
{
    const Sim &s = *c.s;
    int64_t tx = link_txo(s, c.ob(), p.size);
    c.st().tc_at = c.now + tx;
    if (c.P->simai && p.size > 60 && p.size < 9000)
    {
        // ns-3-alibabacloud's TransmitStart first schedules SendCallback (RdmaHw::SendPacketComplete,
        // which fires SendComplete if the frame's (dip, sport, pg) finds a queue pair: a data frame)
        int k = c.ord++;
        if (p.kind == K_DATA && s.sent[p.flow] < 0)
        {
            s.sent[p.flow] = c.now + tx;
            if (s.dag)
            {
                Chain e;
                cpush(&e, ccode(tx, k), c.cur);
                dag_fire(s, p.flow, 1, c.now + tx, e, 0);
            }
        }
    }
    cpush(&s.h_tc[c.nic()], ccode(tx, c.ord++), c.cur);
    hq_ok &= ~(1u << c.d);
    p.ord = (uint8_t)(c.ord < 255 ? c.ord : 255);
    c.ord++;
    c.nout++;
    int64_t ts = c.now + tx + c.ldelay();
    LFrame *f = link_push(s, ts, c.ob());
    if (!f)
        return;
    f->ts = ts;
    f->c = c.cur;
    f->p = p;
    trace_tx(s, c.now, c.h, c.d, p);
}

template <int ND>
__device__ __forceinline__ void h_schedule_ns(HCtx<ND> &c, int64_t at)
{
    c.st().ns_at = at;
    cpush(&c.s->h_ns[c.nic()], ccode(at - c.now, c.ord++), c.cur);
    hq_ok &= ~(1u << (MAXD + c.d));
}

// RdmaEgressQueue::GetNextQindex: -1 the ACK queue, -1024 none, else the QP's index in the group
template <int ND>
__device__ __forceinline__ int next_qindex(HCtx<ND> &c)
{
    const Sim &s = *c.s;
    int ackq = c.P->ack_high_prio ? 0 : PG;
    if (!(c.st().paused >> ackq & 1) && c.st().ackq_n > 0)
        return -1;
    int *list = s.qps + (size_t)c.nic() * s.qps_cap;
    int res = -1024, fcount = c.st().qp_n, min_finish = INT_MAX;
    int idx = fcount ? c.st().rr % fcount : 0;
    for (int qi = 1; qi <= fcount; qi++)
    {
        idx = idx + 1 == fcount ? 0 : idx + 1; // (qi + m_rrlast) % fcount
        const SndQP &q = s.qp[list[idx]];
        if (!(c.st().paused >> PG & 1) && bytes_left(q) > 0 && !win_bound(*c.P, q))
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
        c.st().qp_n = nxt;
    }
    return res;
}

// QbbNetDevice::DequeueAndTransmit on a host
template <int ND>
__device__ __forceinline__ void h_dequeue(HCtx<ND> &c)
{
    const Sim &s = *c.s;
    if (c.st().tc_at >= 0)
        return;
    int idx = next_qindex(c);
    if (idx == -1)
    {
        Pkt p = s.ackq[(size_t)c.nic() * s.ackq_cap + c.st().ackq_head];
        c.st().ackq_head = ring_add(c.st().ackq_head, 1, s.ackq_cap);
        c.st().ackq_n--;
        h_transmit(c, p);
        return;
    }
    int *list = s.qps + (size_t)c.nic() * s.qps_cap;
    if (idx >= 0)
    {
        int f = list[idx];
        c.st().rr = idx;
        SndQP &q = s.qp[f];
        // RdmaHw::GetNxtPacket
        uint64_t left = bytes_left(q);
        uint32_t payload = left < c.P->mtu ? (uint32_t)left : c.P->mtu;
        Pkt p = {};
        p.kind = K_DATA;
        p.flow = f;
        p.src = (uint16_t)s.fi[f].src;
        p.dst = (uint16_t)s.fi[f].dst;
        p.sport = (uint16_t)s.fi[f].sport;
        p.seq = (uint32_t)q.snd_nxt;
        p.size = (uint16_t)data_size(*c.P, payload);
        q.snd_nxt += payload;
        h_transmit(c, p);
        // RdmaHw::PktSent, UpdateNextAvail
        q.last_pkt = p.size;
        q.next_avail = c.now + qp_tx(*c.P, q, c.P->rate_bound ? q.rate : q.max_rate, p.size);
        return;
    }
    int64_t t = INT64_MAX;
    for (int i = 0; i < c.st().qp_n; i++)
    {
        const SndQP &q = s.qp[list[i]];
        if (bytes_left(q) == 0 && !c.P->simai) // ns-3-alibabacloud dropped this skip
            continue;
        t = q.next_avail < t ? q.next_avail : t;
    }
    if (c.st().ns_at < 0 && t < INT64_MAX && t > c.now)
        h_schedule_ns(c, t);
}

// RdmaHw::ChangeRate (and QbbNetDevice::UpdateNextAvail)
template <int ND>
__device__ __forceinline__ void change_rate(HCtx<ND> &c, SndQP &q, uint64_t new_rate)
{
    int64_t st = qp_tx(*c.P, q, q.rate, q.last_pkt), nt = qp_tx(*c.P, q, new_rate, q.last_pkt);
    q.next_avail = q.next_avail + nt - st;
    if (c.st().ns_at >= 0 && q.next_avail < c.st().ns_at)
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
template <int ND>
__device__ __forceinline__ void hpcc_update(HCtx<ND> &c, SndQP &q, const Pkt &p, bool fast)
{
    const Params &P = *c.P;
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

// a DCQCN timer of q set for t: its device's lower bound (Host::due_min)
template <int ND>
__device__ __forceinline__ void due_note(HCtx<ND> &c, const SndQP &q, int64_t t)
{
    int64_t &m = c.stv[q.nic].due_min;
    if (t < m)
        m = t;
}

// RdmaHw::cnp_received_mlx
template <int ND>
__device__ __forceinline__ void dcqcn_cnp(HCtx<ND> &c, int f, SndQP &q)
{
    const Sim &s = *c.s;
    q.alpha_cnp = 1;
    q.dec_cnp = 1;
    if (q.first_cnp)
    {
        q.alpha = 1;
        q.alpha_cnp = 0;
        q.t_alpha = c.now + c.P->alpha_int;
        due_note(c, q, q.t_alpha);
        cpush(&s.qc[f].alpha, ccode(c.P->alpha_int, c.ord++), c.cur);
        q.t_dec = c.now + c.P->dec_int + 1;
        due_note(c, q, q.t_dec);
        cpush(&s.qc[f].dec, ccode(c.P->dec_int + 1, c.ord++), c.cur);
        q.target = q.rate = rate_of((double)q.rate * c.P->rate_first_cnp);
        q.first_cnp = 0;
    }
}

// the DCQCN timers: 0 UpdateAlphaMlx, 1 CheckRateDecreaseMlx, 2 RateIncEventTimerMlx
template <int ND>
__device__ __forceinline__ void dcqcn_timer(HCtx<ND> &c, int f, int kind)
{
    const Sim &s = *c.s;
    const Params &P = *c.P;
    SndQP &q = s.qp[f];
    if (kind == 0)
    {
        if (q.alpha_cnp)
            q.alpha = fma(1 - P.g, q.alpha, P.g); // as GCC contracts it
        else
            q.alpha = (1 - P.g) * q.alpha;
        q.alpha_cnp = 0;
        q.t_alpha = c.now + P.alpha_int;
        due_note(c, q, q.t_alpha);
        cpush(&s.qc[f].alpha, ccode(P.alpha_int, c.ord++), c.cur);
    }
    else if (kind == 1)
    {
        q.t_dec = c.now + P.dec_int;
        due_note(c, q, q.t_dec);
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
            due_note(c, q, q.t_rp);
            cpush(&s.qc[f].rp, ccode(P.rp_int, c.ord++), c.cur);
        }
    }
    else
    {
        q.t_rp = c.now + P.rp_int;
        due_note(c, q, q.t_rp);
        cpush(&s.qc[f].rp, ccode(P.rp_int, c.ord++), c.cur);
        if (q.rp_stage >= P.rpg_threshold)
        {
            q.target += q.rp_stage == P.rpg_threshold ? P.rai : P.rhai;
            if (q.target > q.max_rate) // the NIC's rate
                q.target = q.max_rate;
        }
        q.rate = rate_of((double)q.rate / 2) + rate_of((double)q.target / 2);
        q.rp_stage++;
    }
}

// RdmaHw::ReceiveAck (ACK and NACK)
template <int ND>
__device__ __forceinline__ void h_ack(HCtx<ND> &c, const Pkt &p)
{
    const Sim &s = *c.s;
    int f = p.flow;
    SndQP &q = s.qp[f];
    if (q.done)
    {
        atomicAdd(&s.ev[10], 1ull); // m_qpMap no longer has it
        return;
    }
    hsel(c, q.nic); // the queue pair's NIC (GetNicIdxOfQp): rate changes, TriggerTransmit
    if (p.seq > q.snd_una)
        q.snd_una = p.seq;
    if (q.snd_una >= q.size)
    {
        // QpComplete
        q.t_alpha = q.t_dec = q.t_rp = -1;
        q.done = 1;
        s.fct[f] = c.now - s.fi[f].start;
        atomicAdd(s.flows_done, 1u);
        if (s.fi[f].fast)
            atomicSub(&s.srv_fact[s.h_srv[c.h]], 1);
        if (s.dag)
            c.ord += dag_fire(s, f, 0, c.now, c.cur, c.ord); // (the ACK event's next events follow its callback's)
    }
    if (p.kind == K_NACK)
        q.snd_nxt = q.snd_una;
    if (p.cnp && c.P->cc == 1 && !q.done) // (after QpComplete its timers would touch nothing else)
        dcqcn_cnp(c, f, q);
    if (c.P->cc == 3)
    {
        if (p.seq > q.last_update_seq)
            hpcc_update(c, q, p, false);
        else if (c.P->fast_react)
            hpcc_update(c, q, p, true);
    }
    h_dequeue(c);
}

// RdmaHw::ReceiveUdp
template <int ND>
__device__ __forceinline__ void h_data(HCtx<ND> &c, const Pkt &p)
{
    const Sim &s = *c.s;
    RcvQP &r = s.rx[p.flow];
    uint32_t payload = p.size - data_hdr(*c.P);
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
        if (r.expected >= c.P->ack_interval)
            x = 1;
        else if (c.P->chunk && r.expected % c.P->chunk == 0)
            x = 1;
        else
            x = 5;
    }
    else if (p.seq > expected)
    {
        if (c.now >= r.nack_timer || r.last_nack != expected)
        {
            r.nack_timer = c.now + c.P->nack_interval;
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
    a.size = (uint16_t)ack_size(*c.P);
    hsel(c, s.fi[p.flow].rnic); // the receiving queue pair's NIC (GetNicIdxOfRxQp)
    if (c.st().ackq_n >= s.ackq_cap)
    {
        atomicAdd(&s.ev[6], 1ull);
        return;
    }
    s.ackq[(size_t)c.nic() * s.ackq_cap + ring_add(c.st().ackq_head, c.st().ackq_n, s.ackq_cap)] = a;
    c.st().ackq_n++;
    h_dequeue(c);
}

// RdmaClient::StartApplication -> RdmaHw::AddQueuePair -> QbbNetDevice::NewQp
template <int ND>
__device__ __forceinline__ void h_start(HCtx<ND> &c, int f)
{
    const Sim &s = *c.s;
    const FlowInfo &fi = s.fi[f];
    SndQP &q = s.qp[f];
    q = SndQP{};
    q.size = fi.size;
    if (fi.fast)
        atomicAdd(&s.srv_fact[s.h_srv[c.h]], 1); // (its server's GPUs run single slots until it completes)
    hsel(c, fi.snic); // GetNicIdxOfQp
    q.nic = fi.snic;
    const uint64_t nic = s.li[c.link()].bps;
    q.rate = q.max_rate = nic;
    q.win = fi.win;
    q.base_rtt = fi.base_rtt;
    q.alpha = 1;
    q.first_cnp = 1;
    q.target = nic;
    q.t_alpha = q.t_dec = q.t_rp = -1;
    q.cur_rate = nic;
    q.u = 1;
    q.next_avail = c.now;
    if (c.st().qp_n >= s.qps_cap)
    {
        atomicAdd(&s.ev[7], 1ull);
        return;
    }
    s.qps[(size_t)c.nic() * s.qps_cap + c.st().qp_n++] = f;
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

// a DAG flow's StartApplication: its source port (portNumber[src][dst]++, in the order of the
// installing events, which is the order of these starts) and devices, then AddQueuePair
template <int ND>
__device__ __forceinline__ void h_start_dyn(HCtx<ND> &c, int f)
{
    const Sim &s = *c.s;
    FlowInfo &x = s.fi[f];
    int w = s.h_world[c.h], hb = s.wh_base[w], hn = s.wh_n[w];
    int a = x.src - hb, b = x.dst - hb;
    int64_t pi = s.wp_off[w] + (int64_t)a * hn + b, pr = s.wp_off[w] + (int64_t)b * hn + a;
    uint16_t port = s.pcnt[pi]++;
    x.sport = port;
    x.start = c.now;
    const PairNic &sn = s.pnic[pi], &rn = s.pnic[pr];
    if (x.fast)
        atomicSub(&s.srv_fpend[s.h_srv[c.h]], 1);
    x.snic = sn.sn > 1 ? sn.s[qp_pick(node_ip(a), node_ip(b), port, 100, sn.sn)] : sn.s[0];
    x.rnic = rn.rn > 1 ? rn.r[qp_pick(node_ip(b), node_ip(a), 100, port, rn.rn)] : rn.r[0];
    h_start(c, f);
}

// A host's slot, run by the lanes of one wave: they load the slot's arrivals (of all its devices)
// into LDS and compute HPCC's per-hop utilizations of its ACKs (with FAST_REACT and without
// SAMPLE_FEEDBACK an ACK's depend only on it and on the hops of the QP's previous ACK, so they need
// not wait for the events; a flow's ACKs all arrive on one device); lane 0 then runs the events.
// hints: [0] the arrivals, [1 + 2 d] device d's first frame in s.msg, [2 + 2 d] its count
constexpr int HHINTS = 1 + 2 * MAXD;
// ND: the most devices a host of this kind has (1: one NIC, its state stays in registers)
template <int ND>
__device__ __forceinline__ bool host_step(const Sim &s, int h, int64_t slot, int64_t *wake, int lane, int lanes, HArr *pool, int *hints,
                                          struct Due *due)
{
    const int64_t D = s.P.delay, t0 = slot * D, t1 = t0 + D;
    const Params &WP = s.Pw[s.h_world[h]];
    const int nd = ND == 1 ? 1 : s.deg[h];
    (void)t0;
    if (lane == 0)
    {
        int total = 0;
        for (int d = 0; d < nd; d++)
        {
            uint32_t base;
            int n = bucket(s, s.in_link[(size_t)h * s.K + d], slot, &base);
            hints[1 + 2 * d] = (int)base;
            hints[2 + 2 * d] = n;
            total += n;
        }
        hints[0] = total;
        // nothing arrives, no DAG message, and its own earliest event (wake) lies beyond the slot:
        // the host has no event in it
        hints[HHINTS] = total == 0 && !(s.dag && s.hhead[(size_t)h * s.nbi + fmod(s.fnbi, slot)]) && s.hs[s.nic_off[h]].wake >= t1;
    }
    __syncthreads();
    if (hints[HHINTS])
    {
        *wake = s.hs[s.nic_off[h]].wake;
        return false;
    }
    const int total = hints[0];
    const bool pooled = total <= HPOOL;
    if (pooled)
        for (int j = lane; j < total; j += lanes)
        {
            int d = 0, k = j;
            while (k >= hints[2 + 2 * d])
                k -= hints[2 + 2 * d++];
            int link_in = s.in_link[(size_t)h * s.K + d];
            uint32_t fi = (uint32_t)hints[1 + 2 * d] + k;
            const LFrame &f = s.msg[fi];
            HArr &a = pool[j];
            a.ts = f.ts;
            a.p = f.p;
            a.acode = arrival_code(s, link_in, s.li[link_in].delay, f);
            a.frame = fi;
            a.pre = 0;
        }
    __syncthreads();
    // (with FAST_REACT every ACK of a QP calls UpdateRateHp, which stores its hops; without it,
    // only those that end an RTT do)
    if (pooled && WP.cc == 3 && WP.fast_react && !WP.sample_feedback)
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
            hpcc_hops(WP, a.p.hop, a.p.nhop, k >= 0 ? pool[k].p.hop : q.hop, q.max_rate, q.win, &a.U, &a.dt);
            a.pre = 1;
        }
    __syncthreads();
    if (lane != 0)
        return false;
    // the context: in LDS (lane 0's alone). For several devices its arrays are indexed by the
    // current device, which would put it in scratch; for one, the compiler kept it there too
    __shared__ HCtx<MAXD> hc_lds;
    HCtx<ND> &c = *reinterpret_cast<HCtx<ND> *>(&hc_lds);
    c.s = &s;
    c.P = &WP;
    c.h = h;
    c.slot = slot;
    c.nout = 0;
    c.pre_ok = false;
    c.nd = nd;
    c.nic0 = s.nic_off[h];
    // per device: its state, out link, arrivals (a range of the pool, or a cursor in its bucket)
    Arrivals in[ND];
    int ai[ND], aend[ND], lin[ND];
    int64_t indl[ND];
    for (int d = 0, b = 0; d < nd; d++)
    {
        c.stv[d] = s.hs[c.nic0 + d];
        c.lk[d] = s.out_link[(size_t)h * s.K + d];
        const LinkInfo li = s.li[c.lk[d]];
        c.ldl[d] = li.delay;
        link_open(c.obv[d], c.lk[d], li, c.stv[d].ob_last);
        lin[d] = s.in_link[(size_t)h * s.K + d];
        indl[d] = s.li[lin[d]].delay;
        if (pooled)
        {
            ai[d] = b;
            aend[d] = b += hints[2 + 2 * d];
        }
        else
            in[d].init(s, lin[d], slot);
    }
    hsel(c, 0);
    hq_ok = 0;
    // the DCQCN timers due in this slot (their intervals are at least a slot, so none is added)
    // (due: in LDS, not scratch; MAX_DUE of them)
    int ndue = 0;
    if (WP.cc == 1)
        for (int d = 0; d < nd; d++)
        {
            if (c.stv[d].due_min >= t1)
                continue; // (none due: no queue pair to read)
            const int *list = s.qps + (size_t)(c.nic0 + d) * s.qps_cap;
            int64_t mn = INT64_MAX;
            for (int i = 0; i < c.stv[d].qp_n; i++)
            {
                const SndQP &q = s.qp[list[i]];
                int64_t t[3] = {q.t_alpha, q.t_dec, q.t_rp};
                for (int k = 0; k < 3; k++)
                    mn = t[k] >= 0 && t[k] < mn ? t[k] : mn;
                for (int k = 0; k < 3; k++)
                    if (t[k] >= 0 && t[k] < t1)
                    {
                        if (ndue == MAX_DUE)
                        {
                            atomicAdd(&s.ev[12], 1ull);
                            continue;
                        }
                        Due x = {t[k], list[i], k};
                        int j = ndue++;
                        while (j > 0 && before(s, x.at, 0, due_chain(s, x), due[j - 1].at, 0, due_chain(s, due[j - 1])))
                        {
                            due[j] = due[j - 1];
                            j--;
                        }
                        due[j] = x;
                    }
            }
            c.stv[d].due_min = mn;
        }
    int di = 0;
    uint32_t acode[ND];
    int64_t ats[ND], sf_t = INT64_MAX;
    const LFrame *af[ND];
    for (int d = 0; d < nd; d++)
        af[d] = nullptr;
    int sf = -1, sf_at = -1;
    Host &h0 = c.stv[0]; // the host's flow starts
    // DAG: the dependencies fired for this host's flows that arrive in this slot; a flow whose last
    // one this is becomes ready, to start at the latest of them (in ns-3's order)
    int *dyn = s.dag ? s.dyn + (size_t)h * s.dyn_cap : nullptr;
    int ndyn = s.dag ? s.dyn_n[h] : 0;
    if (s.dag && s.hhead[(size_t)h * s.nbi + fmod(s.fnbi, slot)])
    {
        int b = (int)fmod(s.fnbi, slot);
        unsigned int nx = s.hhead[(size_t)h * s.nbi + b];
        s.hhead[(size_t)h * s.nbi + b] = 0;
        for (; nx; nx = s.dmsg[(size_t)b * s.dcap + nx - 1].next)
        {
            const DMsg &m = s.dmsg[(size_t)b * s.dcap + nx - 1];
            int f = m.flow;
            if (s.dep_t[f] < 0 || before(s, s.dep_t[f], 0, s.dep_c[f], m.ts, 0, m.c))
            {
                s.dep_t[f] = m.ts;
                s.dep_c[f] = m.c;
            }
            if (--s.dep_left[f] == 0)
            {
                if (ndyn >= s.dyn_cap)
                    atomicAdd(&s.ev[15], 1ull);
                else
                    dyn[ndyn++] = f;
            }
            else if (s.fi[f].fast)
                atomicSub(&s.srv_fpend[s.h_srv[h]], 1); // (its last message holds the server)
        }
    }
    for (;;)
    {
        // candidates: 0 an arrival, 1 a TransmitComplete, 2 an m_nextSend (each per device), 3 a
        // DCQCN timer, 4 a flow start (the arrivals' and the flow start's times are kept between
        // events)
        for (int d = 0; d < nd; d++)
            if (!af[d])
            {
                if (pooled)
                {
                    if (ai[d] < aend[d])
                    {
                        af[d] = &s.msg[pool[ai[d]].frame];
                        acode[d] = pool[ai[d]].acode;
                        ats[d] = pool[ai[d]].ts;
                    }
                }
                else
                {
                    af[d] = in[d].peek();
                    if (af[d])
                    {
                        acode[d] = arrival_code(s, lin[d], indl[d], *af[d]);
                        ats[d] = af[d]->ts;
                    }
                }
            }
        while (di < ndue && due_now(s, due[di]) != due[di].at)
            di++; // cancelled or moved
        if (sf_at != h0.next_start)
        {
            sf_at = h0.next_start;
            sf = sf_at < h0.start_end ? s.start_list[sf_at] : -1;
            sf_t = sf >= 0 ? s.fi[sf].start : INT64_MAX;
        }
        // a candidate's chain is cpush(code(k, d), chain(k, d)). The selection keeps only indices: a
        // version that carried a pointer to the best candidate's chain along gave wrong results at
        // -O2 and above (the pointer was stale at a flow start), and right ones at -O1
        // the earliest ready DAG flow
        int di_dyn = -1;
        for (int i = 0; i < ndyn; i++)
            if (di_dyn < 0 || before(s, s.dep_t[dyn[i]], 0, s.dep_c[dyn[i]], s.dep_t[dyn[di_dyn]], 0, s.dep_c[dyn[di_dyn]]))
                di_dyn = i;
        auto code = [&](int k, int d) -> uint32_t { return k == 0 ? acode[d] : 0; };
        auto chain = [&](int k, int d) -> const Chain & {
            return k == 0   ? af[d]->c
                   : k == 1 ? s.h_tc[c.nic0 + d]
                   : k == 2 ? s.h_ns[c.nic0 + d]
                   : k == 3 ? due_chain(s, due[di])
                   : k == 4 ? s.fi[sf].start_chain
                            : s.dep_c[dyn[di_dyn]];
        };
        auto ctime = [&](int k, int d) -> int64_t {
            if (k == 0)
                return af[d] ? ats[d] : INT64_MAX;
            if (k == 1)
                return c.stv[d].tc_at >= 0 && c.stv[d].tc_at < t1 ? c.stv[d].tc_at : INT64_MAX;
            if (k == 2)
                return c.stv[d].ns_at >= 0 && c.stv[d].ns_at < t1 ? c.stv[d].ns_at : INT64_MAX;
            if (k == 3)
                return di < ndue ? due[di].at : INT64_MAX;
            if (k == 4)
                return sf_t < t1 ? sf_t : INT64_MAX;
            return di_dyn >= 0 && s.dep_t[dyn[di_dyn]] < t1 ? s.dep_t[dyn[di_dyn]] : INT64_MAX;
        };
        auto quick = [&](int k, int d) -> const QPre * {
            if (k > 2)
                return nullptr;
            unsigned bit = 1u << (k == 1 ? d : k == 2 ? MAXD + d : 2 * MAXD + d);
            QPre &q = k == 1 ? hq_tc[d] : k == 2 ? hq_ns[d] : hq_ar[d];
            if (!(hq_ok & bit))
            {
                qpre_of(q, code(k, d), chain(k, d));
                hq_ok |= bit;
            }
            return &q;
        };
        int bk = -1, bd = 0;
        int64_t bt = INT64_MAX;
        for (int k = 0; k < 6; k++)
            for (int d = 0; d < (k < 3 ? nd : 1); d++)
            {
                int64_t t = ctime(k, d);
                if (t == INT64_MAX)
                    continue;
                bool b4 = bk < 0 || t < bt;
                if (bk >= 0 && t == bt)
                {
                    const QPre *qa = quick(k, d), *qb = quick(bk, bd);
                    int r = qa && qb ? qcmp(s, qa->r, qa->h, qa->root, qb->r, qb->h, qb->root) : 0;
                    b4 = r ? r < 0 : before(s, t, code(k, d), chain(k, d), bt, code(bk, bd), chain(bk, bd));
                }
                if (b4)
                {
                    bk = k;
                    bd = d;
                    bt = t;
                }
            }
        if (bk < 0)
            break;
        if (bt < t0)
            atomicAdd(&s.ev[32], 1ull); // an event of a slot not run (a bug: reported)
        c.now = bt;
        if (code(bk, bd))
            cpush(&c.cur, code(bk, bd), chain(bk, bd));
        else
            c.cur = chain(bk, bd);
        c.ord = 0;
        trace_event(h, c.now, bk == 0 ? "arrival" : bk == 1 ? "tc" : bk == 2 ? "nextsend" : bk == 3 ? "timer" : "start", bd, c.cur);
        if (bk == 5)
        {
            int f = dyn[di_dyn];
            dyn[di_dyn] = dyn[--ndyn];
            h_start_dyn(c, f);
            continue;
        }
        if (bk < 3)
            hsel(c, bd);
        if (bk == 0)
        {
            const Pkt &p = af[bd]->p; // (the frame is not written while it is read)
            if (pooled)
            {
                c.pre_ok = pool[ai[bd]].pre;
                c.pre_U = pool[ai[bd]].U;
                c.pre_dt = pool[ai[bd]].dt;
                ai[bd]++;
            }
            else
                in[bd].pop();
            af[bd] = nullptr;
            hq_ok &= ~(1u << (2 * MAXD + bd));
            if (p.kind == K_DATA)
                h_data(c, p);
            else if (p.kind == K_ACK || p.kind == K_NACK)
            {
                h_ack(c, p);
                c.pre_ok = false;
            }
            else if (WP.qbb)
            {
                if (p.kind == K_PAUSE)
                    c.st().paused |= 1u << p.q;
                else
                {
                    c.st().paused &= ~(1u << p.q);
                    h_dequeue(c);
                }
            }
        }
        else if (bk == 1)
        {
            c.st().tc_at = -1;
            h_dequeue(c);
        }
        else if (bk == 2)
        {
            c.st().ns_at = -1;
            h_dequeue(c);
        }
        else if (bk == 3)
        {
            Due x = due[di++];
            dcqcn_timer(c, x.flow, x.kind);
        }
        else
        {
            h0.next_start++;
            h_start(c, sf);
        }
    }
    bool act = c.nout > 0;
    // the earliest event without input
    int64_t w = INT64_MAX;
    if (h0.next_start < h0.start_end)
        w = s.fi[s.start_list[h0.next_start]].start;
    if (s.dag)
    {
        s.dyn_n[h] = ndyn;
        for (int i = 0; i < ndyn; i++)
            w = s.dep_t[dyn[i]] < w ? s.dep_t[dyn[i]] : w;
    }
    for (int d = 0; d < nd; d++)
    {
        link_flush(s, c.obv[d]);
        c.stv[d].ob_last = c.obv[d].last;
        const Host &st = c.stv[d];
        if (st.ackq_n > 0 || st.tc_at >= 0)
            act = true;
        if (st.ns_at >= 0 && st.ns_at < w)
            w = st.ns_at;
        if (WP.cc == 1)
        {
            // the bound, exact again if a timer this slot ran left it behind
            if (c.stv[d].due_min < t1)
            {
                const int *list = s.qps + (size_t)(c.nic0 + d) * s.qps_cap;
                int64_t mn = INT64_MAX;
                for (int i = 0; i < st.qp_n; i++)
                {
                    const SndQP &q = s.qp[list[i]];
                    int64_t t[3] = {q.t_alpha, q.t_dec, q.t_rp};
                    for (int k = 0; k < 3; k++)
                        mn = t[k] >= 0 && t[k] < mn ? t[k] : mn;
                }
                c.stv[d].due_min = mn;
            }
            if (st.due_min < w)
                w = st.due_min;
        }
        if (st.tc_at >= 0 && st.tc_at < w)
            w = st.tc_at;
        s.hs[c.nic0 + d] = st;
    }
    s.hs[c.nic0].wake = w; // (with its TransmitCompletes: when it must run again without input)
    *wake = w;
    return act;
}

// ------------------------------------------------------------------ switch
constexpr int KMAX = 192;  // ports of a switch (LDS: see switch_block)
constexpr int KSMALL = 32; // switches with more ports (wide: no safe slots) run in rdma_slot_big

// A switch's slot workspace in LDS, per port: its Port, its arrivals of this slot (a range of the
// pool, or, when the pool overflows, a cursor in the frames of slots s - 2 then s - 1), and the
// prefixes of its next arrival's chain and of its pending TransmitComplete's (whose chain is
// tc[port], in global memory). It is kept small: LDS bounds how many switches a CU holds.
struct SwPortLds
{
    Port pt;
    int64_t ats;       // the next arrival's time (INT64_MAX: none)
    int ai, aend;      // pool: the next arrival and the end of the port's range
    int an, link_in, nout; // the fallback cursor (ai) runs over the an frames from abase; link; frames sent
    uint32_t abase;
    int32_t in_delay, out_delay; // link_in's delay, its out link's
    uint32_t kmin, kmax;   // its PortCfg's
    OutBucket ob;          // its out link's
    uint32_t acode;    // the next arrival's code
    Run ap[PRE], tp[PRE]; // the prefixes of the next arrival's chain and of tc's
    uint64_t ah, th;   // their lead hashes and roots (qcmp)
    uint32_t ar, tr;
};

// an arrival of the slot in the pool: its frame's contents, the code and prefix of its arrival's
// chain, where its frame lies (for the chain itself), and its route
struct ArrLds
{
    int64_t ts;
    Pkt p;
    Run ap[PRE];
    uint64_t ah;    // its chain's lead hash and root (qcmp)
    uint32_t ar;
    int32_t rank;   // wide switches: its place among the slot's arrivals in event order
    uint32_t acode;
    uint32_t frame; // index in s.msg
    int16_t out;    // the egress port (-1: a PFC frame, or no route)
    uint8_t q, in;  // its queue; the ingress port
};
constexpr int POOL = 1024; // arrivals per switch and slot in the pool (global memory)

struct SCtx
{
    const Sim *s;
    const Params *P; // its world's
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
    bool nvs; // an NVSwitch
    // wide switches: the ports whose candidates the running event changed (its transmissions), in
    // LDS (an array here would keep SCtx out of registers); null for the others
    int ndirty;
    int *dirty;
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
__device__ __forceinline__ const PortCfg &pcfg(const SCtx &c, int port) { return c.s->pc[c.s->pbase[c.x] + port]; }
__device__ __forceinline__ uint32_t pfc_thresh(const SCtx &c, int port)
{
    const Params &P = *c.P;
    return (P.buffer_size - c.sw.total_hdrm - c.sw.total_rsrv - c.sw.shared_used) >> pcfg(c, port).shift;
}
__device__ __forceinline__ uint32_t shared_used(const Port &p) { return p.ing > RESERVE ? p.ing - RESERVE : 0; }

__device__ __forceinline__ void s_transmit(SCtx &c, int port, Pkt p)
{
    const Sim &s = *c.s;
    int64_t tx = link_txo(s, c.w[port].ob, p.size);
    c.w[port].pt.tc_at = c.now + tx;
    c.w[port].pt.tc_key = c.sw.evseq * 64 + (c.ord < 63 ? c.ord : 63);
    if (c.dirty)
    {
        if (c.ndirty < 6)
            c.dirty[c.ndirty] = port;
        c.ndirty++;
    }
    const Chain &cur = sc_cur(c);
    Chain tcc;
    cpush(&tcc, ccode(tx, c.ord++), cur);
    c.tc[port] = tcc;
    cprefix(c.w[port].tp, 0, tcc);
    c.w[port].th = tcc.hash;
    c.w[port].tr = tcc.root;
    p.ord = (uint8_t)(c.ord < 255 ? c.ord : 255);
    c.ord++;
    c.w[port].nout++;
    int64_t ts = c.now + tx + c.w[port].out_delay;
    LFrame *f = link_push(s, ts, c.w[port].ob);
    if (!f)
        return;
    f->ts = ts;
    f->c = cur;
    f->p = p;
    trace_tx(s, c.now, c.node, port, p);
}

__device__ __forceinline__ void s_enqueue(SCtx &c, int port, int q, const Pkt &p)
{
    const Sim &s = *c.s;
    Port &pt = c.w[port].pt;
    size_t pi = (size_t)s.pbase[c.x] + port;
    if (q == 0)
    {
        if (pt.q0_n >= s.q0_cap)
        {
            atomicAdd(&s.ev[5], 1ull);
            return;
        }
        s.q0[pi * s.q0_cap + ring_add(pt.q0_head, pt.q0_n, s.q0_cap)] = p;
        pt.q0_n++;
    }
    else
    {
        if (pt.q3_n >= s.q3_cap)
        {
            atomicAdd(&s.ev[5], 1ull);
            return;
        }
        s.q3[pi * s.q3_cap + ring_add(pt.q3_head, pt.q3_n, s.q3_cap)] = p;
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
    size_t pi = (size_t)s.pbase[c.x] + port;
    Pkt p = s.q0[pi * s.q0_cap + pt.q0_head];
    pt.q0_head = ring_add(pt.q0_head, 1, s.q0_cap);
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
    const Params &P = *c.P;
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
    Pkt p = s.q3[((size_t)s.pbase[c.x] + port) * s.q3_cap + pt.q3_head];
    pt.q3_head = ring_add(pt.q3_head, 1, s.q3_cap);
    pt.q3_n--;
    pt.bytes_total -= p.size;
    s_start_q3(c, port, p);
}

// the rest of DequeueAndTransmit for a frame of queue PG, out of the queue (SwitchNotifyDequeue,
// TransmitStart)
__device__ __forceinline__ void s_start_q3(SCtx &c, int port, Pkt p)
{
    const Sim &s = *c.s;
    const Params &P = *c.P;
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
                   port + 1, p.size, in.ing, in.hdrm, c.sw.shared_used, pfc_thresh(c, p.inport), p.src, p.dst, p.seq, pt.q3_n);
#endif
    }
    if (P.ecn && !c.nvs)
    {
        // SwitchMmu::ShouldSendCN
        bool mark = false;
        const uint32_t kmin = c.w[port].kmin, kmax = c.w[port].kmax;
        if (pt.egr > kmax)
            mark = true;
        else if (pt.egr > kmin)
        {
            double pr = pcfg(c, port).pmax * (double)(pt.egr - kmin) / (double)(kmax - kmin);
            mark = nsm_rng::rand_u01(pt.rng) < pr;
        }
        if (mark)
        {
            p.ecn = 3;
            atomicAdd(&s.ev[1], 1ull);
        }
    }
    // CheckAndSendResume (never in a safe slot: no port is paused)
    if (!c.safe && in.mmu_paused && !c.nvs)
    {
        uint32_t su = shared_used(in);
        if (in.hdrm == 0 && (su == 0 || su + RESUME_OFFSET <= pfc_thresh(c, p.inport)))
        {
            s_send_pfc(c, p.inport, 1);
            in.mmu_paused = 0;
        }
    }
    if (p.kind == K_DATA && P.cc == 3 && p.nhop < 0xFFFF && !c.nvs)
    {
        // (constant indices: an index into p's array would put the frame in scratch)
        uint64_t hv = hop_make((uint64_t)c.now, pt.tx_bytes, pt.bytes_total, pcfg(c, port).bps, P.int_multi);
        const int hi = p.nhop % MAX_HOP;
#pragma unroll
        for (int i = 0; i < MAX_HOP; i++)
            if (i == hi)
                p.hop[i] = hv;
        p.nhop++;
    }
    pt.tx_bytes += p.size;
    s_transmit(c, port, p);
}

// SwitchNode::SendToDev
// the egress port of a data frame or ACK (-1: no route) and its queue
// (hosts are numbered within their world for the hash and the table, as in the reference: host
// h of the world is node h, and its switch x node hn + x)
__device__ __forceinline__ int s_route(const Sim &s, const Params &P, int x, const Pkt &p, int *q)
{
    const SwInfo &si = s.si[x];
    bool data = p.kind == K_DATA;
    int src = (data ? p.src : p.dst) - si.hbase, dst = (data ? p.dst : p.src) - si.hbase;
    const Route &rt = s.rt[si.rt_off + dst];
    *q = (P.ack_high_prio && !data) ? 0 : PG;
    if (rt.n == 0)
        return -1;
    uint32_t w2 = data ? (p.sport | 100u << 16) : (100u | (uint32_t)p.sport << 16);
    uint32_t hsh = ecmp_hash(node_ip(src), node_ip(dst), w2, si.seed);
    return s.rtp[rt.off + hsh % rt.n];
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
        uint32_t th = pfc_thresh(c, inport);
        // CheckIngressAdmission
        if (p.size + in.hdrm > pcfg(c, inport).hdrm_cap && p.size + shared_used(in) > th)
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
                   out + 1, p.size, in.ing, in.hdrm, c.sw.shared_used, pfc_thresh(c, inport), p.src, p.dst, p.seq, c.w[out].pt.q3_n);
#endif
        // CheckAndSendPfc
        if (!c.nvs && !in.mmu_paused && (in.hdrm > 0 || shared_used(in) >= pfc_thresh(c, inport)))
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

__device__ __forceinline__ const LFrame &sw_frame(const Sim &s, const SwPortLds &w) { return s.msg[w.abase + w.ai]; }

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
            w.ah = a.ah;
            w.ar = a.ar;
        }
        return;
    }
    (void)t1;
    if (w.ai < w.an)
    {
        const LFrame &f = sw_frame(s, w);
        w.ats = f.ts;
        w.acode = arrival_code(s, w.link_in, w.in_delay, f);
        cprefix(w.ap, w.acode, f.c);
        w.ah = chash(f.c.hash, w.acode);
        w.ar = f.c.root;
    }
}

// the chain of a port's next arrival (without its own code)
__device__ __forceinline__ const Chain &sw_arr_chain(const SCtx &c, const SwPortLds &w)
{
    return c.pool ? c.s->msg[c.pool[w.ai].frame].c : sw_frame(*c.s, w).c;
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
// the LDS int workspace of a switch with K ports (the merge cursors only for safe slots, which
// have at most SAFE_LANES ports), then LK arrival keys: their times in the slot, egress ports, and
// two index lists (uint16, uint8, 2 x uint16)
constexpr int LK = 512;
__host__ __device__ inline int sw_ks(int K) { return K < SAFE_LANES ? K : SAFE_LANES; }
// (wide switches, more than SAFE_LANES ports: then a tournament tree of 2 KP ints, KP the power of
// two from the ports)
__host__ __device__ inline int sw_kp(int K)
{
    int k = 1;
    while (k < K)
        k <<= 1;
    return k;
}
__host__ __device__ inline size_t sw_ints(int K)
{
    return 6 * (size_t)K + (size_t)sw_ks(K) * sw_ks(K) + (LK * 7 + 3) / 4 + (K > SAFE_LANES ? 2 * (size_t)sw_kp(K) : 0);
}
// WIDE: the switches of rdma_slot_big (more than SAFE_LANES ports); the narrow ones' code (in
// rdma_slot) leaves out their path, whose registers would otherwise spill the safe slots'
template <bool WIDE>
__device__ __forceinline__ bool switch_step(const Sim &s, int x, int64_t slot, SwPortLds *w, ArrLds *pool, int *pidx, int lane, int lanes,
                                            int *shared_int, int K)
{
    Chain *tcs = s.p_tc + s.pbase[x];
    const int64_t D = s.P.delay, t0 = slot * D, t1 = t0 + D;
    const int node = s.H + x, deg = s.deg[node];
    const Params &WP = s.Pw[s.s_world[x]];
    Port *gp = s.ps + s.pbase[x];
    // ports, and the bounds of their arrivals: at most the last frame of slot s - 2 (a frame
    // arrives within one transmission of the link's delay), and the frames of slot s - 1 but
    // perhaps the last
    // (K: the ports its LDS workspace has room for)
    const int KS = sw_ks(K);
    int *cnt = shared_int, *first = shared_int + K; // per port: arrivals, the first in slot s - 2's list
    uint32_t *abytes = (uint32_t *)(shared_int + 2 * K); // bytes arriving for queue PG
    uint32_t *ing0 = (uint32_t *)(shared_int + 3 * K);   // ing at the slot's start
    int *ocnt = shared_int + 4 * K, *obase = shared_int + 5 * K; // safe: arrivals per egress port
    int *mcur = shared_int + 6 * K;                       // safe: merge cursors [egress][ingress]
    // safe: per arrival its time in the slot and its egress port; pool indices by egress port, in
    // order; the next arrival of its ingress with the same egress
    uint16_t *kts = (uint16_t *)(mcur + KS * KS), *olist = kts + LK, *onext = olist + LK;
    int8_t *own = (int8_t *)(onext + LK);
    (void)pidx;
    for (int p = lane; p < deg; p += lanes)
    {
        SwPortLds &q = w[p];
        q.pt = gp[p];
        abytes[p] = 0;
        ocnt[p] = 0;
        ing0[p] = q.pt.ing;
        const PortStatic ps = s.pst[s.pbase[x] + p];
        q.link_in = ps.link_in;
        link_open(q.ob, ps.link_out, s.li[ps.link_out], q.pt.ob_last);
        {
            // bucket(), from the port's copy of its link's ring
            int b = (int)fmod(ps.in_fnb, slot);
            long long v = s.msg_cnt[ps.in_coff + b];
            q.abase = ps.in_moff + (uint32_t)b * ps.in_cap;
            q.an = (v >> 16) == slot ? (int)(v & 0xFFFF) : 0;
        }
        q.in_delay = ps.in_delay;
        q.out_delay = ps.out_delay;
        q.kmin = ps.kmin;
        q.kmax = ps.kmax;
        q.nout = 0;
        first[p] = 0;
        cnt[p] = q.an;
        if (q.pt.tc_at >= 0)
        {
            cprefix(q.tp, 0, tcs[p]);
            q.th = tcs[p].hash;
            q.tr = tcs[p].root;
        }
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
            int k = j - base;
            uint32_t fi = w[p].abase + k;
            const LFrame &f = s.msg[fi];
            ArrLds &a = pool[j];
            a.ts = f.ts;
            a.p = f.p;
            a.acode = arrival_code(s, w[p].link_in, w[p].in_delay, f);
            cprefix(a.ap, a.acode, f.c);
            a.ah = chash(f.c.hash, a.acode);
            a.ar = f.c.root;
            a.frame = fi;
            a.rank = -1;
            if (WIDE && s.rank_ahead)
            {
                int64_t rk = s.frank[fi];
                if ((rk >> 20) == slot)
                    a.rank = (int32_t)(rk & 0xFFFFF);
            }
            int q = 0;
            a.out = (int16_t)(f.p.kind == K_PAUSE || f.p.kind == K_RESUME ? -1 : s_route(s, WP, x, f.p, &q));
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
    bool safe = !WIDE && pooled && total <= LK && D <= 65535 && deg <= lanes && deg <= SAFE_LANES && !s.serial;
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
        int64_t room = (int64_t)WP.buffer_size - sw.total_hdrm - sw.total_rsrv - amax;
        int64_t th_min = room > 0 ? room >> sw.max_shift : -1;
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
                mcur[o * KS + p] = -1;
            for (int k = b + cnt[p] - 1; k >= b; k--)
            {
                int o = own[k];
                if (o < 0)
                    continue;
                onext[k] = (uint16_t)mcur[o * KS + p]; // (-1, the end: 0xFFFF)
                mcur[o * KS + p] = k;
            }
        }
        __syncthreads();
        if (lane < deg)
        {
            int o = lane, base = 0;
            for (int p = 0; p < o; p++)
                base += ocnt[p];
            obase[o] = base;
            int *cur = mcur + o * KS;
            // the ingress ports with arrivals left for o: their cursors packed at the front of
            // cur, in port order
            int na = 0;
            for (int p = 0; p < deg; p++)
                if (cur[p] >= 0)
                    cur[na++] = cur[p];
            for (int n = 0; n < ocnt[o]; n++)
            {
                int bp = -1;
                for (int p = 0; p < na; p++)
                {
                    int k = cur[p];
                    if (bp < 0)
                    {
                        bp = p;
                        continue;
                    }
                    int kb = cur[bp];
                    if (kts[k] != kts[kb] ? kts[k] < kts[kb] : ({
                            const ArrLds &A = pool[k], &B = pool[kb];
                            int r = qcmp(s, A.ap, A.ah, A.ar, B.ap, B.ah, B.ar);
                            r ? r < 0 : before(s, A.ts, A.acode, s.msg[A.frame].c, B.ts, B.acode, s.msg[B.frame].c);
                        }))
                        bp = p;
                }
                olist[base + n] = cur[bp];
                int nx = onext[cur[bp]];
                if (nx != 0xFFFF)
                    cur[bp] = nx;
                else
                {
                    for (int q = bp + 1; q < na; q++)
                        cur[q - 1] = cur[q];
                    na--;
                }
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
            c.nvs = s.si[x].nvs;
            c.ndirty = 0;
            c.dirty = nullptr;
            c.P = &WP;
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
                    int r = qcmp(s, a.ap, a.ah, a.ar, me.tp, me.th, me.tr);
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
                    if (WP.qbb)
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
    else if (!WIDE)
    {
        // all events in order, by lane 0 (a few ports: a scan is cheaper than the lanes' barriers)
        if (lane == 0)
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
            c.nvs = s.si[x].nvs;
            c.ndirty = 0;
            c.dirty = nullptr;
            c.P = &WP;
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
                    w[p].ai = 0;
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
                        int r = qcmp(s, pa, k ? w[p].th : w[p].ah, k ? w[p].tr : w[p].ar, pb, bk ? w[best].th : w[best].ah,
                                     bk ? w[best].tr : w[best].ar);
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
                    const LFrame &f = sw_frame(s, w[p]);
                    c.cur_src = &f.c;
                    pk = f.p;
                    out = pk.kind == K_PAUSE || pk.kind == K_RESUME ? -1 : s_route(s, WP, x, pk, &q);
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
                    if (WP.qbb)
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
    }
    else if constexpr (WIDE)
    {
        // all events in order, many ports: a tournament tree over the ports' candidates (built by the
        // lanes; lane 0 replays the paths of the ports each event changes) chooses the next one
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
        c.nvs = s.si[x].nvs;
        __shared__ int dirty_lds[6];
        c.ndirty = 0;
        c.dirty = dirty_lds;
        c.P = &WP;
        for (int p = lane; p < deg; p += lanes)
        {
            if (pooled)
            {
                int base = 0;
                for (int q = 0; q < p; q++)
                    base += cnt[q];
                w[p].ai = base;
                w[p].aend = base + cnt[p];
            }
            else
                w[p].ai = 0;
            sw_peek(c, w[p], t1);
        }
        __syncthreads();
        // a candidate: index 2 port + kind (0 arrival, 1 TransmitComplete), -1 none; time
        auto ctime = [&](int i) -> int64_t {
            const SwPortLds &q = w[i >> 1];
            if (i & 1)
                return q.pt.tc_at >= 0 && q.pt.tc_at < t1 ? q.pt.tc_at : INT64_MAX;
            return q.ats;
        };
        // does candidate i run before j (both at time t)? Undecided ties keep the smaller index, as
        // a scan in port order would
        auto first = [&](int i, int j, int64_t t) -> bool {
            const SwPortLds &a = w[i >> 1], &b = w[j >> 1];
            int ki = i & 1, kj = j & 1;
            if (ki && kj) // two TransmitCompletes: their keys
                return a.pt.tc_key < b.pt.tc_key;
            if (!ki && !kj && c.pool && pool[a.ai].rank >= 0 && pool[b.ai].rank >= 0) // two arrivals: their ranks
                return pool[a.ai].rank < pool[b.ai].rank;
            int r = qcmp(s, ki ? a.tp : a.ap, ki ? a.th : a.ah, ki ? a.tr : a.ar, kj ? b.tp : b.ap, kj ? b.th : b.ah, kj ? b.tr : b.ar);
#ifdef RDMA_PROF
            atomicAdd(&s.ev[28], 1ull);
            if (r == 0)
                atomicAdd(&s.ev[29], 1ull);
#endif
            if (r == 0)
            {
                if (!ki && !kj)
                    atomicAdd(&s.ev[34], 1ull); // (see rdma_rank)
                const Chain &xa = ki ? tcs[i >> 1] : sw_arr_chain(c, a);
                const Chain &xb = kj ? tcs[j >> 1] : sw_arr_chain(c, b);
                bool inexact;
                r = ccmp(s, ki ? 0 : a.acode, xa, kj ? 0 : b.acode, xb, &inexact);
                if (inexact)
                    atomicAdd(&s.ev[9], 1ull);
                if (r == 0)
                {
                    atomicAdd(&s.ev[8], 1ull);
                    return i < j;
                }
            }
            return r < 0;
        };
        // the slot's arrivals in event order, once (a bitonic sort of their pool indices in pidx):
        // simultaneous arrivals, as in lockstep collectives, would otherwise be compared again at
        // every event
        if (RANK_SORT && pooled && total > 1)
        {
            int n2 = 1;
            while (n2 < total)
                n2 <<= 1;
            for (int i = lane; i < n2; i += lanes)
                pidx[i] = i < total ? i : -1;
            __syncthreads();
            // does pool arrival u run before v (-1: past the end)? Undecided: the smaller index
            auto arr_first = [&](int u, int v) -> bool {
                if (u < 0 || v < 0)
                    return v < 0 && u >= 0;
                const ArrLds &A = pool[u], &B = pool[v];
                if (A.ts != B.ts)
                    return A.ts < B.ts;
                int r = qcmp(s, A.ap, A.ah, A.ar, B.ap, B.ah, B.ar);
                if (r == 0)
                {
                    bool inexact;
                    r = ccmp(s, A.acode, s.msg[A.frame].c, B.acode, s.msg[B.frame].c, &inexact);
                    if (inexact)
                        atomicAdd(&s.ev[9], 1ull);
                    if (r == 0)
                    {
                        atomicAdd(&s.ev[8], 1ull);
                        return u < v;
                    }
                }
                return r < 0;
            };
            for (int k = 2; k <= n2; k <<= 1)
                for (int j = k >> 1; j > 0; j >>= 1)
                {
                    for (int i = lane; i < n2; i += lanes)
                    {
                        int l = i ^ j;
                        if (l > i)
                        {
                            int u = pidx[i], v = pidx[l];
                            bool up = (i & k) == 0;
                            if (up ? arr_first(v, u) : arr_first(u, v))
                            {
                                pidx[i] = v;
                                pidx[l] = u;
                            }
                        }
                    }
                    __syncthreads();
                }
            for (int i = lane; i < total; i += lanes)
                pool[pidx[i]].rank = i;
            __syncthreads();
        }
        // the tree: leaf KP + p holds port p's earlier candidate, node n the better of 2n and 2n + 1
        const int KP = sw_kp(deg);
        int *tree = shared_int + 6 * K + KS * KS + (LK * 7 + 3) / 4;
        auto better = [&](int i, int j) -> int {
            if (i < 0)
                return j;
            if (j < 0)
                return i;
            int64_t ti = ctime(i), tj = ctime(j);
            if (ti != tj)
                return ti < tj ? i : j;
            return first(i, j, ti) ? i : j;
        };
        auto leaf = [&](int p) -> int {
            if (p >= deg)
                return -1;
            int a = ctime(2 * p) != INT64_MAX ? 2 * p : -1, b = ctime(2 * p + 1) != INT64_MAX ? 2 * p + 1 : -1;
            return better(a, b);
        };
        for (int p = lane; p < KP; p += lanes)
            tree[KP + p] = leaf(p);
        __syncthreads();
        for (int n = KP >> 1; n >= 1; n >>= 1)
        {
            for (int i = lane; i < n; i += lanes)
                tree[n + i] = better(tree[2 * (n + i)], tree[2 * (n + i) + 1]);
            __syncthreads();
        }
        if (lane == 0)
        {
            auto fix = [&](int p) {
                int n = KP + p;
                tree[n] = leaf(p);
                for (n >>= 1; n >= 1; n >>= 1)
                    tree[n] = better(tree[2 * n], tree[2 * n + 1]);
            };
#ifdef RDMA_PROF
            unsigned long long nev = 0;
#endif
            for (;;)
            {
                int best = tree[1];
                if (best < 0)
                    break;
                int64_t bt = ctime(best);
                int p = best >> 1;
                c.now = bt;
                c.ord = 0;
                c.cur_ok = false;
                c.ndirty = 0;
                c.sw.evseq++;
#ifdef RDMA_PROF
                nev++;
#endif
                if (best & 1)
                {
                    c.cur_src = &tcs[p];
                    c.cur_code = 0;
                    trace_event(c.node, c.now, "tc", p, sc_cur(c));
                    w[p].pt.tc_at = -1;
                    s_dequeue(c, p);
                }
                else
                {
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
                        const LFrame &f = sw_frame(s, w[p]);
                        c.cur_src = &f.c;
                        pk = f.p;
                        out = pk.kind == K_PAUSE || pk.kind == K_RESUME ? -1 : s_route(s, WP, x, pk, &q);
                    }
                    trace_event(c.node, c.now, "arrival", p, sc_cur(c));
                    w[p].ai++;
                    sw_peek(c, w[p], t1);
                    if (pk.kind == K_PAUSE || pk.kind == K_RESUME)
                    {
                        if (WP.qbb)
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
                }
                // the changed candidates: this port's, and those of the ports it transmitted on
                fix(p);
                if (c.ndirty > 6)
                    for (int r = 0; r < deg; r++)
                        fix(r);
                else
                    for (int i = 0; i < c.ndirty; i++)
                        if (c.dirty[i] != p)
                            fix(c.dirty[i]);
            }
            s.sw[x] = c.sw;
#ifdef RDMA_PROF
            atomicAdd(&s.ev[20], nev);
            atomicMax(&s.ev[22], nev);
#endif
        }
    }
    __syncthreads();
    for (int p = lane; p < deg; p += lanes)
    {
        link_flush(s, w[p].ob);
        w[p].pt.ob_last = w[p].ob.last;
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
    // idle skip: nothing sent and nothing queued in this slot: go on at the slot of the earliest
    // host event or arrival (this slot's arrivals are read: its pending flag is cleared)
    unsigned int cur = atomicAdd(&s.activity[0], 0u);
    unsigned long long nw = atomicAdd(s.next_wake, 0ull);
    s.pend[fmod(s.fpend, slot)] = 0;
    if (s.dag)
        s.dmsg_n[fmod(s.fnbi, slot)] = 0; // this slot's dependency messages are read
    int64_t next = slot + 1;
    if (cur == 0)
    {
        int64_t ws = nw != ~0ull ? (int64_t)fdiv(s.fD, nw) : INT64_MAX;
        for (int i = 1; i < s.pend_n; i++)
            if (s.pend[fmod(s.fpend, slot + i)])
            {
                ws = slot + i < ws ? slot + i : ws;
                break;
            }
        if (ws != INT64_MAX && ws > next)
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
    *s.done_blocks = 0;
    __threadfence(); // (the tickets are reset before the next slot shows: rdma_persist)
    *s.slot = next;
    __threadfence();
}

// A block (one wave) per host and per switch. A block's lanes load its slot's inputs together,
// then the first lane runs its events (entities sharing a wave would diverge and run in turn).
// Their LDS is kept small: it bounds how many blocks a CU holds.
// An entity's window at the launch's slot (Sim::hz): 0 when it has run it already, else the slots
// it runs. A quiet server's GPU (no NVLink queue pair running, none fired and waiting to start)
// runs up to the slot of the server's next NVLink flow of fixed start: its frames reach the
// server's GPUs a slot later at the earliest. (A DAG flow fired at t starts send_lat later, beyond
// any window running at t.)
__device__ __forceinline__ int entity_window(const Sim &s, int e, int64_t slot)
{
    if (!s.any_win)
        return 1;
    if (s.hz[e] > slot)
        return 0;
    int k = s.wk[e];
    if (e < s.H && s.wq[e] > k && s.srv_fact[s.h_srv[e]] == 0 && (!s.dag || s.srv_fpend[s.h_srv[e]] == 0))
    {
        int srv = s.h_srv[e], q = s.wq[e];
        int lo = s.srv_rs_off[srv], hi = s.srv_rs_off[srv + 1], end = hi;
        while (lo < hi)
        {
            int m = (lo + hi) / 2;
            if (s.srv_rs[m] < slot)
                lo = m + 1;
            else
                hi = m;
        }
        if (lo < end && s.srv_rs[lo] < slot + q)
            q = (int)(s.srv_rs[lo] - slot);
        k = q > k ? q : k;
    }
    if (slot + k > s.max_slots)
        k = s.max_slots - slot > 1 ? (int)(s.max_slots - slot) : 1;
    return k;
}

template <int ND>
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
    Due *due = reinterpret_cast<Due *>(smem + HPOOL * sizeof(HArr) + (HHINTS + 1) * sizeof(int) + 8);
    __shared__ int win;
    if (s.any_win)
    {
        if (lane == 0)
            win = entity_window(s, h, slot);
        __syncthreads();
    }
    const int k = s.any_win ? win : 1;
    if (k == 0)
    {
        // ahead: its earliest event without input is known (arrivals mark their slots), so the
        // launches may skip past its window
        if (lane == 0)
        {
            int64_t wk = s.hs[s.nic_off[h]].wake;
            slot_end(s, slot, 0, wk != INT64_MAX ? (unsigned long long)wk : ~0ull);
        }
        return;
    }
    bool act = false;
    for (int j = 0; j < k; j++)
    {
        w = INT64_MAX;
        act |= host_step<ND>(s, h, slot + j, &w, lane, lanes, pool, hints, due);
    }
#ifdef RDMA_PROF
    if (lane == 0)
        atomicMax(&s.ev[16], (unsigned long long)(clock64() - p0));
#endif
    if (lane == 0)
    {
        if (k > 1)
            s.hz[h] = slot + k;
        slot_end(s, slot, act, w != INT64_MAX ? (unsigned long long)w : ~0ull);
    }
}

template <bool WIDE>
__device__ __forceinline__ void switch_block(const Sim &s, int x, char *smem, int kl)
{
    int64_t slot = *s.slot;
    int lane = threadIdx.x, lanes = blockDim.x;
#ifdef RDMA_PROF
    long long p0 = clock64();
#endif
    // a switch's workspace: its ports and ints in LDS, its arrival pool and its index lists in
    // global memory
    SwPortLds *w = reinterpret_cast<SwPortLds *>(smem);
    int *ints = reinterpret_cast<int *>(smem + (size_t)kl * sizeof(SwPortLds));
    ArrLds *pool = s.apool + (size_t)x * POOL;
    int *pidx = s.aidx + (size_t)x * 2 * POOL;
    __shared__ int blk_act, win;
    if (s.any_win)
    {
        if (lane == 0)
            win = entity_window(s, s.H + x, slot);
        __syncthreads();
    }
    const int k = s.any_win ? win : 1;
    if (k == 0)
    {
        // ahead: busy (frames queued: TransmitCompletes due) only until its window's end
        if (lane == 0)
            slot_end(s, slot, 0, s.sw_busy[x] ? (unsigned long long)(s.hz[s.H + x] * s.P.delay) : ~0ull);
        return;
    }
    int any_act = 0;
    for (int j = 0; j < k; j++)
    {
        const int64_t sl = slot + j;
        if (lane == 0)
            blk_act = 0;
        __syncthreads();
        // an idle switch (nothing queued or in transmission) without arrivals has no events: skip it
        bool run = s.sw_busy[x];
        if (!run)
        {
            const int node = s.H + x, deg = s.deg[node];
            int any = 0;
            for (int p = lane; p < deg; p += lanes)
            {
                const PortStatic &ps = s.pst[s.pbase[x] + p];
                long long v = s.msg_cnt[ps.in_coff + (int)fmod(ps.in_fnb, sl)];
                any |= (v >> 16) == sl && (v & 0xFFFF) > 0;
            }
            run = __syncthreads_or(any);
        }
        if (run && switch_step<WIDE>(s, x, sl, w, pool, pidx, lane, lanes, ints, kl))
            blk_act = 1; // (any lane: the ports are spread over them)
        __syncthreads();
        if (lane == 0)
            s.sw_busy[x] = blk_act;
        any_act |= blk_act;
        if (k > 1)
            __syncthreads(); // (blk_act is reset for the next slot)
    }
#ifdef RDMA_PROF
    if (lane == 0)
        atomicMax(&s.ev[17], (unsigned long long)(clock64() - p0));
#endif
    if (lane == 0)
    {
        if (k > 1)
            s.hz[s.H + x] = slot + k;
        slot_end(s, slot, any_act, ~0ull);
    }
}

// the slot kernel: blocks 0 .. H - 1 are the hosts, then the switches
// The simulation's parameters and arrays, in constant memory (set once, before the launches): as a
// kernel argument, its address (taken by the contexts) put a copy in every wave's scratch at its
// start, and every field read went there
__constant__ Sim c_sim;

// A block is one wave (BLOCK lanes, --spread at most): the compiler may give it all the registers
constexpr int BLOCK = 32;
// (MULTI: the hosts with several devices, GPUs, first: blocks start in order, and they are often
// a slot's last to finish; their path's scratch, which limits the waves in flight, stays out of
// the kernel without them)
template <bool MULTI>
__device__ __forceinline__ void slot_entity(const Sim &s, int b, char *smem)
{
    if (MULTI)
    {
        if (b < s.nm)
        {
            host_block<MAXD>(s, s.hlistm[b], smem);
            return;
        }
        b -= s.nm;
    }
    if (b < s.n1)
        host_block<1>(s, s.hlist1[b], smem);
    else
        switch_block<false>(s, s.slist_s[b - s.n1], smem, s.ks);
}
template <bool MULTI>
__global__ void __launch_bounds__(BLOCK) rdma_slot()
{
    const Sim &s = c_sim;
    extern __shared__ __align__(16) char smem[];
    if (*s.finished)
        return;
    slot_entity<MULTI>(s, blockIdx.x, smem);
}

// the switches with many ports (spines), alongside rdma_slot
__global__ void __launch_bounds__(BLOCK) rdma_slot_big()
{
    const Sim &s = c_sim;
    extern __shared__ __align__(16) char smem[];
    if (*s.finished)
        return;
    switch_block<true>(s, s.slist_b[blockIdx.x], smem, s.kb);
}

// Ranks ahead, alongside a launch's kernels every rank_every launches (a block per wide switch and
// slot): the arrivals of slot + rank_ahead + i (i < rank_every) are all filed when the in-links
// are at least rank_ahead + rank_every + 1 slots long. Their ties
// (same time) are decided as first() would, a pair per lane, so the switch's events need not walk
// chains (lockstep lineages: ~150 tail nodes per comparison) on the launch's critical path.
struct RankArr
{
    int64_t ts;
    Run ap[PRE];
    uint64_t ah;
    uint32_t ar, acode, frame;
    int port, rank, hard;
};
constexpr int RPOOL = 256, RPAIRS = 2048;
__global__ void rdma_rank()
{
    const Sim &s = c_sim;
    if (*s.finished)
        return;
    const int x = s.slist_b[blockIdx.x % s.ns_b];
    if (!s.rk_ok[x])
        return;
    const int64_t a = *s.slot + s.rank_ahead + blockIdx.x / s.ns_b;
    const int lane = threadIdx.x, lanes = blockDim.x, node = s.H + x, deg = s.deg[node];
    __shared__ RankArr ra[RPOOL];
    __shared__ int pr[RPAIRS][3];
    __shared__ int n, np, cnt[256], off[257];
    __shared__ uint32_t base[256];
    for (int p = lane; p < deg; p += lanes)
        cnt[p] = bucket(s, s.in_link[(size_t)node * s.K + p], a, &base[p]);
    __syncthreads();
    if (lane == 0)
    {
        off[0] = 0;
        for (int p = 0; p < deg; p++)
            off[p + 1] = off[p] + cnt[p];
        n = off[deg];
        np = 0;
    }
    __syncthreads();
    if (n > RPOOL || n < 2)
        return; // (none to rank, or too many: the switch compares as before)
    for (int j = lane; j < n; j += lanes)
    {
        int p = 0;
        while (j >= off[p + 1])
            p++;
        uint32_t fi = base[p] + (j - off[p]);
        const LFrame &f = s.msg[fi];
        int link = s.in_link[(size_t)node * s.K + p];
        RankArr &e = ra[j];
        e.ts = f.ts;
        e.acode = arrival_code(s, link, s.li[link].delay, f);
        cprefix(e.ap, e.acode, f.c);
        e.ah = chash(f.c.hash, e.acode);
        e.ar = f.c.root;
        e.frame = fi;
        e.port = p;
        e.rank = 0;
        e.hard = 0;
    }
    __syncthreads();
    for (int i = lane; i < n; i += lanes)
        for (int j = i + 1; j < n; j++)
            if (ra[j].ts == ra[i].ts)
            {
                int k = atomicAdd(&np, 1);
                if (k < RPAIRS)
                {
                    pr[k][0] = i;
                    pr[k][1] = j;
                }
            }
    __syncthreads();
    if (np > RPAIRS)
        return;
    // only arrivals in a pair the prefixes leave open are ranked (against all of their time): the
    // switch decides the others' ties by the prefixes as cheaply as by ranks
    for (int k = lane; k < np; k += lanes)
    {
        const RankArr &A = ra[pr[k][0]], &B = ra[pr[k][1]];
        int r = qcmp(s, A.ap, A.ah, A.ar, B.ap, B.ah, B.ar);
        pr[k][2] = r;
        if (r == 0)
        {
            ra[pr[k][0]].hard = ra[pr[k][1]].hard = 1;
            atomicAdd(&s.ev[34], 1ull);
        }
    }
    __syncthreads();
    for (int k = lane; k < np; k += lanes)
    {
        const RankArr &A = ra[pr[k][0]], &B = ra[pr[k][1]];
        if (!A.hard && !B.hard)
            continue;
        int r = pr[k][2];
        if (r == 0)
        {
            bool inexact;
            r = ccmp(s, A.acode, s.msg[A.frame].c, B.acode, s.msg[B.frame].c, &inexact);
            if (inexact)
                atomicAdd(&s.ev[9], 1ull);
            if (r == 0)
            {
                atomicAdd(&s.ev[8], 1ull);
                r = A.port < B.port ? -1 : 1; // (undecided: in port order, as first())
            }
        }
        atomicAdd(&ra[r < 0 ? pr[k][1] : pr[k][0]].rank, 1);
    }
    __syncthreads();
    for (int j = lane; j < n; j += lanes)
        if (ra[j].hard)
            s.frank[ra[j].frame] = a << 20 | ra[j].rank;
}

// Persistent (when every block fits on the GPU at once, and no switch is wide): a launch runs up to
// `slots` slots, rdma_slot's blocks waiting for the slot's last one (slot_end) instead of the next
// launch.
template <bool MULTI>
__global__ void __launch_bounds__(BLOCK) rdma_persist(int slots)
{
    const Sim &s = c_sim;
    extern __shared__ __align__(16) char smem[];
    const int b = blockIdx.x;
    for (int it = 0; it < slots; it++)
    {
        int64_t slot = *(volatile int64_t *)s.slot;
        if (*(volatile int *)s.finished)
            return;
        slot_entity<MULTI>(s, b, smem);
        if (threadIdx.x == 0)
        {
            // until the slot's last block has moved on (a block that never runs would stop it all:
            // after about a second, give up and report)
            for (long long n = 0; *(volatile int64_t *)s.slot == slot && !*(volatile int *)s.finished; n++)
            {
                __builtin_amdgcn_s_sleep(1);
                if (n > (1ll << 24))
                {
                    atomicAdd(&s.ev[36], 1ull);
                    *(volatile int *)s.finished = 1;
                    break;
                }
            }
            __threadfence();
        }
        __syncthreads();
    }
}

} // namespace rdma
