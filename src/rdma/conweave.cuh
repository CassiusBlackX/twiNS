// Single-epoch receive VOQ from ConWeave. Source paths are prescribed by the
// caller, and only the selected data QP carries the packet tags. No feedback,
// adaptive path selection, or epoch aging is modeled by this experiment.
struct ConWeaveState
{
    uint64_t tail_tx, rx_tail_tx; // source state and the last timestamp learned at the receiver
    int64_t phase0_tx, phase0_rx, due;
    Chain flush_chain;
    uint32_t length, high_water, flushes, overflow;
    bool phase1, have_phase0, tail_seen;
};

__device__ __forceinline__ void cw_record(const Sim &s, int64_t t, int node,
                                         const char *op, uint32_t seq, uint64_t value)
{
    if (s.conweave_trace)
        printf("CW %lld %u %s %u %llu\n", (long long)t, node, op, seq, (unsigned long long)value);
}

__device__ __forceinline__ void cw_schedule(SCtx &c, ConWeaveState &state,
                                           uint32_t seq, int64_t deadline)
{
    state.due = deadline > c.now ? deadline : c.now;
    cpush(&state.flush_chain, ccode(state.due - c.now, c.ord++), sc_cur(c));
    cw_record(*c.s, c.now, c.node, "schedule", seq, state.due);
}

__device__ __noinline__ void cw_forward(SCtx &c, int ingress, Pkt p, int egress, int q)
{
    const Sim &s = *c.s;
    ConWeaveState &state = *s.conweave;
    const bool next_phase = s.conweave_tail < p.seq;
    const bool is_tail_seq = s.conweave_tail == p.seq;
    if (c.node == s.conweave_tx)
    {
        const bool first_tail = is_tail_seq && !state.tail_seen;
        if (first_tail)
        {
            state.tail_seen = true;
            state.tail_tx = c.now;
        }
        // IRN data with DCQCN has no INT. These are out-of-band tags, so
        // neither the serialized packet length nor the MTU changes.
        p.hop[1] = c.now;
        p.hop[2] = state.tail_tx;
        p.hop[3] = first_tail;
        egress = next_phase ? s.conweave_new : s.conweave_old;
        if (s.conweave_drop_tail && first_tail)
        {
            cw_record(s, c.now, c.node, "drop", p.seq, egress + 1);
            return;
        }
        cw_record(s, c.now, c.node, "route", p.seq, egress + 1);
    }
    else
    {
        const bool tail = p.hop[3];
        if (tail || next_phase)
            state.rx_tail_tx = p.hop[2];
        if (!next_phase)
        {
            state.have_phase0 = true;
            state.phase0_tx = p.hop[1];
            state.phase0_rx = c.now;
        }
        const int64_t gap = state.have_phase0 ?
            (state.rx_tail_tx > (uint64_t)state.phase0_tx ? state.rx_tail_tx - state.phase0_tx : 0) : s.conweave_wait;
        if (next_phase && !state.phase1)
        {
            if (!state.length)
            {
                int64_t begin = state.have_phase0 ? state.phase0_rx : c.now;
                cw_schedule(c, state, p.seq, begin + gap + s.conweave_slack);
            }
            if (state.length == s.conweave_cap)
            {
                ++state.overflow;
                return;
            }
            p.inport = ingress;
            s.conweave_fifo[state.length++] = p;
            if (state.length > state.high_water) state.high_water = state.length;
            cw_record(s, c.now, c.node, "hold", p.seq, state.length);
            return;
        }
        if (!next_phase && state.length)
            cw_schedule(c, state, p.seq, tail ? c.now + 1 : c.now + gap + s.conweave_slack);
        else if (tail)
            state.phase1 = true;
    }
    s_forward(c, ingress, p, egress, q);
}

__device__ __noinline__ void cw_flush(SCtx &c)
{
    const Sim &s = *c.s;
    ConWeaveState &state = *s.conweave;
    c.now = state.due;
    c.ord = 0;
    c.cur = state.flush_chain;
    c.cur_ok = true;
    state.phase1 = true;
    ++state.flushes;
    cw_record(s, c.now, c.node, "flush", state.length, 0);
    for (uint32_t i = 0; i < state.length; ++i)
    {
        const Pkt &p = s.conweave_fifo[i];
        int q = PG;
        const int egress = s_route(s, *c.P, c.x, p, &q);
        s_forward(c, p.inport, p, egress, q);
    }
    state.length = 0;
}
