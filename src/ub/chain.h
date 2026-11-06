// An event's place in ns-3's event order, by lineage (after src/rdma/rdma.h Chain and
// rstep.cuh ccmp, with 64-bit run codes for picosecond leads and wide ordinals).
//
// ns-3 runs simultaneous events in the order they were scheduled. An event's chain is the
// sequence, from it back to its root, of (lead, ordinal): lead = its delay (the time from its
// parent's execution to its own), ordinal = the number of events its parent had scheduled before
// it. Two simultaneous events run in the order of their first differing lead going back (the
// larger lead: an ancestor that ran earlier, so scheduled earlier); if all leads are equal the
// ancestors ran at equal times, and the ordinals at the deepest level where they differ decide
// (siblings run in the order they were scheduled). Roots are the tasks started at load, in their
// start order. Chains are run-length encoded: RUNS runs inline, older runs spill into TailNodes of
// one shared ring (immutable; pos tells if the ring has overwritten one: the comparison is then
// counted as inexact).
#pragma once
#include <cstdint>
#include <cstring>

#ifndef UB_HD
#if defined(__CUDACC__) || defined(__HIPCC__)
#define UB_HD __host__ __device__
#else
#define UB_HD
#endif
#endif

namespace ub {

#ifndef UB_RUNS
#define UB_RUNS 6
#endif
constexpr int RUNS = UB_RUNS;
constexpr int ORD_BITS = 20;
constexpr uint64_t ORD_MAX = (1ull << ORD_BITS) - 1;
constexpr uint64_t LEAD_MAX = (1ull << (64 - ORD_BITS)) - 2;
constexpr int NANC = 4;

struct Run {
    uint64_t code;
    uint64_t cnt;
};
struct Chain {
    Run r[RUNS];
    uint64_t tail;          // older runs: a TailNode's pos (0: none)
    uint64_t hash, anc[NANC];
    uint32_t root, pad;     // 1 + the root task's place in the start order (0: none)
};
struct TailNode {
    uint64_t code, cnt, next, pos;
};

struct ChainCtx {
    TailNode *tail;
    uint64_t mask;          // ring size - 1
    uint64_t *cnt;          // nodes allocated
    const uint32_t *rootRank;
    unsigned long long *memo;
    uint64_t memoMask;
    unsigned long long *walk;
    uint64_t walkMask;
    unsigned long long *stat; // [0] undecided ties, [1] inexact (lost tail), [2] ordinal overflow
};

UB_HD inline uint64_t ccode(int64_t lead, uint64_t ord)
{
    uint64_t l = (uint64_t)lead + 1;
    if (l > LEAD_MAX)
        l = LEAD_MAX;
    return l << ORD_BITS | (ORD_MAX - (ord < ORD_MAX ? ord : ORD_MAX));
}
UB_HD inline uint64_t chash(uint64_t h, uint64_t code)
{
    uint64_t z = h + 0x9e3779b97f4a7c15ull * ((code >> ORD_BITS) + 1ull);
    z = (z ^ (z >> 30)) * 0xbf58476d1ce4e5b9ull;
    z = (z ^ (z >> 27)) * 0x94d049bb133111ebull;
    return z ^ (z >> 31);
}

#if defined(__HIPCC__) || defined(__CUDACC__)
#define UB_ATOMIC_ADD(p, v) atomicAdd((unsigned long long *)(p), (unsigned long long)(v))
#define UB_CAS(p, o, n) atomicCAS((unsigned long long *)(p), (unsigned long long)(o), (unsigned long long)(n))
#else
inline unsigned long long ub_atomic_add_host(unsigned long long *p, unsigned long long v)
{
    return __atomic_fetch_add(p, v, __ATOMIC_RELAXED);
}
inline unsigned long long ub_cas_host(unsigned long long *p, unsigned long long o, unsigned long long n)
{
    __atomic_compare_exchange_n(p, &o, n, false, __ATOMIC_RELAXED, __ATOMIC_RELAXED);
    return o;
}
#define UB_ATOMIC_ADD(p, v) ub_atomic_add_host((unsigned long long *)(p), (unsigned long long)(v))
#define UB_CAS(p, o, n) ub_cas_host((unsigned long long *)(p), (unsigned long long)(o), (unsigned long long)(n))
#endif

// the chain of an event scheduled with code by the event whose chain is par
UB_HD inline void cpush(const ChainCtx &x, Chain *dst, uint64_t code, const Chain &par)
{
    Chain c;
    if (par.r[0].code == code) {
        c = par;
        c.r[0].cnt++;
        c.hash = chash(par.hash, code);
    } else {
        c.r[0].code = code;
        c.r[0].cnt = 1;
        for (int i = 1; i < RUNS; i++)
            c.r[i] = par.r[i - 1];
        c.root = par.root;
        c.tail = par.tail;
        c.hash = chash(par.hash, code);
        c.pad = 0;
        if (par.r[RUNS - 1].code) {
            uint64_t pos = UB_ATOMIC_ADD(x.cnt, 1ull) + 1;
            TailNode &n = x.tail[(pos - 1) & x.mask];
            n.code = par.r[RUNS - 1].code;
            n.cnt = par.r[RUNS - 1].cnt;
            n.next = par.tail;
            n.pos = pos;
            c.tail = pos;
        }
    }
    c.anc[0] = par.hash;
    for (int i = 1; i < NANC; i++)
        c.anc[i] = par.anc[i - 1];
    *dst = c;
}

UB_HD inline void chain_root(Chain *c, uint32_t root)
{
    memset(c, 0, sizeof(Chain));
    c->root = root;
}

struct RunCursor {
    const ChainCtx *x;
    const Chain *c;
    int i;
    uint64_t node;
    uint64_t code;
    uint64_t left;
    bool lost;
    UB_HD void init(const ChainCtx &ctx, const Chain &ch)
    {
        x = &ctx;
        c = &ch;
        i = 0;
        node = 0;
        lost = false;
        load();
    }
    UB_HD void load()
    {
        if (i < RUNS) {
            code = c->r[i].code;
            left = c->r[i].cnt;
            if (code || i < RUNS - 1)
                return;
            i = RUNS;
        }
        if (i == RUNS && node == 0) {
            node = c->tail;
            i = RUNS + 1;
        }
        if (node == 0) {
            code = 0;
            return;
        }
        const TailNode &n = x->tail[(node - 1) & x->mask];
        if (n.pos != node) {
            lost = true;
            code = 0;
            return;
        }
        code = n.code;
        left = n.cnt;
    }
    UB_HD void next()
    {
        if (i < RUNS) {
            i++;
            if (i == RUNS) {
                node = c->tail;
                i = RUNS + 1;
            }
        } else
            node = x->tail[(node - 1) & x->mask].next;
        load();
    }
};

UB_HD inline uint64_t memo_key(uint64_t ha, uint64_t hb)
{
    uint64_t k = chash(ha * 0x9e3779b97f4a7c15ull ^ hb, 0x5bd1e995u) & ~3ull;
    return k ? k : 4;
}
UB_HD inline int memo_get(const ChainCtx &x, uint64_t ha, uint64_t hb)
{
    uint64_t k = memo_key(ha, hb);
    for (int i = 0; i < 4; i++) {
        unsigned long long v = x.memo[(k + i) & x.memoMask];
        if (v == 0)
            return 0;
        if ((v & ~3ull) == k)
            return (v & 3) == 1 ? -1 : 1;
    }
    return 0;
}
UB_HD inline void memo_put(const ChainCtx &x, uint64_t ha, uint64_t hb, int r)
{
    uint64_t k = memo_key(ha, hb);
    unsigned long long v = k | (r < 0 ? 1 : 2);
    for (int i = 0; i < 4; i++) {
        unsigned long long *p = &x.memo[(k + i) & x.memoMask];
        unsigned long long old = UB_CAS(p, 0ull, v);
        if (old == 0 || (old & ~3ull) == k)
            return;
    }
}

// < 0: a first, > 0: b first, 0: undecided; *inexact: rests on an overwritten tail
constexpr int WALK_EVERY = 16, WALK_KEEP = 4;
UB_HD inline int ccmp_walk(const ChainCtx &x, const Chain &a, const Chain &b, bool *inexact, bool *by_lead)
{
    *inexact = false;
    *by_lead = false;
    int ord = 0;
    RunCursor A, B;
    A.init(x, a);
    B.init(x, b);
    uint64_t seen[WALK_KEEP];
    int nseen = 0;
    for (;;) {
        uint64_t ca = A.code, cb = B.code;
        if (ca == 0 || cb == 0) {
            *inexact = (ca == 0 && A.lost) || (cb == 0 && B.lost);
            if (ca == 0 && cb == 0) {
                if (a.root && b.root && a.root != b.root)
                    return x.rootRank[a.root - 1] < x.rootRank[b.root - 1] ? -1 : 1;
                return ord;
            }
            int r = ca == 0 ? (A.lost ? 0 : -1) : (B.lost ? 0 : 1);
            if (r == 0)
                return ord;
            *by_lead = true;
            for (int i = 0; i < nseen; i++)
                x.walk[seen[i] >> 2 & x.walkMask] = seen[i] | (r < 0 ? 1 : 2);
            return r;
        }
        if (ca >> ORD_BITS != cb >> ORD_BITS) {
            int r = ca > cb ? -1 : 1;
            *by_lead = true;
            for (int i = 0; i < nseen; i++)
                x.walk[seen[i] >> 2 & x.walkMask] = seen[i] | (r < 0 ? 1 : 2);
            return r;
        }
        if (ca != cb)
            ord = (ca & ORD_MAX) > (cb & ORD_MAX) ? -1 : 1;
        if (A.i > RUNS && B.i > RUNS && A.node == B.node && A.left == B.left)
            return ord;
        if (A.i > RUNS && B.i > RUNS) {
            const uint64_t k = memo_key(A.node + (A.left << 40), B.node + (B.left << 40));
            if ((k >> 2 & (WALK_EVERY - 1)) == 0) {
                const unsigned long long v = x.walk[k >> 2 & x.walkMask];
                if ((v & ~3ull) == k) {
                    *by_lead = true;
                    return (v & 3) == 1 ? -1 : 1;
                }
                if (nseen < WALK_KEEP)
                    seen[nseen++] = k;
            }
        }
        uint64_t m = A.left < B.left ? A.left : B.left;
        A.left -= m;
        B.left -= m;
        if (A.left == 0)
            A.next();
        if (B.left == 0)
            B.next();
    }
}

UB_HD inline int ccmp(const ChainCtx &x, const Chain &a, const Chain &b, bool *inexact)
{
    uint64_t ha[NANC + 1], hb[NANC + 1];
    ha[0] = a.hash;
    hb[0] = b.hash;
    for (int i = 1; i <= NANC; i++) {
        ha[i] = a.anc[i - 1];
        hb[i] = b.anc[i - 1];
    }
    if (a.root && b.root && a.root != b.root && ha[0] == hb[0]) {
        *inexact = false;
        return x.rootRank[a.root - 1] < x.rootRank[b.root - 1] ? -1 : 1;
    }
    RunCursor A, B;
    A.init(x, a);
    B.init(x, b);
    int eq = 0;
    for (int j = 0;; j++) {
        int m = memo_get(x, ha[j], hb[j]);
        if (m) {
            *inexact = false;
            return m;
        }
        if (j == NANC || A.code == 0 || B.code == 0 || (A.code >> ORD_BITS) != (B.code >> ORD_BITS))
            break;
        eq = j + 1;
        if (--A.left == 0)
            A.next();
        if (--B.left == 0)
            B.next();
    }
    bool by_lead;
    int r = ccmp_walk(x, a, b, inexact, &by_lead);
    if (by_lead && !*inexact)
        for (int j = 0; j <= eq; j++)
            memo_put(x, ha[j], hb[j], r);
    return r;
}

// does (ta, a) run before (tb, b)? Undecided ties keep b.
UB_HD inline bool before(const ChainCtx &x, int64_t ta, const Chain &a, int64_t tb, const Chain &b)
{
    if (ta != tb)
        return ta < tb;
    bool inexact;
    int c = ccmp(x, a, b, &inexact);
    if (inexact)
        UB_ATOMIC_ADD(&x.stat[1], 1ull);
    if (c == 0)
        UB_ATOMIC_ADD(&x.stat[0], 1ull);
    return c < 0;
}

} // namespace ub
