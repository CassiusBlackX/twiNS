// The slot kernel (included by main.cu once per model variant, inside its namespace)
namespace TWINS_NS
{

__global__ void ns_slot(Due *due_buf, int64_t *sw_ts, uint32_t *sw_ref, uint8_t *sw_up)
{
    const Sim &s = c_sim;
    if (*s.finished)
        return;
    int64_t t = *s.slot;
    __shared__ int rows[BLOCK * 33];
    __shared__ int base[BLOCK], cnt[BLOCK];
    __shared__ int in_win[BLOCK * 3];
    __shared__ int mpos[BLOCK];
    __shared__ int blk_act;
    __shared__ unsigned long long blk_wake;
    if (threadIdx.x == 0)
    {
        blk_act = 0;
        blk_wake = ~0ull;
    }
    __syncthreads();
    if (threadIdx.x == 0)
    {
        if (blk_act)
            atomicAdd(&s.activity[0], 1u);
        if (blk_wake != ~0ull)
            atomicMin(s.next_wake, blk_wake);
    }
#ifdef NS_PROF
    long long p0 = wall_clock64();
#endif
    if (blockIdx.x < s.host_blocks)
    {
        int h = blockIdx.x * s.host_lanes + threadIdx.x;
        if (threadIdx.x < s.host_lanes && h < s.H)
        {
            int64_t w = INT64_MAX;
            if (host_step(s, h, t, due_buf + (size_t)h * MAX_DUE, &w))
                blk_act = 1;
            atomicMin(&blk_wake, (unsigned long long)w);
        }
    }
    else
    {
        int b = blockIdx.x - s.host_blocks;
        int local = threadIdx.x / s.k, q = threadIdx.x % s.k;
        int sw = b * s.sw_per_block + local;
        bool active = local < s.sw_per_block && sw < s.S;
        int l = active ? local : 0;
        size_t per_sw = (size_t)s.k * 2 * MAX_TX;
        SwScratch sc;
        sc.rows = rows + l * s.k * (s.k + 1);
        sc.base = base + l * s.k;
        sc.cnt = cnt + l * s.k;
        sc.in_win = in_win + l * s.k * 3;
        sc.mpos = mpos + l * s.k;
        sc.up = sw_up + (size_t)(active ? sw : 0) * per_sw;
        sc.ts = sw_ts + (size_t)(active ? sw : 0) * per_sw;
        sc.ref = sw_ref + (size_t)(active ? sw : 0) * per_sw;
        if (switch_step(s, sw, q, t, active, sc))
            blk_act = 1;
    }
    __syncthreads();
    if (threadIdx.x == 0)
    {
        if (blk_act)
            atomicAdd(&s.activity[0], 1u);
        if (blk_wake != ~0ull)
            atomicMin(s.next_wake, blk_wake);
    }
#ifdef NS_PROF
    if (threadIdx.x == 0)
    {
        unsigned long long d = (unsigned long long)(wall_clock64() - p0);
        atomicMax(&s.ev[blockIdx.x < s.host_blocks ? 9 + 6 : 10 + 6], d); // per-slot maxima (ev[15], ev[16])
    }
#endif
    if (threadIdx.x == 0)
    {
        __threadfence();
        unsigned int ticket = atomicAdd(s.done_blocks, 1u);
        if (ticket == gridDim.x - 1)
        {
            __threadfence();
            unsigned int fd = atomicAdd(s.flows_done, 0u);
            if (fd >= (unsigned int)s.M || t + 1 >= s.max_slots)
            {
                *s.finished = 1;
                *s.finished_host = 1;
            }
            // exact idle skip: nothing sent in this or the previous slot (so nothing arrives in
            // the next) and no queued frames: run next at the earliest host timer or flow start
            unsigned int cur = atomicAdd(&s.activity[0], 0u), prev = s.activity[1];
            unsigned long long nw = atomicAdd(s.next_wake, 0ull);
            int64_t next = t + 1;
            if (cur == 0 && prev == 0 && nw != ~0ull && (int64_t)nw > next)
                next = (int64_t)nw < s.max_slots ? (int64_t)nw : s.max_slots;
            s.activity[1] = cur;
            s.activity[0] = 0;
            *s.next_wake = ~0ull;
            *s.slot = next;
            *s.done_blocks = 0;
#ifdef NS_PROF
            s.ev[17] += s.ev[15];
            s.ev[18] += s.ev[16];
            s.ev[15] = 0;
            s.ev[16] = 0;
#endif
            __threadfence();
        }
    }
}

} // namespace TWINS_NS
