// TwiNS driver: topology (FatTree or leaf-spine), flows, worlds, slot loop, per-flow CSV in the format of the
// ns-3 comparison scenario (validation/ns3/twins-ref.cc): src,dst,flow_size,start_ns,established_ns,completed_ns
#include <algorithm>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <sstream>
#include <string>
#include <type_traits>
#include <vector>

#include "step.cuh"

using namespace nsm;

#define CHECK(x)                                                                                    \
    do                                                                                              \
    {                                                                                               \
        cudaError_t err_ = (x);                                                                     \
        if (err_ != cudaSuccess)                                                                    \
        {                                                                                           \
            fprintf(stderr, "%s:%d %s: %s\n", __FILE__, __LINE__, #x, cudaGetErrorString(err_));    \
            exit(1);                                                                                \
        }                                                                                           \
    } while (0)

constexpr int BLOCK = 32;

// The simulation's pointers and sizes, in constant memory: a by-value kernel argument whose
// address is taken (every step function holds a Sim reference) is copied to scratch memory,
// adding a slow dependent load to every state access.
__constant__ Sim c_sim;

__global__ void ns_slot(Due *due_buf, int64_t *sw_ts, uint32_t *sw_ref)
{
    const Sim &s = c_sim;
    if (*s.finished)
        return;
    int64_t t = *s.slot;
    __shared__ int rows[BLOCK * 33];
    __shared__ int base[BLOCK], cnt[BLOCK];
    __shared__ int in_win[BLOCK * 3];
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
            if (fd >= (unsigned int)s.F || t + 1 >= s.max_slots)
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

template <typename T>
static T *dalloc(size_t n)
{
    T *p;
    CHECK(cudaMalloc(&p, sizeof(T) * std::max<size_t>(n, 1)));
    CHECK(cudaMemset(p, 0, sizeof(T) * std::max<size_t>(n, 1)));
    return p;
}

template <typename T>
static T *dupload(const std::vector<T> &v)
{
    T *p = dalloc<T>(v.size());
    if (!v.empty())
        CHECK(cudaMemcpy(p, v.data(), sizeof(T) * v.size(), cudaMemcpyHostToDevice));
    return p;
}

template <typename T>
static std::vector<T> ddown(const T *p, size_t n)
{
    std::vector<T> v(n);
    if (n)
        CHECK(cudaMemcpy(v.data(), p, sizeof(T) * n, cudaMemcpyDeviceToHost));
    return v;
}

struct FlowIn
{
    int src, dst;
    int64_t size, start_ns;
};

static std::vector<FlowIn> load_flows(const std::string &path, int H)
{
    std::vector<FlowIn> out;
    std::ifstream in(path);
    if (!in)
    {
        fprintf(stderr, "cannot open %s\n", path.c_str());
        exit(1);
    }
    std::string line;
    while (std::getline(in, line))
    {
        if (line.empty() || !isdigit(line[0]))
            continue;
        std::stringstream ss(line);
        std::string f;
        std::vector<int64_t> v;
        while (std::getline(ss, f, ','))
            v.push_back(std::stoll(f));
        if (v.size() == 3)
            out.push_back({(int)v[0], (int)((v[0] + H / 2) % H), v[1], v[2]});
        else if (v.size() == 4)
            out.push_back({(int)v[0], (int)v[1], v[2], v[3]});
    }
    return out;
}

int main(int argc, char **argv)
{
    int k = 4, graph_k = 64;
    std::string flow_file, output = "fct.csv", tcp = "dctcp", ecmp_from, qtrace_file;
    long long qtrace_until = 0, qtrace_from = 0;
    int host_lanes = 0; // 0: chosen from the host count
    int ooo_segs = 0;   // per-flow caps; 0: default
    int rtt_ring = 0;
    std::string topo = "fattree", worlds_file;
    int ecn_k = RED_K, queue_pkts = QDISC_CAP;
    int leaves = 16, spines = 8, hpl = 16;
    int64_t max_slots = 2000000;
    for (int i = 1; i < argc; i++)
    {
        std::string a = argv[i];
        auto val = [&](const char *p) { return a.substr(strlen(p)); };
        if (a.rfind("--k=", 0) == 0)
            k = std::stoi(val("--k="));
        else if (a.rfind("--flows=", 0) == 0)
            flow_file = val("--flows=");
        else if (a.rfind("--out=", 0) == 0)
            output = val("--out=");
        else if (a.rfind("--tcp=", 0) == 0)
            tcp = val("--tcp=");
        else if (a.rfind("--max_slots=", 0) == 0)
            max_slots = std::stoll(val("--max_slots="));
        else if (a.rfind("--qtrace=", 0) == 0)
            qtrace_file = val("--qtrace=");
        else if (a.rfind("--qtrace_from=", 0) == 0)
            qtrace_from = std::stoll(val("--qtrace_from="));
        else if (a.rfind("--qtrace_until=", 0) == 0)
            qtrace_until = std::stoll(val("--qtrace_until="));
        else if (a.rfind("--ecmp_from=", 0) == 0)
            ecmp_from = val("--ecmp_from=");
        else if (a.rfind("--rtt_ring=", 0) == 0)
            rtt_ring = std::stoi(val("--rtt_ring="));
        else if (a.rfind("--ooo_segs=", 0) == 0)
            ooo_segs = std::stoi(val("--ooo_segs="));
        else if (a.rfind("--host_lanes=", 0) == 0)
            host_lanes = std::stoi(val("--host_lanes="));
        else if (a.rfind("--worlds=", 0) == 0)
            worlds_file = val("--worlds=");
        else if (a.rfind("--ecnK=", 0) == 0)
            ecn_k = std::stoi(val("--ecnK="));
        else if (a.rfind("--queue=", 0) == 0)
            queue_pkts = std::stoi(val("--queue="));
        else if (a.rfind("--topo=", 0) == 0)
            topo = val("--topo=");
        else if (a.rfind("--leaves=", 0) == 0)
            leaves = std::stoi(val("--leaves="));
        else if (a.rfind("--spines=", 0) == 0)
            spines = std::stoi(val("--spines="));
        else if (a.rfind("--hpl=", 0) == 0)
            hpl = std::stoi(val("--hpl="));
        else if (a.rfind("--graph_k=", 0) == 0)
            graph_k = std::stoi(val("--graph_k="));
        else
        {
            fprintf(stderr, "unknown argument %s\n", a.c_str());
            return 1;
        }
    }
    bool ls = topo == "leafspine";
    bool bad_topo = ls ? (leaves < 1 || spines < 1 || hpl < 1 || std::max(hpl + spines, leaves) > BLOCK || leaves * hpl > 65535)
                       : (topo != "fattree" || k < 4 || k > 32 || (k & (k - 1)));
    if ((flow_file.empty() && worlds_file.empty()) || bad_topo || (tcp != "dctcp" && tcp != "newreno" && tcp != "cubic"))
    {
        fprintf(stderr, "usage: twins (--k=<4..32> | --topo=leafspine --leaves=L --spines=S --hpl=N)\n"
                        "         (--flows=<csv> [--out=csv] [--tcp=dctcp|newreno|cubic] [--ecnK=30] [--queue=200] [--ecmp_from=csv]\n"
                        "          | --worlds=<file>: one world per line, flows=..,tcp=..,ecnK=..,queue=..,ecmp_from=..,out=..)\n"
                        "         leaf-spine: max(N+S, L) <= %d\n", BLOCK);
        return 1;
    }
    Sim s;
    memset(&s, 0, sizeof(s));
    // ---- worlds: independent simulations run side by side as disjoint copies of the topology
    struct World
    {
        std::string flows, ecmp_from, out;
        uint8_t cc = CC_DCTCP;
        int red_k = RED_K, qcap = QDISC_CAP;
    };
    auto cc_of = [](const std::string &t) -> int { return t == "dctcp" ? CC_DCTCP : t == "newreno" ? CC_NEWRENO : t == "cubic" ? CC_CUBIC : -1; };
    std::vector<World> worlds;
    if (worlds_file.empty())
        worlds.push_back({flow_file, ecmp_from, output, (uint8_t)cc_of(tcp), ecn_k, queue_pkts});
    else
    {
        std::ifstream in(worlds_file);
        std::string line;
        while (std::getline(in, line))
        {
            if (line.empty() || line[0] == '#')
                continue;
            World w;
            w.cc = (uint8_t)cc_of(tcp);
            w.red_k = ecn_k;
            w.qcap = queue_pkts;
            std::stringstream ss(line);
            std::string kv;
            while (std::getline(ss, kv, ','))
            {
                size_t e = kv.find('=');
                std::string key = kv.substr(0, e), v = e == std::string::npos ? "" : kv.substr(e + 1);
                if (key == "flows")
                    w.flows = v;
                else if (key == "tcp" && cc_of(v) >= 0)
                    w.cc = (uint8_t)cc_of(v);
                else if (key == "ecnK")
                    w.red_k = std::stoi(v);
                else if (key == "queue")
                    w.qcap = std::stoi(v);
                else if (key == "ecmp_from")
                    w.ecmp_from = v;
                else if (key == "out")
                    w.out = v;
                else
                {
                    fprintf(stderr, "%s: bad entry %s\n", worlds_file.c_str(), kv.c_str());
                    return 1;
                }
            }
            if (w.flows.empty() || w.out.empty())
            {
                fprintf(stderr, "%s: every world needs flows= and out=\n", worlds_file.c_str());
                return 1;
            }
            worlds.push_back(w);
        }
    }
    const int W = (int)worlds.size();
    if (ls)
    {
        k = std::max(hpl + spines, leaves); // ports per switch (unused ones stay idle)
        s.topo = TOPO_LEAFSPINE;
        s.leaves = leaves;
        s.spines = spines;
        s.hpl = hpl;
        s.H1 = leaves * hpl;
        s.S1 = leaves + spines;
        s.ecmp_n = spines;
    }
    else
    {
        s.topo = TOPO_FATTREE;
        s.H1 = k * k * k / 4;
        s.S1 = k * k + k * k / 4;
        s.ecmp_n = k / 2;
    }
    s.H = W * s.H1;
    s.S = W * s.S1;
    if (s.H > 65535)
    {
        fprintf(stderr, "%d worlds of %d hosts exceed 65535 hosts\n", W, s.H1);
        return 1;
    }
    int hk = k / 2;
    s.k = k;
    s.hk = hk;
    s.L = s.H + s.S * k;
    // hosts per wave: one while waves are plentiful (no divergence between hosts), more once
    // the GPU is full (measured on an RX 9060 XT, 32 CUs)
    if (host_lanes <= 0)
        host_lanes = s.H <= 128 ? 1 : s.H / 128;
    s.host_lanes = std::max(1, std::min(host_lanes, BLOCK));
    s.host_blocks = (s.H + s.host_lanes - 1) / s.host_lanes;
    s.sw_per_block = BLOCK / k;
    s.max_slots = max_slots;
    const int H = s.H, S = s.S, E = k * hk, H1 = s.H1, S1 = s.S1;

    // ---- flows (sorted by (src, start) for the per-host start queues)
    std::vector<FlowIn> flows;
    std::vector<int> wfirst(W + 1);
    for (int w = 0; w < W; w++)
    {
        wfirst[w] = (int)flows.size();
        for (FlowIn f : load_flows(worlds[w].flows, H1))
        {
            f.src += w * H1;
            f.dst += w * H1;
            flows.push_back(f);
        }
    }
    wfirst[W] = (int)flows.size();
    int F = (int)flows.size();
    s.F = F;
    std::vector<int> order(F);
    for (int i = 0; i < F; i++)
        order[i] = i;
    std::stable_sort(order.begin(), order.end(), [&](int a, int b) {
        return flows[a].src != flows[b].src ? flows[a].src < flows[b].src : flows[a].start_ns < flows[b].start_ns;
    });
    std::vector<int> pend_next(H, 0), pend_end(H, 0);
    {
        int i = 0;
        for (int h = 0; h < H; h++)
        {
            pend_next[h] = i;
            while (i < F && flows[order[i]].src == h)
                i++;
            pend_end[h] = i;
        }
    }
    // ---- flow state
    std::vector<FlowRec> recs(F);
    for (auto &r : recs)
    {
#define NS_INIT(T, n, v) r.n = (T)(v);
        NS_FLOW_FIELDS(NS_INIT)
#undef NS_INIT
    }
    {
        for (int i = 0; i < F; i++)
        {
            recs[i].src = flows[i].src;
            recs[i].dst = flows[i].dst;
            recs[i].size = (uint32_t)flows[i].size;
            recs[i].start = flows[i].start_ns * 1000;
        }
        // ECMP hashes: from an ns-3 run of the same flows (identical paths), or a hash of the
        // flow id and direction (statistically equivalent)
        auto mix = [](uint32_t x) {
            x ^= x >> 16;
            x *= 0x7feb352du;
            x ^= x >> 15;
            x *= 0x846ca68bu;
            x ^= x >> 16;
            return x;
        };
        for (int i = 0; i < F; i++)
        {
            recs[i].hash_data = mix(2u * i + 1);
            recs[i].hash_ack = mix(2u * i + 2);
        }
        for (int w = 0; w < W; w++)
        {
            const std::string &ecmp_from = worlds[w].ecmp_from;
            if (ecmp_from.empty())
                continue;
            std::ifstream in(ecmp_from);
            std::string line;
            std::getline(in, line);
            int i = wfirst[w], F = wfirst[w + 1];
            while (std::getline(in, line) && i < F)
            {
                std::stringstream ss(line);
                std::string f;
                std::vector<std::string> v;
                while (std::getline(ss, f, ','))
                    v.push_back(f);
                if (v.size() < 8)
                {
                    fprintf(stderr, "%s has no hash columns\n", ecmp_from.c_str());
                    exit(1);
                }
                recs[i].hash_data = (uint32_t)std::stoul(v[6]);
                recs[i].hash_ack = (uint32_t)std::stoul(v[7]);
                i++;
            }
            if (i != F)
            {
                fprintf(stderr, "%s: %d rows for %d flows\n", ecmp_from.c_str(), i - wfirst[w], F - wfirst[w]);
                exit(1);
            }
        }
    }
    s.fl.rec = dupload(recs);
#define NS_BASE(T, n, v) s.fl.n.base = reinterpret_cast<char *>(s.fl.rec) + offsetof(FlowRec, n);
    NS_FLOW_FIELDS(NS_BASE)
#undef NS_BASE
    // out-of-order window and RTT history, per flow: min(the flow's segments, a cap). A flow with
    // at most cap segments cannot overflow either; longer flows overflow only with more than cap
    // segments in flight (counted as ooo-ovf, rtt-ovf)
    if (ooo_segs <= 0)
        ooo_segs = 65536;
    if (rtt_ring <= 0)
        rtt_ring = 65536;
    {
        std::vector<uint64_t> hoff(F), ooff(F);
        std::vector<uint32_t> hn(F), ow(F);
        uint64_t ht = 0, ot = 0;
        for (int i = 0; i < F; i++)
        {
            uint64_t segs = (recs[i].size + MSS - 1) / MSS + 1;
            hn[i] = (uint32_t)std::min<uint64_t>(segs, rtt_ring);
            ow[i] = (uint32_t)((std::min<uint64_t>(segs, ooo_segs) + 31) / 32);
            hoff[i] = ht;
            ooff[i] = ot;
            ht += hn[i];
            ot += ow[i];
        }
        s.fl.hist_off = dupload(hoff);
        s.fl.hist_n = dupload(hn);
        s.fl.ooo_off = dupload(ooff);
        s.fl.ooo_w = dupload(ow);
        s.fl.hist_t = dalloc<int64_t>(ht);
        s.fl.hist_tag = dalloc<uint32_t>(ht);
        s.fl.ooo_bm = dalloc<uint32_t>(ot);
    }
    s.fl.dchain = dalloc<uint16_t>((size_t)F * CHAIN);
    s.fl.rchain = dalloc<uint16_t>((size_t)F * CHAIN);
    // ---- hosts and ports
    s.ring = 0;
    for (const World &w : worlds)
        s.ring = std::max(s.ring, w.qcap + 2);
    s.hs.ring = dalloc<Frame>((size_t)H * s.ring);
    s.hs.q_head = dalloc<int>(H);
    s.hs.q_size = dalloc<int>(H);
    s.hs.link_free = dalloc<int64_t>(H);
    s.hs.last_start = dalloc<int64_t>(H);
    s.hs.last_chain = dalloc<uint16_t>((size_t)H * CHAIN);
    s.hs.act_snd = dalloc<int>((size_t)H * MAX_ACTIVE);
    s.hs.n_snd = dalloc<int>(H);
    s.hs.act_rcv = dalloc<int>((size_t)H * MAX_ACTIVE);
    s.hs.n_rcv = dalloc<int>(H);
    s.hs.pend_next = dupload(pend_next);
    s.hs.pend_end = dupload(pend_end);
    s.hs.pend_flow = dupload(order);
    s.hs.wake = dalloc<int64_t>(H); // 0: every host runs its first slot
    {
        std::vector<uint8_t> cc(H);
        std::vector<int> hk_(H), hq(H), pk((size_t)S * k), pq((size_t)S * k);
        for (int h = 0; h < H; h++)
        {
            cc[h] = worlds[h / H1].cc;
            hk_[h] = worlds[h / H1].red_k;
            hq[h] = worlds[h / H1].qcap;
        }
        for (size_t p = 0; p < (size_t)S * k; p++)
        {
            pk[p] = worlds[p / k / S1].red_k;
            pq[p] = worlds[p / k / S1].qcap;
        }
        s.hs.cc = dupload(cc);
        s.hs.red_k = dupload(hk_);
        s.hs.qcap = dupload(hq);
        s.ps.red_k = dupload(pk);
        s.ps.qcap = dupload(pq);
    }
    s.ps.ring = dalloc<Frame>((size_t)S * k * s.ring);
    s.ps.q_head = dalloc<int>((size_t)S * k);
    s.ps.q_size = dalloc<int>((size_t)S * k);
    s.ps.link_free = dalloc<int64_t>((size_t)S * k);
    s.ps.last_start = dalloc<int64_t>((size_t)S * k);
    s.ps.last_chain = dalloc<uint16_t>((size_t)S * k * CHAIN);
    // ---- links: host h sends on link h; switch sw port p sends on link H + sw*k + p
    auto sw_link = [&](int sw, int p) { return H + sw * k + p; };
    std::vector<int> host_in(H), sw_in((size_t)S * k, -1);
    for (int w = 0; w < W; w++)
    {
        const int hb = w * H1, sb = w * S1; // the world's first host and switch
        if (ls)
        {
            for (int h = 0; h < H1; h++)
            {
                int l = h / hpl, p = h % hpl;
                host_in[hb + h] = sw_link(sb + l, p);
                sw_in[(size_t)(sb + l) * k + p] = hb + h;
            }
            for (int l = 0; l < leaves; l++)
                for (int j = 0; j < spines; j++)
                {
                    int sp = sb + leaves + j;
                    sw_in[(size_t)(sb + l) * k + hpl + j] = sw_link(sp, l);
                    sw_in[(size_t)sp * k + l] = sw_link(sb + l, hpl + j);
                }
            continue;
        }
        for (int h = 0; h < H1; h++)
        {
            int e = sb + h / hk, p = h % hk;
            host_in[hb + h] = sw_link(e, p);
            sw_in[(size_t)e * k + p] = hb + h;
        }
        for (int pod = 0; pod < k; pod++)
            for (int j = 0; j < hk; j++)
            {
                int agg = sb + E + pod * hk + j;
                for (int x = 0; x < hk; x++)
                {
                    int e2 = sb + pod * hk + x;
                    sw_in[(size_t)agg * k + x] = sw_link(e2, j + hk);
                    sw_in[(size_t)e2 * k + j + hk] = sw_link(agg, x);
                }
            }
        for (int i = 0; i < hk; i++)
            for (int j = 0; j < hk; j++)
            {
                int core = sb + k * k + i * hk + j;
                for (int pod = 0; pod < k; pod++)
                {
                    int agg = sb + E + pod * hk + i;
                    sw_in[(size_t)core * k + pod] = sw_link(agg, j + hk);
                    sw_in[(size_t)agg * k + j + hk] = sw_link(core, pod);
                }
            }
    }
    s.host_in_link = dupload(host_in);
    s.sw_in_link = dupload(sw_in);
    s.msg = dalloc<Frame>((size_t)NB * s.L * MAX_TX);
    s.msg_cnt = dalloc<long long>((size_t)NB * s.L);
    {
        std::vector<long long> init((size_t)NB * s.L, -(1ll << 16)); // no slot
        CHECK(cudaMemcpy(s.msg_cnt, init.data(), 8 * init.size(), cudaMemcpyHostToDevice));
    }
    s.activity = dalloc<unsigned int>(2);
    s.next_wake = dalloc<unsigned long long>(1);
    {
        unsigned long long m = ~0ull;
        CHECK(cudaMemcpy(s.next_wake, &m, 8, cudaMemcpyHostToDevice));
        unsigned int a[2] = {1, 1}; // no skip before the first slots have run
        CHECK(cudaMemcpy(s.activity, a, 8, cudaMemcpyHostToDevice));
    }
    s.slot = dalloc<int64_t>(1);
    s.done_blocks = dalloc<unsigned int>(1);
    s.flows_done = dalloc<unsigned int>(1);
    s.finished = dalloc<int>(1);
    s.error = dalloc<int>(1);
    s.ev = dalloc<unsigned long long>(20);
    s.qtrace = nullptr;
    s.qtrace_cnt = dalloc<unsigned long long>(1);
    s.qtrace_n = 0;
    s.qtrace_until = qtrace_until;
    s.qtrace_from = qtrace_from;
    if (!qtrace_file.empty())
    {
        s.qtrace_n = 20000000;
        s.qtrace = dalloc<QRec>(s.qtrace_n);
    }
    int *h_finished;
    CHECK(cudaHostAlloc((void **)&h_finished, sizeof(int), cudaHostAllocMapped));
    *h_finished = 0;
    CHECK(cudaHostGetDevicePointer((void **)&s.finished_host, h_finished, 0));
    if (F == 0)
        *h_finished = 1;
    Due *due = dalloc<Due>((size_t)H * MAX_DUE);
    size_t per_sw = (size_t)k * 2 * MAX_TX;
    int64_t *sw_ts = dalloc<int64_t>((size_t)S * per_sw);
    uint32_t *sw_ref = dalloc<uint32_t>((size_t)S * per_sw);

    // ---- run
    int grid = s.host_blocks + (S + s.sw_per_block - 1) / s.sw_per_block;
#if defined(TWINS_USE_HIP)
    CHECK(hipMemcpyToSymbol(HIP_SYMBOL(c_sim), &s, sizeof(Sim)));
#else
    CHECK(cudaMemcpyToSymbol(c_sim, &s, sizeof(Sim)));
#endif
    cudaStream_t stream;
    CHECK(cudaStreamCreate(&stream));
    cudaGraph_t graph;
    cudaGraphExec_t exec;
    CHECK(cudaStreamBeginCapture(stream, cudaStreamCaptureModeGlobal));
    for (int i = 0; i < graph_k; i++)
        ns_slot<<<grid, BLOCK, 0, stream>>>(due, sw_ts, sw_ref);
    CHECK(cudaStreamEndCapture(stream, &graph));
    CHECK(cudaGraphInstantiate(&exec, graph, 0));
    CHECK(cudaDeviceSynchronize());
    auto t0 = std::chrono::high_resolution_clock::now();
    while (!*(volatile int *)h_finished)
    {
        CHECK(cudaGraphLaunch(exec, stream));
        CHECK(cudaStreamSynchronize(stream));
    }
    double sec = std::chrono::duration<double>(std::chrono::high_resolution_clock::now() - t0).count();
    int64_t slots;
    CHECK(cudaMemcpy(&slots, s.slot, 8, cudaMemcpyDeviceToHost));
    unsigned int fd;
    CHECK(cudaMemcpy(&fd, s.flows_done, 4, cudaMemcpyDeviceToHost));
    std::vector<unsigned long long> ev = ddown(s.ev, 20);
#ifdef NS_PROF
    {
        int khz = 0;
        hipDeviceGetAttribute(&khz, hipDeviceAttributeWallClockRate, 0);
        double rate = khz / 1000.0; // ticks per us
        printf("prof: per slot, slowest host block %.1f us, slowest switch block %.1f us\n", ev[17] / (double)rate / slots,
               ev[18] / (double)rate / slots);
    }
#endif
    printf("Simulation Duration: %.6fs\n", sec);
    printf("Executed Slots: %lld\n", (long long)slots);
    printf("flows %d completed %u wall %.4f s drops %llu marks %llu retx %llu rto %llu | tx-cap %llu active-ovf %llu ooo-ovf %llu due-ovf %llu"
           " | retx by state open %llu disorder %llu cwr %llu recovery %llu loss %llu | max-inflight %llu segs rtt-ovf %llu ring-ovf %llu\n",
           F, fd, sec, ev[0], ev[1], ev[2], ev[3], ev[4], ev[5], ev[6], ev[7], ev[8], ev[9], ev[10], ev[11], ev[12], ev[13], ev[14], ev[19]);
    if (ev[5] || ev[6] || ev[7] || ev[14] || ev[19])
        fprintf(stderr, "warning: a capacity overflowed (active/ooo/due/rtt/ring-ovf); the results may differ from ns-3\n");

    if (s.qtrace)
    {
        unsigned long long n;
        CHECK(cudaMemcpy(&n, s.qtrace_cnt, 8, cudaMemcpyDeviceToHost));
        n = std::min(n, s.qtrace_n);
        std::vector<QRec> q = ddown(s.qtrace, n);
        std::stable_sort(q.begin(), q.end(), [](const QRec &a, const QRec &b) {
            if (a.ts != b.ts)
                return a.ts < b.ts;
            if (a.node != b.node)
                return a.node < b.node;
            return a.dev < b.dev;
        });
        FILE *o = fopen(qtrace_file.c_str(), "w");
        for (const QRec &r : q)
        {
            const char *kind = (r.flags & F_DATA) ? "D" : (r.flags & F_SYN) ? ((r.flags & F_ACK) && r.len == SYN_FRAME ? "SA" : "S") : "A";
            if ((r.flags & F_SYN) && (r.flags & F_ACK))
                kind = "SA";
            fprintf(o, "%lld %d %d %d %d %s %u %d\n", (long long)r.ts, r.node, r.dev, r.src, r.dst, kind, r.sa, r.qlen);
        }
        fclose(o);
        printf("qtrace: %llu records\n", n);
    }
    std::vector<FlowRec> out_rec = ddown(s.fl.rec, F);
    auto field = [&](auto member) {
        std::vector<std::remove_reference_t<decltype(out_rec[0].*member)>> v(F);
        for (int i = 0; i < F; i++)
            v[i] = out_rec[i].*member;
        return v;
    };
    std::vector<int64_t> est = field(&FlowRec::est), done = field(&FlowRec::done);
    if (fd < (unsigned)F)
    {
        auto st = field(&FlowRec::st), cong = field(&FlowRec::cong);
        auto una = field(&FlowRec::una), nxt = field(&FlowRec::nxt), high = field(&FlowRec::high), cwnd = field(&FlowRec::cwnd);
        auto ssth = field(&FlowRec::ssthresh), rcv = field(&FlowRec::rcv_nxt), dup = field(&FlowRec::dupacks), sk = field(&FlowRec::sacked);
        auto rto_at = field(&FlowRec::rto_at);
        auto rec = field(&FlowRec::recover), size = field(&FlowRec::size);
        auto nsnd = ddown(s.hs.n_snd, H), nrcv = ddown(s.hs.n_rcv, H);
        int shown = 0;
        for (int i = 0; i < F && shown < 6; i++)
            if (done[i] < 0)
            {
                printf("stuck flow %d %d->%d size %u st %d cong %d una %u nxt %u high %u cwnd %u ssth %u dup %u sacked %u recover %u rto_at %lld rcv_nxt %u | n_snd %d n_rcv %d\n",
                       i, flows[i].src, flows[i].dst, size[i], st[i], cong[i], una[i], nxt[i], high[i], cwnd[i], ssth[i], dup[i], sk[i],
                       rec[i], (long long)rto_at[i], rcv[i], nsnd[flows[i].src], nrcv[flows[i].dst]);
                shown++;
            }
    }
    for (int w = 0; w < W; w++)
    {
        std::ofstream o(worlds[w].out);
        o << "src,dst,flow_size,start_ns,established_ns,completed_ns\n";
        for (int i = wfirst[w]; i < wfirst[w + 1]; i++)
            o << flows[i].src - w * H1 << "," << flows[i].dst - w * H1 << "," << flows[i].size << "," << flows[i].start_ns << ","
              << (est[i] >= 0 ? est[i] / 1000 : -1) << "," << (done[i] >= 0 ? done[i] / 1000 : -1) << "\n";
    }
    if (W > 1)
        printf("worlds %d\n", W);
    return 0;
}
