"""Local ordering control and compact trajectory diagnostics for the 5090 experiment."""

import hashlib
import re

from crosspod import replace


def patch_shadow(tree):
    step = tree/'src/rdma/rstep.cuh'
    code = step.read_text()
    if 'lineage_quick_sink' in code:
        return
    replace(step, ' int r=ccmp_impl(s,pa,a,pb,b,inexact);', ''' int r=ccmp_impl(s,pa,a,pb,b,inexact);
#ifdef TWINS_LINEAGE_SHADOW
 // Observe each independent result in thread-private volatile storage. Neither
 // it nor a lost tail can influence ordering, failure flags, or retries here.
 volatile int lineage_full_sink = r;
 volatile bool lineage_inexact_sink = *inexact;
#ifdef TWINS_LINEAGE_DIAG
 atomicAdd(s.lc_totals + 3, 1ull);
 if (r) atomicAdd(s.lc_totals + 4, 1ull);
 if (*inexact) atomicAdd(s.lc_totals + 5, 1ull);
#endif
 *inexact = false;
 return 0;
#endif''')
    replace(step, '__device__ __forceinline__ int qcmp(const Sim &s, const Run *pa, uint64_t ha, uint32_t ra, const Run *pb, uint64_t hb, uint32_t rb)',
            '__device__ __forceinline__ int qcmp_lineage(const Sim &s, const Run *pa, uint64_t ha, uint32_t ra, const Run *pb, uint64_t hb, uint32_t rb)')
    replace(step, '// a candidate event: does (ta, a) run before (tb, b)?', '''__device__ __forceinline__ int qcmp(const Sim &s, const Run *pa, uint64_t ha, uint32_t ra,
                                   const Run *pb, uint64_t hb, uint32_t rb, uint32_t ca, uint32_t cb)
{
#ifdef TWINS_LINEAGE_SHADOW
    int r = qcmp_lineage(s, pa, ha, ra, pb, hb, rb);
    volatile int lineage_quick_sink = r;
#ifdef TWINS_LINEAGE_DIAG
    atomicAdd(s.lc_totals + 6, 1ull);
    if (r) atomicAdd(s.lc_totals + 7, 1ull);
#endif
#endif
#if defined(TWINS_LINEAGE_FIXED) || defined(TWINS_LINEAGE_SHADOW)
    // As in a quick comparison over empty chains, compare only the current
    // arrival lead. Non-arrivals have code 0. Residual ties keep local order.
    if (!ca || !cb || (ca >> CODE_ORD_BITS) == (cb >> CODE_ORD_BITS))
        return 0;
    return ca > cb ? -1 : 1;
#else
    return qcmp_lineage(s, pa, ha, ra, pb, hb, rb);
#endif
}

// a candidate event: does (ta, a) run before (tb, b)?''')
    for old, new, count in (
        ('qcmp(s, qa->r, qa->h, qa->root, qb->r, qb->h, qb->root)',
         'qcmp(s, qa->r, qa->h, qa->root, qb->r, qb->h, qb->root, code(k, d), code(bk, bd))', 1),
        ('qcmp(s, A.ap, A.ah, A.ar, B.ap, B.ah, B.ar)',
         'qcmp(s, A.ap, A.ah, A.ar, B.ap, B.ah, B.ar, A.acode, B.acode)', 4),
        ('qcmp(s, a.ap, a.ah, a.ar, me.tp, me.th, me.tr)',
         'qcmp(s, a.ap, a.ah, a.ar, me.tp, me.th, me.tr, a.acode, 0)', 1),
        ('bk ? w[best].tr : w[best].ar);',
         'bk ? w[best].tr : w[best].ar, k ? 0 : w[p].acode, bk ? 0 : w[best].acode);', 1),
        ('kj ? b.th : b.ah, kj ? b.tr : b.ar);',
         'kj ? b.th : b.ah, kj ? b.tr : b.ar, ki ? 0 : a.acode, kj ? 0 : b.acode);', 1),
    ):
        code = step.read_text()
        assert code.count(old) == count, old
        step.write_text(code.replace(old, new))
    replace(step, '    h.code = E.r[0].code;', '''#if defined(TWINS_LINEAGE_FIXED) || defined(TWINS_LINEAGE_SHADOW)
    h.code = 0; // completion has no historical lead in the controlled calendar
#else
    h.code = E.r[0].code;
#endif''')
    replace(step, '            c = j < e ? -1 : 1;', '''#if defined(TWINS_LINEAGE_FIXED) || defined(TWINS_LINEAGE_SHADOW)
            const CEvH &a = s.cev_h[s.cs[j].j], &b = s.cev_h[x.j];
            const bool earlier = a.flow != b.flow ? a.flow < b.flow
                               : a.kind != b.kind ? a.kind < b.kind
                               : a.base != b.base ? a.base < b.base : j < e;
            c = earlier ? -1 : 1;
#else
            c = j < e ? -1 : 1;
#endif''')


def add(path, anchor, lines):
    block = '#ifdef TWINS_LINEAGE_DIAG\n' + lines + '\n#endif\n'
    replace(path, anchor, block + anchor)


def patch_diagnostics(tree):
    hdr = tree/'src/rdma/rdma.h'
    if 'lc_totals' in hdr.read_text():
        return
    step = tree/'src/rdma/rstep.cuh'
    main = tree/'src/rdma/rmain.cu'
    closed = tree/'src/astra/closed.h'
    add(hdr, '    unsigned long long *ev; // counters (see main)', '''    unsigned long long *lc_links, *lc_events, *lc_flows, *lc_totals;''')
    add(step, '__device__ __forceinline__ void trace_tx', '''__host__ __device__ __forceinline__ unsigned long long lc_mix(unsigned long long h, unsigned long long v)
{
    return (h ^ v) * 1099511628211ull;
}

__device__ __forceinline__ void lc_event(unsigned long long *base, size_t index,
                                       int64_t at, unsigned event, unsigned object, const Pkt *p = nullptr)
{
    auto *row = base + index * 2;
    auto h = lc_mix(lc_mix(lc_mix(row[1], at), event), object);
    if (p)
    {
        h = lc_mix(h, p->flow);
        h = lc_mix(h, (uint64_t(p->seq) << 24) | (uint64_t(p->size) << 8) | p->kind);
        h = lc_mix(h, (uint64_t(p->src) << 48) | (uint64_t(p->dst) << 32) | (uint64_t(p->sport) << 16)
                      | (uint64_t(p->ecn) << 8) | (uint64_t(p->cnp) << 4) | p->q);
        h = lc_mix(h, (uint64_t(p->nhop) << 16) | (uint64_t(p->ord) << 8) | p->inport);
    }
    ++row[0];
    row[1] = h;
}
''')
    add(step, '    trace_tx(s, c.now, c.h, c.d, p);', '''    lc_event(s.lc_links, c.ob().link, c.now, 0, 0, &p);
    if (p.kind == K_DATA)
    {
        auto *r = s.lc_flows + 3 * size_t(p.flow);
        const unsigned long long high = (unsigned long long)p.seq + 1;
        ++r[1];
        if (high <= r[0]) ++r[2];
        else r[0] = high;
    }''')
    add(step, '    trace_tx(s, c.now, c.node, port, p);',
        '    lc_event(s.lc_links, c.w[port].ob.link, c.now, 0, 0, &p);')
    add(step, '        trace_event(h, c.now, bk == 0', '''        const unsigned object = bk == 3 ? (4u * due[di].flow + due[di].kind)
                              : bk == 4 ? sf : bk == 5 ? dyn[di_dyn] : bd;
        lc_event(s.lc_events, h, c.now, bk, object, bk == 0 ? &af[bd]->p : nullptr);''')
    add(step, '                    trace_event(c.node, c.now, "tc", o, sc_cur(c));',
        '                    lc_event(s.lc_events, s.H + s.pbase[c.x] + o, c.now, 1, o);')
    add(step, '                trace_event(c.node, c.now, "arrival", a.in, sc_cur(c));',
        '                lc_event(s.lc_events, s.H + s.pbase[c.x] + o, c.now, 0, a.in, &a.p);')
    for tag, line in (('tc', '1, p'), ('arrival', '0, p, &pk')):
        # These repeated sites are the two serialized switch agendas.
        pattern = rf'^( +)trace_event\(c.node, c.now, "{tag}", p, sc_cur\(c\)\);$'
        code = step.read_text()
        found = re.findall(pattern, code, re.M)
        assert len(found) == 2 and all(len(s) >= 16 for s in found), found
        patched = re.sub(pattern, lambda m: '#ifdef TWINS_LINEAGE_DIAG\n' + m[1] +
                         f'lc_event(s.lc_events, s.H + s.pbase[c.x], c.now, {line});\n#endif\n' + m[0],
                         code, flags=re.M)
        step.write_text(patched)
    replace(step, '    __threadfence();\n#ifdef RDMA_PROF\n    PV_ADD(0, 1);', '''    __threadfence();
#ifdef TWINS_LINEAGE_DIAG
    ++s.lc_totals[0];
    s.lc_totals[1] += nsl;
    s.lc_totals[2] = lc_mix(lc_mix(s.lc_totals[2], slot), nsl);
#endif
#ifdef RDMA_PROF
    PV_ADD(0, 1);''')
    add(main, '    s.ev = dev_alloc<unsigned long long>(40);', '''    s.lc_links = dev_alloc<unsigned long long>(size_t(L) * 2);
    s.lc_events = dev_alloc<unsigned long long>((size_t(H) + nports) * 2);
    s.lc_flows = dev_alloc<unsigned long long>(size_t(F) * 3);
    s.lc_totals = dev_alloc<unsigned long long>(8);''')
    add(main, '    printf("lineage_alloc live=', '''    {
        unsigned long long totals[8];
        CK(cudaMemcpy(totals, s.lc_totals, sizeof(totals), cudaMemcpyDeviceToHost));
        TailPool pool;
        unsigned long long tail_nodes;
        CK(hipMemcpyFromSymbol(&pool, HIP_SYMBOL(g_tail), sizeof(pool)));
        CK(cudaMemcpy(&tail_nodes, pool.cnt, sizeof(tail_nodes), cudaMemcpyDeviceToHost));
        printf("lineage_diag steps=%llu slots=%llu agenda=%llu full=%llu full_decided=%llu "
               "full_lost=%llu quick=%llu quick_decided=%llu tail_nodes=%llu\\n",
               totals[0], totals[1], totals[2], totals[3], totals[4], totals[5], totals[6], totals[7], tail_nodes);
        auto write = [&](const char *path, unsigned long long *dev, size_t count, int width) {
            vector<unsigned long long> rows(count * width);
            CK(cudaMemcpy(rows.data(), dev, rows.size() * sizeof(rows[0]), cudaMemcpyDeviceToHost));
            FILE *file = fopen(path, "w");
            if (!file) { perror(path); exit(1); }
            for (size_t i = 0; i < count; ++i)
            {
                fprintf(file, "%zu", i);
                for (int j = 0; j < width; ++j) fprintf(file, ",%llu", rows[i * width + j]);
                fputc('\\n', file);
            }
            if (fclose(file)) { perror(path); exit(1); }
        };
        write("link-events.csv", s.lc_links, L, 2);
        write("entity-events.csv", s.lc_events, size_t(H) + nports, 2);
        write("flow-sends.csv", s.lc_flows, F, 3);
    }''')
    add(closed, '    void sort_due(Sim &s, hipStream_t st, int64_t reach)',
        '''    unsigned long long lc_callback_count = 0, lc_callback_hash = 0;
    void lc_callback(int64_t at, int kind, int64_t id)
    {
        ++lc_callback_count;
        lc_callback_hash = lc_mix(lc_mix(lc_mix(lc_callback_hash, at), kind), id);
    }
''')
    add(closed, '                    fe->run_timer();', '                    lc_callback(fe->now, 2, id);')
    add(closed, '                    if (p.h.kind == 0)\n                        fe->qp_finish',
        '                    lc_callback(fe->now, p.h.kind, gfe(p.h.flow));')
    add(closed, '        printf("closed loop: %zu flows', '''        printf("lineage_callbacks count=%llu agenda=%llu\\n", lc_callback_count, lc_callback_hash);''')


def count_rows(path, width):
    values = [tuple(map(int, r.split(','))) for r in path.read_text().splitlines()]
    assert all(len(v) == width for v in values)
    assert all(v[0] == index for index, v in enumerate(values))
    return values


def receipt(work, log):
    result = {}
    for tag in ('lineage_diag', 'lineage_callbacks'):
        result[tag] = [dict((k, int(v)) for k, v in re.findall(r'(\w+)=(\d+)', row))
                       for row in re.findall(rf'^{tag} (.*)$', log, re.M)]
    for name in ('link-events.csv', 'entity-events.csv', 'flow-sends.csv'):
        p = work/name
        if p.is_file():
            rows = count_rows(p, 4 if name.startswith('flow') else 3)
            totals = [sum(col) for col in zip(*rows)]
            result[name] = dict(rows=len(rows), sha256=hashlib.sha256(p.read_bytes()).hexdigest(),
                                count=totals[2] if name.startswith('flow') else totals[1])
            if name.startswith('flow'):
                result[name]['retransmitted'] = totals[3]
    return result


def compare(out, rows, case, arms):
    a, b = [next(r for r in rows if r['case']==case and r['arm']==k) for k in arms]
    work = [out/'runs'/'diag'/case/('0-'+arm) for arm in arms]
    groups = {}
    for file in ('link-events.csv','entity-events.csv','flow-sends.csv'):
        aa, bb = [count_rows(w/file, 4 if file.startswith('flow') else 3) for w in work]
        assert len(aa)==len(bb)
        groups[file] = dict(equal=aa==bb, rows=len(aa),
                            differing=sum(x!=y for x,y in zip(aa,bb)),
                            counts=[r[file]['count'] for r in (a,b)])
        if file.startswith('link'):
            groups[file]['different_counts'] = sum(x[1]!=y[1] for x,y in zip(aa,bb))
        if file.startswith('flow'):
            groups[file]['retransmitted'] = [r[file]['retransmitted'] for r in (a,b)]
    fct_name = 'messages.fct' if case=='allgather' else 'output.fct'
    outputs = {name:a['outputs'][name]['normalized_sha256']==b['outputs'][name]['normalized_sha256']
               for name in a['outputs']}
    cols = [{int(parts[0]):tuple(map(int, parts[1:])) for line in (w/fct_name).read_text().splitlines()
             if (parts:=line.split())} for w in work]
    ac, bc = cols
    shared = ac.keys() & bc.keys()
    same_flow = {f for f in shared if ac[f][2:]==bc[f][2:]}
    delta_fct = [bc[f][1]-ac[f][1] for f in same_flow]
    delta_start = [bc[f][0]-ac[f][0] for f in same_flow]
    delta_end = [bc[f][0]+bc[f][1]-ac[f][0]-ac[f][1] for f in same_flow]
    diags = [r['lineage_diag'][-1] for r in (a,b)]
    steps = [{k:d[k] for k in ('steps','slots','agenda')} for d in diags]
    callbacks = [r['lineage_callbacks'][-1] if r['lineage_callbacks'] else None for r in (a,b)]
    return dict(same_trajectory=all(outputs.values()) and a['attempts'][-1]['done']==b['attempts'][-1]['done']
                and all(g['equal'] for g in groups.values()) and steps[0]==steps[1] and callbacks[0]==callbacks[1],
                arms=arms, outputs_equal=outputs, checks=groups, device=steps, callbacks=callbacks,
                completions=[r['attempts'][-1]['done'] for r in (a,b)],
                dataset_by_id=dict(only_a=len(ac.keys()-bc.keys()), only_b=len(bc.keys()-ac.keys()),
                                   different_endpoints_or_size=len(shared)-len(same_flow),
                                   changed_start=sum(x!=0 for x in delta_start),
                                   changed_fct=sum(x!=0 for x in delta_fct),
                                   changed_end=sum(x!=0 for x in delta_end),
                                   fct_delta_ns_range=[min(delta_fct,default=0),max(delta_fct,default=0)],
                                   end_delta_ns_range=[min(delta_end,default=0),max(delta_end,default=0)]))
