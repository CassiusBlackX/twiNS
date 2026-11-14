// The uid oracle (paper, Appendix A; docs/exactness-audit.md): checks lineage order against the
// order in which ns-3 actually ran simultaneous events, on an event log of a real ns-3 run.
//
// The log (NS3_EVLOG, from the audit patch of ns-3's DefaultSimulatorImpl) has a record 'S' for
// every Schedule (uid, parent: the uid of the running event or 0 at set-up, context, time stamp)
// and 'X' for every executed event, in execution order. From it this tool knows each event's
// time, parent, lead and ordinal (its rank among its parent's Schedule calls), hence its full
// lineage. For each group of events that ran at one time stamp it compares pairs (all pairs of a
// group of up to ALL_PAIRS events, else neighbours in execution order and as many random pairs)
// and checks, for each pair (a, b) with a run first:
//   1. Theorem 1: the lineage order (Definition 1, on full lineages) puts a first;
//   2. Lemma A.2: TwiNS's TCP comparator (src/chain_tcp.h, the GPU's code) on the chains TwiNS
//      would build (codes from the leads, run-length encoded, CHAIN_RUNS runs, truncation) puts a
//      first or reports an undecided tie, never b first.
// Pairs at one context (ns-3 node), which are those TwiNS compares, are counted apart.
//
// With a second argument "rdma" (a log of the HPCC fork, validation/audit/hpcc_evlog_patch.py), check
// 2 is TwiNS-RDMA's comparator instead (src/rdma/rstep.cuh ccmp_walk on intact chains: 27-bit lead
// codes, equal lossy codes undecided, 5-bit ordinals that saturate, the deepest ordinal difference
// where the lineages meet, distinct roots in set-up order). Fingerprints (P5) are not checked here:
// on equal lead sequences they give the same verdict as the walk.
//
// usage: oracle LOG [rdma]   [CHAIN_RUNS is fixed at build: -DORACLE_RUNS=9]
// build: g++ -O2 -std=c++17 -I src -DORACLE_RUNS=9 validation/audit/oracle.cc -o oracle
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <iterator>
#include <algorithm>
#include <random>
#include <unordered_map>
#include <vector>

#ifndef ORACLE_RUNS
#define ORACLE_RUNS 9
#endif
constexpr int CHAIN_RUNS = ORACLE_RUNS;
constexpr int CHAIN = 2 * CHAIN_RUNS + 2;
#include "chain_tcp.h"


// The comparator before the audit (src/step.cuh at 7d3d82b), for comparison: 16 ns timer codes
// saturating at 0xFFFE, no truncation mark, a truncated chain that runs out decided by its root.
namespace old
{
inline uint16_t lead(int64_t lead_ps)
{
    int64_t c = 4096 + lead_ps / 16000;
    return (uint16_t)(c < 0xFFFE ? c : 0xFFFE);
}
inline void push(uint16_t *dst, uint16_t code, const uint16_t *parent)
{
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
inline int cmp(const uint16_t *a, const uint16_t *b)
{
    int ia = 0, ib = 0;
    uint32_t ra = a[1], rb = b[1];
    for (;;)
    {
        uint16_t ca = a[2 * ia], cb = b[2 * ib];
        if (ca == 0 && cb != 0 && a[2 * CHAIN_RUNS] != 0)
            return -1;
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
} // namespace old

struct EvRec
{
    uint8_t kind;
    uint8_t pad[3];
    uint32_t uid;
    uint32_t parent;
    uint32_t context;
    uint64_t ts;
};
static_assert(sizeof(EvRec) == 24, "EvRec");

constexpr size_t ALL_PAIRS = 256;
constexpr int64_t LINK_PS = 1000000, PS_PER_BYTE = 80;

static std::vector<uint64_t> g_ts;
static std::vector<uint32_t> g_par, g_ord, g_nsched, g_ctx;
static std::unordered_map<uint32_t, std::vector<uint16_t>> g_chain, g_old; // events not yet run, and the one running

// the code TwiNS's TCP model gives a lead (chain_tcp.h); unmapped: a lead the model has no code for
static uint64_t g_unmapped = 0;
static uint16_t code_of(int64_t lead, bool o = false)
{
    if (lead == 0)
        return CHAIN_NOW;
    if (lead == 1)
        return CHAIN_STEP;
    if (lead < 2048 * PS_PER_BYTE && lead % PS_PER_BYTE == 0)
        return chain_tc((int)(lead / PS_PER_BYTE));
    if (lead >= LINK_PS && lead < LINK_PS + 2048 * PS_PER_BYTE && (lead - LINK_PS) % PS_PER_BYTE == 0)
        return chain_arrival((int)((lead - LINK_PS) / PS_PER_BYTE));
    if (lead >= 2 * LINK_PS)
        return o ? old::lead(lead) : chain_lead(lead);
    g_unmapped++;
    return 0xFFFF; // (lossy: a comparison meeting two of these is undecided)
}

// TwiNS-RDMA's lead codes (src/rdma/rstep.cuh, a copy), leads in ns
constexpr uint32_t LC_LIN = 1u << 26, LC_SAT = (1u << 27) - 2;
static uint32_t rdma_lead_code(int64_t lead)
{
    const uint64_t L = (uint64_t)(lead < 0 ? 0 : lead);
    if (L < LC_LIN)
        return (uint32_t)L + 1;
    const int e = 63 - __builtin_clzll(L);
    if (e > 50)
        return LC_SAT;
    const int sh = e - 20;
    const uint32_t bucket = (uint32_t)(e - 26) * (1u << 20) + (uint32_t)((L >> sh) - (1ull << 20));
    return LC_LIN + 1 + 2 * bucket + ((L & ((1ull << sh) - 1)) ? 1 : 0);
}
static bool rdma_lossy(uint32_t lc) { return lc > LC_LIN && ((lc - LC_LIN - 1) & 1); }
constexpr uint32_t RDMA_ORD_MAX = 31; // (CODE_ORD_MAX: ordinals at or above it share a code)
enum { RW_LOSSY = 1, RW_ORD = 2 };
// < 0: a first, > 0: b first, 0: undecided (*why)
static int rdma_cmp(uint32_t a, uint32_t b, int *why)
{
    int ord = 0; // the deepest ordinal difference: -1/1, 2 unknown (equal saturated ordinals)
    for (;;)
    {
        uint32_t pa = g_par[a], pb = g_par[b];
        if (pa == 0 && pb == 0)
            return a < b ? -1 : 1; // distinct roots: set-up order
        if (pa == 0)
            return -1;
        if (pb == 0)
            return 1;
        const uint32_t ca = rdma_lead_code((int64_t)(g_ts[a] - g_ts[pa])), cb = rdma_lead_code((int64_t)(g_ts[b] - g_ts[pb]));
        if (ca != cb)
            return ca > cb ? -1 : 1;
        if (rdma_lossy(ca))
        {
            *why = RW_LOSSY;
            return 0;
        }
        const uint32_t oa = std::min(g_ord[a], RDMA_ORD_MAX), ob = std::min(g_ord[b], RDMA_ORD_MAX);
        if (oa != ob)
            ord = oa < ob ? -1 : 1;
        else if (oa == RDMA_ORD_MAX)
            ord = 2;
        if (pa == pb) // (the lineages meet: below here they are one)
        {
            if (ord == 2)
            {
                *why = RW_ORD;
                return 0;
            }
            return ord;
        }
        a = pa;
        b = pb;
    }
}

// Definition 1 on full lineages: < 0 if a runs first
static int lin_cmp(uint32_t a, uint32_t b, uint64_t *depth)
{
    for (;;)
    {
        ++*depth;
        uint32_t pa = g_par[a], pb = g_par[b];
        if (pa == 0 && pb == 0)
            return a < b ? -1 : 1; // roots: in set-up order
        if (pa == 0)
            return -1;
        if (pb == 0)
            return 1;
        int64_t la = (int64_t)(g_ts[a] - g_ts[pa]), lb = (int64_t)(g_ts[b] - g_ts[pb]);
        if (la != lb)
            return la > lb ? -1 : 1;
        if (pa == pb)
            return g_ord[a] < g_ord[b] ? -1 : 1;
        a = pa;
        b = pb;
    }
}

int main(int argc, char **argv)
{
    if (argc < 2)
    {
        fprintf(stderr, "usage: oracle LOG [rdma]\n");
        return 2;
    }
    const bool rdma = argc > 2 && !strcmp(argv[2], "rdma");
    FILE *f = fopen(argv[1], "rb");
    if (!f)
    {
        perror(argv[1]);
        return 2;
    }
    std::vector<EvRec> buf(1 << 20);
    std::mt19937_64 rng(1);
    uint64_t sched = 0, ran = 0, groups = 0, tied_events = 0, maxuid = 0;
    // [all pairs, same-context pairs]
    uint64_t pairs[2] = {}, thm_bad[2] = {}, cmp_ok[2] = {}, cmp_und[2] = {}, cmp_bad[2] = {}, depth = 0;
    uint64_t why[2][8] = {};
    uint64_t old_ok[2] = {}, old_und[2] = {}, old_bad[2] = {};
    std::vector<uint32_t> group;
    uint64_t gts = ~0ull;
    auto check = [&](uint32_t a, uint32_t b) {
        int same = g_ctx[a] == g_ctx[b];
        for (int k = 0; k < 1 + same; k++)
            pairs[k]++;
        int t = lin_cmp(a, b, &depth);
        if (t > 0)
            for (int k = 0; k < 1 + same; k++)
                thm_bad[k]++;
        if (rdma)
        {
            int w = 0;
            int c = rdma_cmp(a, b, &w);
            for (int k = 0; k < 1 + same; k++)
            {
                if (c < 0)
                    cmp_ok[k]++;
                else if (c == 0)
                {
                    cmp_und[k]++;
                    why[k][w & 7]++;
                }
                else
                    cmp_bad[k]++;
            }
            return;
        }
        {
            auto oa = g_old.find(a), ob = g_old.find(b);
            if (oa != g_old.end() && ob != g_old.end())
            {
                int c = old::cmp(oa->second.data(), ob->second.data());
                for (int k = 0; k < 1 + same; k++)
                    (c < 0 ? old_ok : c == 0 ? old_und : old_bad)[k]++;
            }
        }
        auto ia = g_chain.find(a), ib = g_chain.find(b);
        if (ia == g_chain.end() || ib == g_chain.end())
            return;
        int w = 0;
        int c = chain_cmp(ia->second.data(), ib->second.data(), &w);
        for (int k = 0; k < 1 + same; k++)
        {
            if (c < 0)
                cmp_ok[k]++;
            else if (c == 0)
            {
                cmp_und[k]++;
                why[k][w & 7]++;
            }
            else
                cmp_bad[k]++;
        }
    };
    auto flush = [&]() {
        if (group.size() > 1)
        {
            groups++;
            tied_events += group.size();
            size_t n = group.size();
            if (n <= ALL_PAIRS)
            {
                for (size_t i = 0; i < n; i++)
                    for (size_t j = i + 1; j < n; j++)
                        check(group[i], group[j]);
            }
            else
            {
                for (size_t i = 0; i + 1 < n; i++)
                    check(group[i], group[i + 1]);
                for (size_t r = 0; r < n; r++)
                {
                    size_t i = rng() % n, j = rng() % n;
                    if (i == j)
                        continue;
                    if (i > j)
                        std::swap(i, j);
                    check(group[i], group[j]);
                }
            }
        }
        for (uint32_t u : group)
        {
            g_chain.erase(u); // (its children have their chains)
            g_old.erase(u);
        }
        group.clear();
    };
    size_t got;
    while ((got = fread(buf.data(), sizeof(EvRec), buf.size(), f)) > 0)
    {
        for (size_t i = 0; i < got; i++)
        {
            const EvRec &r = buf[i];
            if (r.uid >= g_ts.size())
            {
                size_t n = std::max<size_t>(r.uid + 1, g_ts.size() * 2);
                g_ts.resize(n);
                g_par.resize(n);
                g_ord.resize(n);
                g_nsched.resize(n);
                g_ctx.resize(n);
            }
            if (r.kind == 'S')
            {
                sched++;
                maxuid = std::max<uint64_t>(maxuid, r.uid);
                g_ts[r.uid] = r.ts;
                g_par[r.uid] = r.parent;
                g_ctx[r.uid] = r.context;
                if (rdma)
                {
                    g_ord[r.uid] = r.parent ? g_nsched[r.parent]++ : 0;
                    continue;
                }
                std::vector<uint16_t> ch(CHAIN), oc(CHAIN);
                if (r.parent == 0)
                {
                    g_ord[r.uid] = 0;
                    chain_set_root(ch.data(), CHAIN_ROOT_APP + r.uid); // (set-up order: the uid)
                    oc = ch;
                }
                else
                {
                    g_ord[r.uid] = g_nsched[r.parent]++;
                    auto p = g_chain.find(r.parent), q = g_old.find(r.parent);
                    if (p == g_chain.end() || q == g_old.end())
                        continue; // (scheduled outside a logged event)
                    const int64_t lead = (int64_t)(r.ts - g_ts[r.parent]);
                    chain_push(ch.data(), code_of(lead), p->second.data());
                    old::push(oc.data(), code_of(lead, true), q->second.data());
                }
                g_chain[r.uid] = std::move(ch);
                g_old[r.uid] = std::move(oc);
            }
            else if (r.kind == 'X')
            {
                ran++;
                if ((ran & ((1u << 22) - 1)) == 0)
                    for (auto it = g_chain.begin(); it != g_chain.end();)
                        it = g_ts[it->first] < r.ts ? g_chain.erase(it) : std::next(it); // (cancelled events)
                if ((ran & ((1u << 22) - 1)) == 0)
                    for (auto it = g_old.begin(); it != g_old.end();)
                        it = g_ts[it->first] < r.ts ? g_old.erase(it) : std::next(it);
                if (r.ts != gts)
                {
                    flush();
                    gts = r.ts;
                }
                group.push_back(r.uid);
            }
        }
    }
    flush();
    printf("log %s: %llu events scheduled, %llu run, max uid %llu (2^32 = 4294967296)\n", argv[1], (unsigned long long)sched,
           (unsigned long long)ran, (unsigned long long)maxuid);
    printf("time stamps with simultaneous events: %llu, holding %llu events; leads without a TwiNS code: %llu\n",
           (unsigned long long)groups, (unsigned long long)tied_events, (unsigned long long)g_unmapped);
    const char *name[2] = {"all pairs", "same node"};
    if (rdma)
    {
        for (int k = 0; k < 2; k++)
            printf("%s: %llu checked; Theorem 1 (full lineages) disagrees %llu; TwiNS-RDMA comparator: right %llu, undecided %llu "
                   "(lossy %llu ordinal %llu), WRONG %llu\n",
                   name[k], (unsigned long long)pairs[k], (unsigned long long)thm_bad[k], (unsigned long long)cmp_ok[k],
                   (unsigned long long)cmp_und[k], (unsigned long long)why[k][RW_LOSSY], (unsigned long long)why[k][RW_ORD],
                   (unsigned long long)cmp_bad[k]);
        printf("lineage levels walked: %llu\n", (unsigned long long)depth);
        return (thm_bad[0] || cmp_bad[0]) ? 1 : 0;
    }
    for (int k = 0; k < 2; k++)
        printf("%s: %llu checked; Theorem 1 (full lineages) disagrees %llu; chain comparator (%d runs): "
               "right %llu, undecided %llu (lossy %llu trunc %llu same-root %llu count %llu no-root %llu), WRONG %llu\n",
               name[k], (unsigned long long)pairs[k], (unsigned long long)thm_bad[k], CHAIN_RUNS, (unsigned long long)cmp_ok[k],
               (unsigned long long)cmp_und[k], (unsigned long long)why[k][TIE_LOSSY], (unsigned long long)why[k][TIE_TRUNC],
               (unsigned long long)why[k][TIE_SAME], (unsigned long long)why[k][TIE_CNT], (unsigned long long)why[k][TIE_NROOT],
               (unsigned long long)cmp_bad[k]);
    for (int k = 0; k < 2; k++)
        printf("%s, the comparator before the audit: right %llu, undecided %llu, WRONG %llu\n", name[k], (unsigned long long)old_ok[k],
               (unsigned long long)old_und[k], (unsigned long long)old_bad[k]);
    printf("lineage levels walked: %llu\n", (unsigned long long)depth);
    return (thm_bad[0] || cmp_bad[0]) ? 1 : 0;
}
