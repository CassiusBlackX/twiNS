// The TCP model's event-order chains (see Appendix A of the paper and docs/exactness-audit.md):
// codes, push, comparison. Shared by the GPU code (step.cuh) and the uid oracle
// (validation/audit/oracle.cc), which includes it after defining CHAIN_RUNS and CHAIN.
// (no include guard: step.cuh, and with it this file, is included once in each kernel's namespace)
#include <cstdint>
#if !defined(__CUDACC__) && !defined(__HIPCC__)
#ifndef __host__
#define __host__
#endif
#ifndef __device__
#define __device__
#endif
#endif

// count of leading zeros of a nonzero 64-bit value, on the host and on both GPU backends
__host__ __device__ inline int twins_clz64(uint64_t x)
{
#if defined(__CUDA_ARCH__)
    return __clzll((long long)x);
#else
    return __builtin_clzll(x);
#endif
}

// Codes of the lead from a parent event to its child, increasing with the lead:
//   ScheduleNow (CompleteFork, ConnectionSucceeded) 1
//   Socket::Send's TimeStep(1) deferral          2
//   TransmitComplete of a frame of len bytes     len             (lead len * 80 ps; len < 2048)
//   arrival of a frame of len bytes              2048 + len      (lead 1 us + len * 80 ps)
//   host timers (lead >= 2 us)                   4096 + 2 b + x  (see chain_lead)
// Every code but the timers' odd ones stands for exactly one lead, so equal codes mean equal
// leads. A timer's lead L is put in bucket b(L) of a monotone grid: q = floor(L / 1 us); b = q
// below 1024, else q with its 11 leading bits kept (1024 buckets per octave). x = 0 iff L is the
// bucket's least value (an exact code: 10 us, 1 ms * 2^n and 10 ms * 2^n are), else 1 (a lossy
// code: equal lossy codes need not be equal leads, and a comparison that meets two of them is
// undecided). Codes of different buckets order as their leads; within a bucket the exact code
// is the least lead. Timer leads below 2 us would not order above the arrivals' codes: counted.
#ifdef TWINS_LINK_TIMING
using HistoryWord = uint64_t;
__host__ __device__ inline HistoryWord chain_lead(int64_t ps) { return uint64_t(ps) + 1; }
__host__ __device__ inline bool chain_lossy(HistoryWord) { return false; }
constexpr HistoryWord CHAIN_NOW = 1, CHAIN_STEP = 2;
#else
using HistoryWord = uint16_t;
__host__ __device__ inline uint16_t chain_tc(int len) { return (uint16_t)len; }
__host__ __device__ inline uint16_t chain_arrival(int len) { return (uint16_t)(2048 + len); }
constexpr uint16_t CHAIN_TIMER0 = 4096;
__host__ __device__ inline uint16_t chain_lead(int64_t lead_ps)
{
    uint64_t q = (uint64_t)lead_ps / 1000000, r = (uint64_t)lead_ps % 1000000;
    uint32_t b;
    bool exact;
    if (q < 1024)
    {
        b = (uint32_t)q;
        exact = r == 0;
    }
    else
    {
        int e = 63 - twins_clz64(q); // 2^e <= q < 2^(e+1), e >= 10
        int sh = e - 10;
        b = 1024u * (uint32_t)(e - 9) + (uint32_t)((q >> sh) - 1024);
        exact = r == 0 && (q & ((1ull << sh) - 1)) == 0;
    }
    if (b > 30719)
        return 0xFFFF; // beyond 2^38 us: lossy
    return (uint16_t)(CHAIN_TIMER0 + 2 * b + (exact ? 0 : 1));
}
__host__ __device__ inline bool chain_lossy(uint16_t code) { return code >= CHAIN_TIMER0 && (code & 1); }
constexpr uint16_t CHAIN_NOW = 1, CHAIN_STEP = 2;

#endif
// A chain: the leads from an event back towards its root, run-length encoded as up to
// CHAIN_RUNS (code, count) pairs (back-to-back transmissions form long runs of TransmitComplete
// codes), then the root, an event scheduled before the simulation runs, as a 32-bit key in
// ns-3's scheduling order (world-local): 1 + i for the i-th message write that the scenario
// schedules in main(), CHAIN_ROOT_APP + rank for the application starts, which the nodes
// schedule as they initialize, ranked by (host, connection) on the host (Sim::app_key). Bit 31
// of the key (CHAIN_TRUNC) marks a truncated chain: its runs overflowed and the oldest were
// dropped, so what follows its last recorded run is unknown. A count that reaches 0xFFFF stays
// there (CHAIN_CNT_SAT): the run may be longer.
constexpr uint32_t CHAIN_ROOT_APP = 0x40000000u, CHAIN_TRUNC = 0x80000000u;
constexpr uint16_t CHAIN_CNT_SAT = 0xFFFF;
__host__ __device__ inline void chain_set_root(HistoryWord *dst, uint32_t key)
{
    for (int i = 0; i < 2 * CHAIN_RUNS; i++)
        dst[i] = 0;
    dst[2 * CHAIN_RUNS] = (uint16_t)(key >> 16);
    dst[2 * CHAIN_RUNS + 1] = (uint16_t)key;
}
__host__ __device__ inline uint32_t chain_root_key(const HistoryWord *c)
{
    return (uint32_t)c[2 * CHAIN_RUNS] << 16 | c[2 * CHAIN_RUNS + 1];
}
__host__ __device__ inline void chain_root_msg(HistoryWord *dst, int msg_local) { chain_set_root(dst, 1u + (uint32_t)msg_local); }

__host__ __device__ inline void chain_push(HistoryWord *dst, HistoryWord code, const HistoryWord *parent)
{
#ifdef NS_NO_CHAIN
    return;
#endif
#ifdef TWINS_EVIDENCE
    unsigned long long len = 1, nr = 1;
    if (parent)
        for (int i = 0; i < CHAIN_RUNS; i++)
            if (parent[2 * i]) { len += parent[2 * i + 1]; nr++; }
    bool ext = parent && parent[0] == code;
    twins_evidence::push(len, nr - (ext ? 1 : 0), ext, false,
                         parent && (chain_root_key(parent) & CHAIN_TRUNC));
#endif
    if (parent && parent[0] == code)
    {
        for (int i = 0; i < CHAIN; i++)
            dst[i] = parent[i];
        if (dst[1] < CHAIN_CNT_SAT)
            dst[1]++;
        return;
    }
    bool trunc = parent && parent[2 * CHAIN_RUNS - 2] != 0; // the oldest run falls off
    for (int i = 2 * CHAIN_RUNS - 1; i >= 2; i--)
        dst[i] = parent ? parent[i - 2] : 0;
    dst[0] = code;
    dst[1] = 1;
    dst[2 * CHAIN_RUNS] = parent ? parent[2 * CHAIN_RUNS] | (trunc ? (uint16_t)(CHAIN_TRUNC >> 16) : 0) : 0;
    dst[2 * CHAIN_RUNS + 1] = parent ? parent[2 * CHAIN_RUNS + 1] : 0;
}

// Why chain_cmp left a tie undecided (counted in Sim::ev[26 + why])
enum : int
{
    TIE_LOSSY = 0, // two equal lossy timer codes
    TIE_TRUNC,     // the recorded runs of a truncated chain ran out with all leads equal
    TIE_SAME,      // equal leads down to the same root (siblings: the TCP chains keep no ordinals)
    TIE_CNT,       // a saturated run count ran out
    TIE_NROOT,     // a chain without a root
    TIE_ORDER,     // two timers due at the same instant (set-up of the slot's timer list)
    TIE_SHORT      // a timer armed less than 2 us ahead (not ordered by its code)
};

// Order of two events with the same time by their chains: < 0 if a runs first, > 0 if b does,
// 0 if the chains do not tell (*why: the reason). Walking back from the events, the first
// differing lead decides, the larger first (an earlier parent). A complete chain (not
// truncated) that ends while the other goes on has reached its root, an event scheduled at
// set-up, which precedes every other parent. Two complete chains that end together, with all
// leads equal, are decided by their roots' set-up order. Every case in which the recorded
// history cannot tell the reference's order is undecided: equal lossy codes, a truncated chain
// that runs out, a saturated count that runs out, equal roots. (Appendix A, Lemma A.2.)
__host__ __device__ inline int chain_cmp_impl(const HistoryWord *a, const HistoryWord *b, int *why, unsigned long long *steps)
{
    int dummy;
    if (!why)
        why = &dummy;
    const uint32_t xa = chain_root_key(a), xb = chain_root_key(b);
    const bool ta = (xa & CHAIN_TRUNC) != 0, tb = (xb & CHAIN_TRUNC) != 0;
    int ia = 0, ib = 0;
    uint32_t ra = a[1], rb = b[1];
    for (;;)
    {
#ifdef TWINS_EVIDENCE
        ++*steps;
#endif
        HistoryWord ca = ia < CHAIN_RUNS ? a[2 * ia] : 0, cb = ib < CHAIN_RUNS ? b[2 * ib] : 0;
        if (ca == 0 || cb == 0)
        {
            if ((ca == 0 && ta) || (cb == 0 && tb))
            {
                *why = TIE_TRUNC;
                return 0;
            }
            if ((ca == 0 && (xa & ~CHAIN_TRUNC) == 0) || (cb == 0 && (xb & ~CHAIN_TRUNC) == 0))
            {
                *why = TIE_NROOT;
                return 0;
            }
            if (ca == 0 && cb != 0)
                return -1; // a reached its root
            if (cb == 0 && ca != 0)
                return 1;
            if (xa == xb)
            {
                *why = TIE_SAME;
                return 0;
            }
            return xa < xb ? -1 : 1;
        }
        if (ca != cb)
            return ca > cb ? -1 : 1;
        if (chain_lossy(ca))
        {
            *why = TIE_LOSSY;
            return 0;
        }
        uint32_t m = ra < rb ? ra : rb;
        ra -= m;
        rb -= m;
        if (ra == 0)
        {
            if (a[2 * ia + 1] == CHAIN_CNT_SAT)
            {
                *why = TIE_CNT;
                return 0;
            }
            if (++ia < CHAIN_RUNS)
                ra = a[2 * ia + 1];
        }
        if (rb == 0)
        {
            if (b[2 * ib + 1] == CHAIN_CNT_SAT)
            {
                *why = TIE_CNT;
                return 0;
            }
            if (++ib < CHAIN_RUNS)
                rb = b[2 * ib + 1];
        }
    }
}


__host__ __device__ inline int chain_cmp(const HistoryWord *a,const HistoryWord *b,int *why=nullptr) {
 unsigned long long steps=0;
 int r=chain_cmp_impl(a,b,why,&steps);
#ifdef TWINS_EVIDENCE
 twins_evidence::comparison(steps);
#endif
 return r;
}
