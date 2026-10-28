// twins-rdma: runs TwiNS's RDMA model (see rdma.h) on a topology file and a flow file, with the
// parameters of an HPCC config file, like validation/hpcc/rdma-ref.cc:
//
//   twins-rdma CONFIG --topo=F --flows=F --out=F [--txtrace=F --from=ns --until=ns]
//              [--rate=100Gbps --delay=1000] [--q3cap=N] [--tail=log2] [--serial] [KEY=VALUE ...]
//   twins-rdma CONFIG --batch=JOBFILE --outdir=DIR [--path=DIR:DIR...] [options] [KEY=VALUE ...]
//
// --batch: every line "name topo flows [KEY=VALUE ...]" of the job file is a world of its own
// (its overrides on top of CONFIG's and the command line's), run side by side with the others;
// world name's results go to DIR/name.fct. The worlds share the slot (the shortest link delay).
// --q3cap: frames a switch port's queue holds (default 4096; an overflow is reported)
// --win: the most slots a block runs in one launch (default 1: one slot per launch; windows rarely
// run in busy networks, and checking for them cost more than they saved)
// --rank: wide switches' simultaneous arrivals are ranked this many slots ahead (default 2; 0: off)
// --persist=0: always a graph of launches per slot (default: one persistent launch for many slots
// when every block fits on the GPU at once)
// (--rank-every=G: one ranking every G launches, of G slots, default 8; --rank-join=M: it joins M
// launches later, default 1; --rank-bs: its block size, default 512)
//
// --tail: the ring of chain tails holds 2^log2 nodes (default 24, 400 MB); ties decided on an
// overwritten tail are reported as "on lost tails"
//
// out: "id start_ns fct_ns" per flow, by id.
// Exit status: 0 every flow done; 2 some unfinished at the stop time; 3 an error counter (an
// overflow, a late event: the results are not exact).
// --sent=F (SimAI topologies): "id ns" per flow at its first SendComplete, by id.
//
// Topologies: TwiNS's ("hosts H", "switches S", "link A B"; --rate and --delay for every link), or
// SimAI's (gen_Topo_Template.py: "nodes gpus_per_server nvswitches switches links gpu_type", the
// NVSwitch ids, the switch ids, then "src dst rate delay error_rate" per link). A SimAI topology
// selects ns-3-alibabacloud's behaviour (Params::simai; reference validation/simai/simai-ref.cc),
// and gives every link its own rate and delay.
#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <functional>
#include <map>
#include <sstream>
#include <string>
#include <vector>
#include "rstep.cuh"

using namespace rdma;
using std::string;
using std::vector;

#define CK(x)                                                                                       \
    do                                                                                              \
    {                                                                                               \
        cudaError_t e_ = (x);                                                                       \
        if (e_ != cudaSuccess)                                                                      \
        {                                                                                           \
            fprintf(stderr, "%s:%d: %s\n", __FILE__, __LINE__, cudaGetErrorString(e_));             \
            exit(1);                                                                                \
        }                                                                                           \
    } while (0)

template <typename T>
static T *dev_alloc(size_t n)
{
    T *p = nullptr;
    CK(cudaMalloc((void **)&p, std::max<size_t>(n, 1) * sizeof(T)));
    CK(cudaMemset(p, 0, std::max<size_t>(n, 1) * sizeof(T)));
    return p;
}
template <typename T>
static T *dev_copy(const vector<T> &v)
{
    T *p = dev_alloc<T>(v.size());
    if (!v.empty())
        CK(cudaMemcpy(p, v.data(), v.size() * sizeof(T), cudaMemcpyHostToDevice));
    return p;
}

// ---------------------------------------------------------------- configuration (rdma-ref's)
typedef std::map<string, vector<string>> Config;
struct Cfg
{
    const Config &c;
    bool has(const char *k) const { return c.count(k) && !c.at(k).empty(); }
    string str(const char *k, const string &d) const { return has(k) ? c.at(k)[0] : d; }
    double num(const char *k, double d) const { return has(k) ? atof(c.at(k)[0].c_str()) : d; }
    uint32_t u32(const char *k, uint32_t d) const { return has(k) ? (uint32_t)strtoul(c.at(k)[0].c_str(), 0, 10) : d; }
};

static bool read_config(const string &path, Config &cfg)
{
    std::ifstream f(path);
    if (!f)
    {
        fprintf(stderr, "cannot open %s\n", path.c_str());
        return false;
    }
    string line;
    while (std::getline(f, line))
    {
        std::istringstream is(line);
        string key, v;
        if (!(is >> key))
            continue;
        vector<string> vals;
        while (is >> v)
            vals.push_back(v);
        cfg[key] = vals;
    }
    return true;
}

// KEY=VALUE, or KEY=V1,V2,... for a key with several values (KMAX_MAP=1,100000000000,800)
static void set_override(Config &cfg, const string &k, const string &v)
{
    vector<string> vals;
    size_t b = 0;
    for (size_t c; (c = v.find(',', b)) != string::npos; b = c + 1)
        vals.push_back(v.substr(b, c - b));
    vals.push_back(v.substr(b));
    cfg[k] = vals;
}

// ns-3 DataRate strings: a number and a unit (bps, Kbps, Mbps, Gbps; b/s, Kb/s, Mb/s, Gb/s)
static uint64_t parse_rate(const string &s)
{
    size_t i = 0;
    while (i < s.size() && (isdigit((unsigned char)s[i]) || s[i] == '.'))
        i++;
    double v = atof(s.substr(0, i).c_str());
    string u = s.substr(i);
    double m = 1;
    if (u[0] == 'K' || u[0] == 'k')
        m = 1e3;
    else if (u[0] == 'M')
        m = 1e6;
    else if (u[0] == 'G')
        m = 1e9;
    return (uint64_t)(v * m);
}

// ns-3 Time strings as SimAI writes them: a number and a unit (ms, us, ns, s)
static int64_t parse_delay(const string &s)
{
    size_t i = 0;
    while (i < s.size() && (isdigit((unsigned char)s[i]) || s[i] == '.'))
        i++;
    double v = atof(s.substr(0, i).c_str());
    string u = s.substr(i);
    double m = u == "s" ? 1e9 : u == "ms" ? 1e6 : u == "us" ? 1e3 : 1;
    return (int64_t)llround(v * m);
}

// the first link's rate and delay of a SimAI topology file (0 for TwiNS's format)
static bool simai_link0(const string &path, uint64_t *bps, int64_t *delay)
{
    std::ifstream f(path);
    string x;
    if (!(f >> x) || x.empty() || !isdigit((unsigned char)x[0]))
        return false;
    int nodes = atoi(x.c_str()), gps, nvs, sws, nl;
    string gpu_type;
    f >> gps >> nvs >> sws >> nl >> gpu_type;
    for (int i = 0; i < nvs + sws; i++)
        f >> x;
    int u, v;
    string r, d;
    if (!(f >> u >> v >> r >> d))
        return false;
    *bps = parse_rate(r);
    *delay = parse_delay(d);
    (void)nodes;
    return true;
}

static void host_push(Chain *c, int64_t lead, int ord) { cpush(c, ccode(lead, ord), Chain(*c)); }

// One network: its parameters, topology, routes and flows, numbered within it.
struct World
{
    string name;
    Params P;
    int H = 0, S = 0, N = 0, L = 0, K = 1;
    vector<int> deg, out_link, in_link, link_dst, link_dport; // [N K] with the world's K
    vector<Route> rt;                                         // [S H]
    vector<uint8_t> rtp;                                      // their ports
    vector<FlowInfo> fi;
    vector<int> start_list, start_off;
    vector<uint64_t> lbps;  // [L] per directed link
    vector<int64_t> ldelay; // [L]
    vector<PortCfg> pcfg;   // its switches' ports in turn
    vector<uint8_t> nvs;    // [S] an NVSwitch
    // DAG worlds: children (child << 1 | kind) per flow, dependencies per flow, host pairs' devices
    // and port counters ([src H + dst])
    bool dag = false;
    vector<int> ch_off, ndeps;
    vector<uint32_t> ch_list;
    vector<PairNic> pnic;
    vector<uint16_t> pcnt;
    // traced DAGs: timers' delays, each cause's actions (2 flow + kind, then 2 F + timer), the flows
    // main's actions install (by themselves or through timers: ready at the start, with their start
    // times and chains), and the roots' chains (main's actions in uid order)
    bool traced = false;
    vector<int64_t> tdelay;
    vector<vector<DAct>> acts;
    struct Pre
    {
        int flow;
        int64_t ts;
        Chain c;
    };
    vector<Pre> pre;
    vector<Chain> troot;
    double stop_ns = 0;     // SIMULATOR_STOP_TIME
};

// a traced DAG (docs/ai/dag.md): its actions by cause; main's, and their timers', expanded here
struct TracedOn
{
    string child, cause;
    unsigned long long ord;
};
static bool traced_dag(World &w, const vector<TracedOn> &ons, bool deps, int64_t send_lat)
{
    const int F = w.fi.size(), T = w.tdelay.size();
    bool roots = false;
    for (auto &x : w.fi)
        roots |= !x.dyn;
    if (deps || roots)
    {
        fprintf(stderr, "%s: a traced DAG (\"on\") has no \"dep\" or \"root\"\n", w.name.c_str());
        return false;
    }
    w.traced = true;
    w.acts.assign(2 * F + T, {});
    vector<std::pair<unsigned long long, uint32_t>> mains;
    vector<char> seen(F + T, 0);
    for (auto &o : ons)
    {
        int id = atoi(o.child.c_str() + 1), cid = atoi(o.cause.c_str() + 1);
        bool tm = o.child[0] == 't';
        if ((o.child[0] != 'f' && !tm) || id < 0 || id >= (tm ? T : F) || seen[tm ? F + id : id]++)
        {
            fprintf(stderr, "%s: bad action %s (each flow and timer has one cause)\n", w.name.c_str(), o.child.c_str());
            return false;
        }
        uint32_t ch = tm ? DACT_TIMER | id : id;
        char k = o.cause[0];
        if (k == 'm')
        {
            mains.push_back({o.ord, ch});
            continue;
        }
        int c = k == 'r' || k == 's' ? 2 * cid + (k == 's') : 2 * F + cid;
        if ((k != 'r' && k != 's' && k != 't') || cid < 0 || cid >= (k == 't' ? T : F))
        {
            fprintf(stderr, "%s: bad cause %s\n", w.name.c_str(), o.cause.c_str());
            return false;
        }
        w.acts[c].push_back(DAct{ch, (uint32_t)o.ord});
        if (!tm)
            w.ndeps[id] = 1;
    }
    for (int i = 0; i < F + T; i++)
        if (!seen[i])
        {
            fprintf(stderr, "%s: %s %d has no cause\n", w.name.c_str(), i < F ? "flow" : "timer", i < F ? i : i - F);
            return false;
        }
    for (auto &a : w.acts)
        std::sort(a.begin(), a.end(), [](const DAct &x, const DAct &y) { return x.ord < y.ord; });
    // main's actions, in uid order, are the roots (root i + 1: the chain of its event, a timer's or
    // a flow's Initialize); what they and their timers install is known now (built here: short)
    std::sort(mains.begin(), mains.end());
    bool ok = true;
    auto push = [&](Chain *c, int64_t lead, int ord) {
        if (c->r[RUNS - 1].code)
            ok = false; // (the host builds chains without tails)
        cpush(c, ccode(lead, ord), Chain(*c));
    };
    std::function<void(uint32_t, int64_t, const Chain &, int)> act = [&](uint32_t ch, int64_t t, const Chain &c, int ord) {
        Chain x = c;
        if (ch & DACT_TIMER)
        {
            int tm = ch & ~DACT_TIMER;
            push(&x, w.tdelay[tm], ord);
            for (auto &a : w.acts[2 * F + tm])
                act(a.child, t + w.tdelay[tm], x, a.ord);
            w.acts[2 * F + tm].clear(); // (fired here, never on the device)
            return;
        }
        push(&x, 0, ord);              // Initialize (SendFlow)
        push(&x, send_lat, 0);         // StartApplication
        w.pre.push_back({(int)ch, t + send_lat, x});
    };
    for (size_t i = 0; i < mains.size(); i++)
    {
        Chain c = {};
        c.root = i + 1;
        uint32_t ch = mains[i].second;
        // the root's own event, from main: its chain's hash starts after it
        push(&c, ch & DACT_TIMER ? w.tdelay[ch & ~DACT_TIMER] : 0, 0);
        c.hash = 0;
        for (int k = 0; k < NANC; k++)
            c.anc[k] = 0;
        w.troot.push_back(c);
        if (ch & DACT_TIMER)
        {
            int tm = ch & ~DACT_TIMER;
            for (auto &a : w.acts[2 * F + tm])
                act(a.child, w.tdelay[tm], c, a.ord);
            w.acts[2 * F + tm].clear();
        }
        else
        {
            Chain x = c;
            push(&x, send_lat, 0);
            w.pre.push_back({(int)ch, send_lat, x});
        }
    }
    if (!ok)
    {
        fprintf(stderr, "%s: the actions of main nest too deep\n", w.name.c_str());
        return false;
    }
    if ((int)mains.size() > F)
    {
        fprintf(stderr, "%s: more roots (main's actions) than flows\n", w.name.c_str());
        return false;
    }
    return true;
}

static bool build_world(World &w, const Config &config, const string &topo_file, const string &flow_file, uint64_t bps, int64_t delay)
{
    Cfg cfg{config};
    Params &P = w.P;
    P = Params{};
    P.cc = cfg.u32("CC_MODE", 1);
    if (P.cc != 1 && P.cc != 3)
    {
        fprintf(stderr, "%s: CC_MODE %d: only 1 (DCQCN) and 3 (HPCC) are modelled\n", w.name.c_str(), P.cc);
        return false;
    }
    if (cfg.u32("MULTI_RATE", 1) && P.cc == 3)
    {
        fprintf(stderr, "%s: MULTI_RATE 1 is not modelled\n", w.name.c_str());
        return false;
    }
    if (cfg.u32("L2_BACK_TO_ZERO", 0))
    {
        fprintf(stderr, "%s: L2_BACK_TO_ZERO 1 is not modelled\n", w.name.c_str());
        return false;
    }
    P.int_size = P.cc == 3 ? 42 : 0;
    P.mtu = cfg.u32("PACKET_PAYLOAD_SIZE", 1000);
    P.ack_interval = cfg.u32("L2_ACK_INTERVAL", 0);
    P.chunk = cfg.u32("L2_CHUNK_SIZE", 0);
    if (P.ack_interval == 0)
    {
        fprintf(stderr, "%s: L2_ACK_INTERVAL 0 is not modelled\n", w.name.c_str());
        return false;
    }
    P.nack_interval = 500000; // RdmaHw "NACK Generation Interval", 500 us
    P.bps = bps;
    P.delay = delay;
    P.var_win = cfg.u32("VAR_WIN", 0);
    P.rate_bound = cfg.u32("RATE_BOUND", 1);
    P.ack_high_prio = cfg.u32("ACK_HIGH_PRIO", 0);
    P.ecn = cfg.u32("ENABLE_QCN", 1);
    P.qbb = 1;
    P.g = cfg.num("EWMA_GAIN", 1.0 / 16);
    P.rate_first_cnp = 1.0;
    P.clamp = cfg.u32("CLAMP_TARGET_RATE", 0);
    // MicroSeconds(double): the attribute is a double, MicroSeconds takes an integer
    P.alpha_int = (int64_t)(uint64_t)cfg.num("ALPHA_RESUME_INTERVAL", 55) * 1000;
    P.dec_int = (int64_t)(uint64_t)cfg.num("RATE_DECREASE_INTERVAL", 4) * 1000;
    P.rp_int = (int64_t)(uint64_t)cfg.num("RP_TIMER", 1500) * 1000;
    P.rpg_threshold = cfg.u32("FAST_RECOVERY_TIMES", 5);
    P.rai = parse_rate(cfg.str("RATE_AI", "5Mb/s"));
    P.rhai = parse_rate(cfg.str("RATE_HAI", "50Mb/s"));
    P.min_rate = parse_rate(cfg.str("MIN_RATE", "100Mb/s"));
    P.u_target = cfg.num("U_TARGET", 0.95);
    P.mi_thresh = cfg.u32("MI_THRESH", 5);
    P.fast_react = cfg.u32("FAST_REACT", 1);
    P.multi_rate = 0;
    P.sample_feedback = cfg.u32("SAMPLE_FEEDBACK", 0);
    P.int_multi = cfg.u32("INT_MULTI", 1);
    P.buffer_size = cfg.u32("BUFFER_SIZE", 16) * 1024 * 1024;
    P.pfc_shift = 3;
    // KMAX_MAP n rate k ...: a port's kmin and kmax in KB (ConfigEcn multiplies by 1000), by its rate
    auto ecn_lookup = [&](const char *key, uint64_t rate, double *out) {
        if (!cfg.has(key))
            return false;
        const vector<string> &v = config.at(key);
        int n = atoi(v[0].c_str());
        for (int i = 0; i < n; i++)
            if (strtoull(v[1 + 2 * i].c_str(), 0, 10) == rate)
            {
                *out = atof(v[2 + 2 * i].c_str());
                return true;
            }
        return false;
    };
    // (SimAI's ReadConf sets global_t = 1 whatever GLOBAL_T says; GLOBAL_T_HONOR 1: as it says)
    int global_t = cfg.u32("GLOBAL_T", 1);

    // ---- topology
    int &H = w.H, &S = w.S;
    vector<std::pair<int, int>> links;
    vector<uint64_t> link_rate; // per undirected link
    vector<int64_t> link_delay;
    int gps = 1; // gpus_per_server
    {
        std::ifstream f(topo_file);
        if (!f)
        {
            fprintf(stderr, "cannot open %s\n", topo_file.c_str());
            return false;
        }
        string a, b, x;
        f >> x;
        if (!x.empty() && isdigit((unsigned char)x[0]))
        {
            // SimAI's format: node ids are GPUs first, then NVSwitches and switches
            int nodes = atoi(x.c_str()), nvs, sws, nl;
            string gpu_type;
            f >> gps >> nvs >> sws >> nl >> gpu_type;
            H = nodes - nvs - sws, S = nvs + sws;
            w.nvs.assign(S, 0);
            for (int i = 0; i < nvs + sws; i++)
            {
                int id;
                f >> id;
                if (id < H || id >= nodes)
                {
                    fprintf(stderr, "%s: switch %d: SimAI topologies must list GPUs before switches\n", w.name.c_str(), id);
                    return false;
                }
                w.nvs[id - H] = i < nvs;
            }
            for (int i = 0; i < nl; i++)
            {
                int u, v;
                string r, d;
                double err;
                f >> u >> v >> r >> d >> err;
                uint64_t lr = parse_rate(r);
                int64_t ld = parse_delay(d);
                if (err != 0)
                {
                    fprintf(stderr, "%s: link error rates are not modelled\n", w.name.c_str());
                    return false;
                }
                if (lr == 0 || ld <= 0)
                {
                    fprintf(stderr, "%s: link %d-%d: rate %s, delay %s\n", w.name.c_str(), u, v, r.c_str(), d.c_str());
                    return false;
                }
                links.push_back({u, v});
                link_rate.push_back(lr);
                link_delay.push_back(ld);
            }
            P.simai = 1;
        }
        else
            do
            {
                if (x == "hosts")
                    f >> H;
                else if (x == "switches")
                    f >> S;
                else if (x == "link")
                {
                    f >> a >> b;
                    auto id = [&](const string &y) { return (y[0] == 'h' ? 0 : H) + atoi(y.c_str() + 1); };
                    links.push_back({id(a), id(b)});
                    link_rate.push_back(bps);
                    link_delay.push_back(delay);
                }
            } while (f >> x);
        if (!P.simai)
            w.nvs.assign(S, 0);
    }
    P.bps = link_rate.empty() ? bps : link_rate[0];
    P.seq_extra = P.simai ? 4 : 0;
    P.ecn_pcnt = P.simai ? 1025 : 257;
    if (P.simai && !cfg.u32("GLOBAL_T_HONOR", 0))
        global_t = 1;
    if (global_t != 1 && !P.simai)
    {
        fprintf(stderr, "%s: GLOBAL_T 0 is modelled for SimAI topologies only\n", w.name.c_str());
        return false;
    }
    // SimAI.conf stops at 4e13 s: clamp (in double, before the conversion overflows)
    w.stop_ns = cfg.num("SIMULATOR_STOP_TIME", P.simai ? 3.01 : 10.0) * 1e9;
    int N = w.N = H + S, L = w.L = 2 * (int)links.size();
    vector<int> &deg = w.deg;
    deg.assign(N, 0);
    for (auto &l : links)
        deg[l.first]++, deg[l.second]++;
    int K = 1;
    for (int n = 0; n < N; n++)
        K = std::max(K, deg[n]);
    w.K = K;
    if (K > KMAX)
    {
        fprintf(stderr, "%s: a node has %d ports (at most %d)\n", w.name.c_str(), K, KMAX);
        return false;
    }
    for (int h = 0; h < H; h++)
        if (deg[h] < 1 || deg[h] > MAXD || (!P.simai && deg[h] != 1))
        {
            fprintf(stderr, "%s: host %d has %d links (%s expected)\n", w.name.c_str(), h, deg[h], P.simai ? "1 to 4" : "one");
            return false;
        }
    w.out_link.assign((size_t)N * K, -1);
    w.in_link.assign((size_t)N * K, -1);
    w.link_dst.assign(L, 0);
    w.link_dport.assign(L, 0);
    w.lbps.assign(L, 0);
    w.ldelay.assign(L, 0);
    vector<std::map<int, int>> nbr(N); // neighbour -> port (ns-3's nbr2if: by node id, the last link wins)
    {
        vector<int> d(N, 0);
        for (size_t i = 0; i < links.size(); i++)
        {
            int a = links[i].first, b = links[i].second, pa = d[a]++, pb = d[b]++;
            w.out_link[(size_t)a * K + pa] = 2 * i;
            w.in_link[(size_t)a * K + pa] = 2 * i + 1;
            w.out_link[(size_t)b * K + pb] = 2 * i + 1;
            w.in_link[(size_t)b * K + pb] = 2 * i;
            w.link_dst[2 * i] = b, w.link_dport[2 * i] = pb;
            w.link_dst[2 * i + 1] = a, w.link_dport[2 * i + 1] = pa;
            w.lbps[2 * i] = w.lbps[2 * i + 1] = link_rate[i];
            w.ldelay[2 * i] = w.ldelay[2 * i + 1] = link_delay[i];
            nbr[a][b] = pa;
            nbr[b][a] = pb;
        }
    }
    // routes (CalculateRoute: BFS from each host over switches, neighbours by id). Next hops
    // through an NVSwitch replace the others: once a node has one, only NVSwitches are added
    auto is_nvs = [&](int n) { return n >= H && w.nvs[n - H]; };
    vector<vector<vector<int>>> next_hop(S, vector<vector<int>>(H)); // [switch][host] -> next nodes
    vector<vector<vector<int>>> hnext(H, vector<vector<int>>(H));    // [host][host] -> next nodes
    vector<vector<int64_t>> hop_n(N, vector<int64_t>(H, -1));         // links from node to host
    // CalculateRoute's sums along the path that first reaches a node: pairDelay, pairTxDelay
    // (one MTU frame per hop) and pairBw (the slowest link), from host i to host h at [i][h]
    vector<vector<uint64_t>> pdelay(H, vector<uint64_t>(H, 0)), ptx(H, vector<uint64_t>(H, 0)), pbw(H, vector<uint64_t>(H, 0));
    for (int h = 0; h < H; h++)
    {
        vector<int> q = {h}, dis(N, -1);
        vector<uint64_t> dl(N, 0), txd(N, 0), bw(N, 0);
        dis[h] = 0;
        bw[h] = ~0ull;
        for (size_t i = 0; i < q.size(); i++)
        {
            int now = q[i], d = dis[now];
            for (auto &e : nbr[now])
            {
                int next = e.first;
                if (dis[next] < 0)
                {
                    int l = w.out_link[(size_t)now * K + e.second];
                    dis[next] = d + 1;
                    dl[next] = dl[now] + (uint64_t)w.ldelay[l];
                    txd[next] = txd[now] + (uint64_t)P.mtu * 1000000000ull * 8 / w.lbps[l];
                    bw[next] = std::min(bw[now], w.lbps[l]);
                    if (next >= H)
                        q.push_back(next);
                }
                if (d + 1 == dis[next])
                {
                    vector<int> &v = next < H ? hnext[next][h] : next_hop[next - H][h];
                    bool via = false;
                    for (int y : v)
                        via = via || is_nvs(y);
                    if (!via)
                    {
                        if (is_nvs(now))
                            v.clear();
                        v.push_back(now);
                    }
                    else if (is_nvs(now))
                        v.push_back(now);
                }
            }
        }
        for (int n = 0; n < N; n++)
            hop_n[n][h] = dis[n];
        for (int i = 0; i < H; i++)
            pdelay[i][h] = dl[i], ptx[i][h] = txd[i], pbw[i][h] = bw[i];
    }
    w.rt.assign((size_t)S * H, Route{});
    for (int x = 0; x < S; x++)
        for (int h = 0; h < H; h++)
        {
            Route &r = w.rt[(size_t)x * H + h];
            r.off = w.rtp.size();
            r.n = next_hop[x][h].size();
            for (uint32_t i = 0; i < r.n; i++)
                w.rtp.push_back((uint8_t)nbr[H + x][next_hop[x][h][i]]);
        }
    // maxRtt, maxBdp: delay * 2 + txDelay, and its product with the bandwidth, over host pairs
    uint64_t max_rtt = 0, max_bdp = 0;
    auto pair_rtt = [&](int a, int b) { return pdelay[a][b] * 2 + ptx[a][b]; };
    auto pair_bdp = [&](int a, int b) { return pair_rtt(a, b) * pbw[a][b] / 1000000000ull / 8; };
    {
        int64_t far = 0;
        for (int i = 0; i < H; i++)
            for (int j = 0; j < H; j++)
                far = std::max(far, hop_n[i][j]);
        if (P.cc == 3 && far - 1 > MAX_HOP)
        {
            // the reference's INT header has 5 hops; on longer paths it writes past hp.hop[]
            fprintf(stderr, "%s: HPCC: paths of %lld switches (INT records at most %d)\n", w.name.c_str(), (long long)(far - 1), MAX_HOP);
            return false;
        }
        for (int i = 0; i < H; i++)
            for (int j = 0; j < H; j++)
            {
                max_rtt = std::max<uint64_t>(max_rtt, pair_rtt(i, j));
                max_bdp = std::max<uint64_t>(max_bdp, pair_bdp(i, j));
            }
    }
    P.base_rtt = max_rtt;
    const int has_win = cfg.u32("HAS_WIN", 1);
    P.win = has_win ? (uint32_t)max_bdp : 0;

    // ---- switch ports: headroom, pfc_a_shift (from 3, one less per halving of the rate above the
    // NIC rate, which is host 0's first link's), ECN thresholds by rate
    {
        uint64_t nic_rate = w.lbps[w.out_link[0]];
        for (int x = 0; x < S; x++)
            for (int p = 0; p < deg[H + x]; p++)
            {
                int l = w.out_link[(size_t)(H + x) * K + p];
                PortCfg pc = {};
                uint64_t r = w.lbps[l];
                pc.bps = r;
                pc.hdrm_cap = (uint32_t)(r * (uint64_t)w.ldelay[l] / 8 / 1000000000ull * 3);
                pc.shift = 3;
                while (r > nic_rate && pc.shift > 0)
                {
                    pc.shift--;
                    r /= 2;
                }
                double kmin = 0, kmax = 0, pmax = 0;
                if (!w.nvs[x] && (!ecn_lookup("KMIN_MAP", pc.bps, &kmin) || !ecn_lookup("KMAX_MAP", pc.bps, &kmax) || !ecn_lookup("PMAX_MAP", pc.bps, &pmax)))
                {
                    fprintf(stderr, "%s: KMIN/KMAX/PMAX_MAP: no entry for %llu bps\n", w.name.c_str(), (unsigned long long)pc.bps);
                    return false;
                }
                pc.kmin = (uint32_t)kmin * 1000;
                pc.kmax = (uint32_t)kmax * 1000;
                pc.pmax = pmax;
                w.pcfg.push_back(pc);
            }
    }

    // ---- flows (rows in start order, stable; per host pair ports from 10000), or a DAG
    vector<FlowInfo> &fi = w.fi;
    w.dag = flow_file.size() > 4 && flow_file.compare(flow_file.size() - 4, 4, ".dag") == 0;
    if (w.dag)
    {
        std::ifstream f(flow_file);
        if (!f)
        {
            fprintf(stderr, "cannot open %s\n", flow_file.c_str());
            return false;
        }
        string x;
        vector<std::pair<int, int>> deps; // (child, parent << 1 | kind)
        vector<TracedOn> ons;
        while (f >> x)
        {
            if (x == "flow")
            {
                unsigned id, a, b;
                unsigned long long sz;
                f >> id >> a >> b >> sz;
                if (id != fi.size())
                {
                    fprintf(stderr, "%s: DAG flow ids must be 0, 1, ... in order (flow %u)\n", w.name.c_str(), id);
                    return false;
                }
                FlowInfo y = {};
                y.src = a, y.dst = b, y.size = sz, y.start = -1, y.dyn = 1;
                fi.push_back(y);
            }
            else if (x == "root")
            {
                unsigned id;
                unsigned long long st;
                f >> id >> st;
                if (id >= fi.size())
                {
                    fprintf(stderr, "%s: root %u before its flow\n", w.name.c_str(), id);
                    return false;
                }
                fi[id].start = (int64_t)st;
                fi[id].dyn = 0;
            }
            else if (x == "dep")
            {
                unsigned id, par;
                string k;
                f >> id >> k >> par;
                if (id >= fi.size() || par >= fi.size() || (k != "r" && k != "s"))
                {
                    fprintf(stderr, "%s: bad dependency %u %s %u\n", w.name.c_str(), id, k.c_str(), par);
                    return false;
                }
                deps.push_back({(int)id, (int)(par << 1 | (k == "s"))});
            }
            else if (x == "timer")
            {
                unsigned id;
                long long d;
                f >> id >> d;
                if (id != w.tdelay.size() || d < 0)
                {
                    fprintf(stderr, "%s: timer ids must be 0, 1, ... in order (timer %u)\n", w.name.c_str(), id);
                    return false;
                }
                w.tdelay.push_back(d);
            }
            else if (x == "on")
            {
                TracedOn o;
                f >> o.child >> o.cause >> o.ord;
                ons.push_back(o);
            }
            else
                std::getline(f, x);
        }
        w.ndeps.assign(fi.size(), 0);
        vector<vector<uint32_t>> ch(fi.size());
        for (auto &d : deps)
        {
            ch[d.second >> 1].push_back((uint32_t)d.first << 1 | (d.second & 1));
            w.ndeps[d.first]++;
        }
        w.ch_off.assign(fi.size() + 1, 0);
        for (size_t g = 0; g < fi.size(); g++)
        {
            std::stable_sort(ch[g].begin(), ch[g].end(), [](uint32_t a, uint32_t b) { return a >> 1 < b >> 1; });
            w.ch_off[g + 1] = w.ch_off[g] + ch[g].size();
            w.ch_list.insert(w.ch_list.end(), ch[g].begin(), ch[g].end());
        }
        int64_t t0 = -1;
        if (!ons.empty() && !traced_dag(w, ons, !deps.empty(), (int64_t)cfg.num("SEND_LAT_NS", 3000)))
            return false;
        for (size_t i = 0; i < fi.size() && !w.traced; i++)
        {
            if (fi[i].dyn && w.ndeps[i] == 0)
            {
                fprintf(stderr, "%s: DAG flow %zu is neither a root nor has dependencies\n", w.name.c_str(), i);
                return false;
            }
            if (!fi[i].dyn)
            {
                // roots get their ports before any message is installed: one start time for all
                if (t0 >= 0 && fi[i].start != t0)
                {
                    fprintf(stderr, "%s: DAG roots must all start at the same time\n", w.name.c_str());
                    return false;
                }
                t0 = fi[i].start;
            }
        }
    }
    else
    {
        std::ifstream f(flow_file);
        if (!f)
        {
            fprintf(stderr, "cannot open %s\n", flow_file.c_str());
            return false;
        }
        string line;
        std::getline(f, line);
        while (std::getline(f, line))
        {
            unsigned a, b;
            unsigned long long sz, st;
            if (sscanf(line.c_str(), "%u,%u,%llu,%llu", &a, &b, &sz, &st) != 4)
                continue;
            FlowInfo x = {};
            x.src = a, x.dst = b, x.size = sz, x.start = (int64_t)st;
            fi.push_back(x);
        }
    }
    int F = fi.size();
    for (auto &x : fi)
    {
        if (x.src >= H || x.dst >= H)
        {
            fprintf(stderr, "%s: a flow between %d and %d: not hosts\n", w.name.c_str(), x.src, x.dst);
            return false;
        }
        if (global_t == 1)
            x.win = P.win, x.base_rtt = P.base_rtt;
        else
        {
            x.win = has_win ? (uint32_t)pair_bdp(x.src, x.dst) : 0;
            x.base_rtt = pair_rtt(x.src, x.dst);
        }
    }
    // the flows started at given times (in a DAG, the roots), in start order
    vector<int> order;
    for (int i = 0; i < F; i++)
        if (!fi[i].dyn)
            order.push_back(i);
    std::stable_sort(order.begin(), order.end(), [&](int a, int b) { return fi[a].start < fi[b].start; });
    const int FR = order.size();
    {
        std::map<std::pair<int, int>, uint32_t> port;
        // ScheduleFlowInputs runs once per start time and starts that time's flows; each
        // StartApplication descends from it through Node::AddApplication's Application::Start
        int64_t prev_t = 0;
        int prev_n = 0, k = 0;
        for (int i = 0; i < FR;)
        {
            int j = i;
            while (j < FR && fi[order[j]].start == fi[order[i]].start)
                j++;
            int64_t t = fi[order[i]].start;
            for (int m = i; m < j; m++)
            {
                FlowInfo &x = fi[order[m]];
                auto key = std::make_pair(x.src, x.dst);
                if (!port.count(key))
                    port[key] = 10000;
                x.sport = port[key]++;
                x.order = m;
                Chain c = {};
                c.root = m + 1;
                host_push(&c, t - prev_t, k == 0 ? 0 : prev_n); // ScheduleFlowInputs (from the previous one, or main)
                host_push(&c, 0, m - i);                        // Application::Start
                host_push(&c, 0, 0);                            // StartApplication
                c.hash = 0; // (the hashes cover what follows the start)
                for (int k = 0; k < NANC; k++)
                    c.anc[k] = 0;
                x.start_chain = c;
            }
            prev_t = t;
            prev_n = j - i;
            k++;
            i = j;
        }
        if (w.dag)
        {
            // the port counters after the roots
            w.pcnt.assign((size_t)H * H, 10000);
            for (auto &e : port)
                w.pcnt[(size_t)e.first.first * H + e.first.second] = (uint16_t)e.second;
        }
    }
    // the devices of a flow's queue pairs: RdmaHw's tables of a host towards a host are its next
    // hops, through NVSwitches (m_rtTable_nxthop_nvswitch) or not (m_rtTable); among several, a
    // queue pair picks by Hash32 of its (sip, dip, sport, dport)
    {
        auto tables = [&](int a, int b, vector<int> &nv, vector<int> &rt_) {
            nv.clear(), rt_.clear();
            for (int y : hnext[a][b])
                (is_nvs(y) ? nv : rt_).push_back(nbr[a][y]);
        };
        auto pick = [&](const vector<int> &v, uint32_t sip, uint32_t dip, uint16_t sport, uint16_t dport) {
            return v[qp_pick(sip, dip, sport, dport, v.size())];
        };
        vector<int> nv, rt_;
        if (w.dag)
        {
            // every host pair's candidates, for the flows that pick at their start
            w.pnic.assign((size_t)H * H, PairNic{});
            for (int a = 0; a < H; a++)
                for (int b = 0; b < H; b++)
                {
                    if (a == b)
                        continue;
                    PairNic &pn = w.pnic[(size_t)a * H + b];
                    if (deg[a] == 1)
                    {
                        pn.sn = pn.rn = 1;
                        continue;
                    }
                    tables(a, b, nv, rt_);
                    const vector<int> sv = (a / gps == b / gps || !nv.empty()) ? nv : rt_, rv = !rt_.empty() ? rt_ : nv;
                    if (sv.size() > 3 || rv.size() > 3)
                    {
                        fprintf(stderr, "%s: GPU %d has more than 3 devices towards %d\n", w.name.c_str(), a, b);
                        return false;
                    }
                    pn.sn = sv.size(), pn.rn = rv.size();
                    for (size_t i = 0; i < sv.size(); i++)
                        pn.s[i] = sv[i];
                    for (size_t i = 0; i < rv.size(); i++)
                        pn.r[i] = rv[i];
                }
        }
        for (size_t i = 0; i < fi.size(); i++)
        {
            FlowInfo &x = fi[i];
            if (x.dyn)
                continue; // (picked at its start)
            if (deg[x.src] == 1 && deg[x.dst] == 1)
            {
                x.snic = x.rnic = 0;
                continue;
            }
            if (x.src == x.dst)
            {
                fprintf(stderr, "%s: flow %zu from a GPU to itself\n", w.name.c_str(), i);
                return false;
            }
            // GetNicIdxOfQp: the NVSwitch table within a server or when it has an entry
            tables(x.src, x.dst, nv, rt_);
            const vector<int> &sv = (x.src / gps == x.dst / gps || !nv.empty()) ? nv : rt_;
            // GetNicIdxOfRxQp: m_rtTable first
            tables(x.dst, x.src, nv, rt_);
            const vector<int> &rv = !rt_.empty() ? rt_ : nv;
            if (sv.empty() || rv.empty())
            {
                fprintf(stderr, "%s: flow %zu: no route between GPUs %d and %d\n", w.name.c_str(), i, x.src, x.dst);
                return false;
            }
            x.snic = (uint8_t)pick(sv, node_ip(x.src), node_ip(x.dst), (uint16_t)x.sport, 100);
            // the receiving queue pair: sip and dip are the receiver's and the sender's
            x.rnic = (uint8_t)pick(rv, node_ip(x.dst), node_ip(x.src), 100, (uint16_t)x.sport);
        }
    }
    w.start_list.assign(FR, 0);
    w.start_off.assign(H + 1, 0);
    for (int i = 0; i < FR; i++)
        w.start_off[fi[order[i]].src + 1]++;
    for (int h = 0; h < H; h++)
        w.start_off[h + 1] += w.start_off[h];
    {
        vector<int> pos(w.start_off.begin(), w.start_off.end() - 1);
        for (int i = 0; i < FR; i++)
            w.start_list[pos[fi[order[i]].src]++] = order[i];
    }
    return true;
}

int main(int argc, char **argv)
{
    if (argc < 2)
    {
        fprintf(stderr, "usage: twins-rdma CONFIG --topo=F --flows=F --out=F [--txtrace=F --from=ns --until=ns] [KEY=VALUE ...]\n"
                        "       twins-rdma CONFIG --batch=JOBFILE --outdir=DIR [KEY=VALUE ...]\n");
        return 1;
    }
    Config base;
    if (!read_config(argv[1], base))
        return 1;
    string topo_file, flow_file, out_file = "fct.txt", tx_file, rate_s = "100Gbps", batch_file, out_dir = ".", data_dirs = "", sent_file;
    int64_t delay = 1000, tx_from = 0, tx_until = INT64_MAX;
    int q3cap = 4096, tail_log2 = 24, spread = 32, serial = 0, kw = 1, rank_ahead = 2, rank_join = 1, rank_bs = 512, rank_every = 8, persist = 1;
    for (int i = 2; i < argc; i++)
    {
        string a = argv[i];
        size_t eq = a.find('=');
        string k = a.substr(0, eq), v = eq == string::npos ? "" : a.substr(eq + 1);
        if (k == "--topo")
            topo_file = v;
        else if (k == "--flows")
            flow_file = v;
        else if (k == "--out")
            out_file = v;
        else if (k == "--batch")
            batch_file = v;
        else if (k == "--outdir")
            out_dir = v;
        else if (k == "--path")
            data_dirs = v;
        else if (k == "--txtrace")
            tx_file = v;
        else if (k == "--sent")
            sent_file = v;
        else if (k == "--from")
            tx_from = atoll(v.c_str());
        else if (k == "--until")
            tx_until = atoll(v.c_str());
        else if (k == "--rate")
            rate_s = v;
        else if (k == "--delay")
            delay = atoll(v.c_str());
        else if (k == "--q3cap")
            q3cap = atoi(v.c_str());
        else if (k == "--tail")
            tail_log2 = atoi(v.c_str());
        else if (k == "--spread")
            spread = std::max(1, std::min(BLOCK, atoi(v.c_str())));
        else if (k == "--serial")
            serial = 1;
        else if (k == "--win")
            kw = std::max(1, std::min(255, atoi(v.c_str())));
        else if (k == "--rank")
            rank_ahead = std::max(0, atoi(v.c_str()));
        else if (k == "--rank-join")
            rank_join = std::max(1, atoi(v.c_str()));
        else if (k == "--rank-bs")
            rank_bs = std::max(32, atoi(v.c_str()));
        else if (k == "--persist")
            persist = atoi(v.c_str());
        else if (k == "--rank-every")
            rank_every = std::max(1, atoi(v.c_str()));
        else if (k.compare(0, 2, "--") != 0 && eq != string::npos)
            set_override(base, k, v);
        else
        {
            fprintf(stderr, "unknown argument %s\n", argv[i]);
            return 1;
        }
    }
    uint64_t bps = parse_rate(rate_s);

    // ---- the worlds: one, or a job file's lines "name topo flows [KEY=VALUE ...]" (the files
    // looked up in the --path directories, colon separated, and as given)
    vector<World> worlds;
    auto find_file = [&](const string &f) {
        size_t b = 0;
        while (b <= data_dirs.size() && !data_dirs.empty())
        {
            size_t c = data_dirs.find(':', b);
            string d = data_dirs.substr(b, c == string::npos ? string::npos : c - b);
            string p = d + "/" + f;
            if (std::ifstream(p))
                return p;
            if (c == string::npos)
                break;
            b = c + 1;
        }
        return f;
    };
    if (batch_file.empty())
    {
        simai_link0(topo_file, &bps, &delay); // a SimAI topology gives the rate and delay
        worlds.emplace_back();
        worlds[0].name = "run";
        if (!build_world(worlds[0], base, topo_file, flow_file, bps, delay))
            return 1;
    }
    else
    {
        if (!tx_file.empty())
        {
            fprintf(stderr, "--txtrace traces one world (no --batch)\n");
            return 1;
        }
        std::ifstream f(batch_file);
        if (!f)
        {
            fprintf(stderr, "cannot open %s\n", batch_file.c_str());
            return 1;
        }
        string line;
        while (std::getline(f, line))
        {
            std::istringstream is(line);
            string name, topo, flows, kv;
            if (!(is >> name) || name[0] == '#' || !(is >> topo >> flows))
                continue;
            Config cfg = base;
            while (is >> kv)
            {
                size_t eq = kv.find('=');
                if (eq == string::npos)
                {
                    fprintf(stderr, "%s: bad override %s\n", name.c_str(), kv.c_str());
                    return 1;
                }
                set_override(cfg, kv.substr(0, eq), kv.substr(eq + 1));
            }
            if (worlds.empty())
                simai_link0(find_file(topo), &bps, &delay);
            worlds.emplace_back();
            worlds.back().name = name;
            if (!build_world(worlds.back(), cfg, find_file(topo), find_file(flows), bps, delay))
                return 1;
            if (worlds.back().P.simai != worlds[0].P.simai)
            {
                fprintf(stderr, "%s: a batch mixes SimAI and HPCC topologies\n", name.c_str());
                return 1;
            }
        }
        if (worlds.empty())
        {
            fprintf(stderr, "%s: no jobs\n", batch_file.c_str());
            return 1;
        }
    }
    const int W = worlds.size();
    // the slot: the shortest link delay of all worlds
    int64_t D = INT64_MAX;
    for (auto &w : worlds)
        for (int64_t d : w.ldelay)
            D = std::min(D, d);
    for (auto &w : worlds)
    {
        w.P.delay = D;
        if (w.P.cc == 1 && (w.P.alpha_int < D || w.P.dec_int < D || w.P.rp_int < D))
        {
            fprintf(stderr, "%s: DCQCN timer intervals must be at least the slot (the shortest link delay, %lld ns)\n", w.name.c_str(),
                    (long long)D);
            return 1;
        }
    }
    delay = D;

    // ---- the worlds side by side: hosts of all worlds, then switches; links and flows in turn
    int H = 0, S = 0, L = 0, F = 0, K = 1;
    vector<int> hbase(W), sbase(W), lbase(W), fbase(W);
    for (int i = 0; i < W; i++)
    {
        World &w = worlds[i];
        hbase[i] = H, sbase[i] = S, lbase[i] = L, fbase[i] = F;
        H += w.H, S += w.S, L += w.L, F += w.fi.size();
        K = std::max(K, w.K);
    }
    const int N = H + S;
    auto gnode = [&](int wi, int n) { return n < worlds[wi].H ? hbase[wi] + n : H + sbase[wi] + (n - worlds[wi].H); };
    vector<int> deg(N), out_link((size_t)N * K, -1), in_link((size_t)N * K, -1), link_dst(L), link_dport(L);
    vector<Route> rt;
    vector<uint8_t> rtp;
    vector<SwInfo> si(S);
    vector<FlowInfo> fi(F);
    vector<Chain> root_chain(F);
    vector<int> start_list(F), start_off(H + 1, 0), h_world(H), s_world(S);
    size_t sl_base = 0; // the next world's first entry in start_list
    vector<Params> Pw(W);
    int64_t max_slots = 0;
    for (int wi = 0; wi < W; wi++)
    {
        World &w = worlds[wi];
        Pw[wi] = w.P;
        max_slots = std::max(max_slots, (int64_t)std::min(w.stop_ns / (double)D, 1e15));
        for (int n = 0; n < w.N; n++)
        {
            int g = gnode(wi, n);
            deg[g] = w.deg[n];
            for (int p = 0; p < w.K; p++)
            {
                int ol = w.out_link[(size_t)n * w.K + p], il = w.in_link[(size_t)n * w.K + p];
                out_link[(size_t)g * K + p] = ol < 0 ? -1 : lbase[wi] + ol;
                in_link[(size_t)g * K + p] = il < 0 ? -1 : lbase[wi] + il;
            }
        }
        for (int l = 0; l < w.L; l++)
        {
            link_dst[lbase[wi] + l] = gnode(wi, w.link_dst[l]);
            link_dport[lbase[wi] + l] = w.link_dport[l];
        }
        const uint32_t rb = rtp.size();
        rtp.insert(rtp.end(), w.rtp.begin(), w.rtp.end());
        for (int x = 0; x < w.S; x++)
        {
            SwInfo &z = si[sbase[wi] + x];
            z.seed = (uint32_t)(w.H + x);
            z.hbase = hbase[wi];
            z.rt_off = rt.size();
            z.nvs = w.nvs[x];
            for (int h = 0; h < w.H; h++)
            {
                Route r = w.rt[(size_t)x * w.H + h];
                r.off += rb;
                rt.push_back(r);
            }
            s_world[sbase[wi] + x] = wi;
        }
        for (size_t r = 0; r < w.troot.size(); r++)
        {
            Chain c = w.troot[r];
            c.root += fbase[wi];
            root_chain[c.root - 1] = c;
        }
        for (size_t f = 0; f < w.fi.size(); f++)
        {
            FlowInfo x = w.fi[f];
            if (!x.dyn)
            {
                x.start_chain.root += fbase[wi]; // roots over all worlds (the order within one is kept)
                root_chain[x.start_chain.root - 1] = x.start_chain;
            }
            x.src += hbase[wi];
            x.dst += hbase[wi];
            fi[fbase[wi] + f] = x;
        }
        for (int h = 0; h < w.H; h++)
        {
            h_world[hbase[wi] + h] = wi;
            start_off[hbase[wi] + h + 1] = w.start_off[h + 1] - w.start_off[h];
        }
        for (size_t f = 0; f < w.start_list.size(); f++)
            start_list[sl_base + f] = fbase[wi] + w.start_list[f];
        sl_base += w.start_list.size();
    }
    for (int h = 0; h < H; h++)
        start_off[h + 1] += start_off[h];
    if (H + S > 65535 * 2 || H > 65535)
    {
        fprintf(stderr, "too many hosts (%d; frames carry 16-bit host ids)\n", H);
        return 1;
    }
    Params P = Pw[0];
    if (W == 1)
        printf("maxRtt=%llu maxBdp=%u\n", (unsigned long long)P.base_rtt, P.win);
    else
        printf("%d worlds: %d hosts, %d switches, %d flows\n", W, H, S, F);

    // ---- device state
    Sim s = {};
    s.H = H, s.S = S, s.N = N, s.L = L, s.K = K, s.F = F;
    s.P = P;
    s.fD = fdiv_make(P.delay);
    s.W = W;
    s.Pw = dev_copy(Pw);
    s.h_world = dev_copy(h_world);
    s.s_world = dev_copy(s_world);
    s.si = dev_copy(si);
    vector<int> pbase(S + 1, 0);
    for (int x = 0; x < S; x++)
        pbase[x + 1] = pbase[x] + deg[H + x];
    const size_t nports = pbase[S];
    s.pbase = dev_copy(pbase);
    s.deg = dev_copy(deg);
    s.out_link = dev_copy(out_link);
    s.in_link = dev_copy(in_link);
    s.link_dst = dev_copy(link_dst);
    s.link_dport = dev_copy(link_dport);
    s.rt = dev_copy(rt);
    s.rtp = dev_copy(rtp);
    // windows (Sim::hz): an entity's is its in-links' shortest delay in slots, without a GPU's
    // NVLink while its server is quiet; under send_lat with DAGs (their messages must not reach a
    // window already run)
    vector<uint8_t> kb(H + S, 1), kq(H + S, 1);
    vector<int> h_srv(H, -1);
    vector<int64_t> ldelay_g(L);
    for (int wi = 0; wi < W; wi++)
        for (int l = 0; l < worlds[wi].L; l++)
            ldelay_g[lbase[wi] + l] = worlds[wi].ldelay[l];
    {
        int64_t cap = kw;
        bool dag = false;
        for (auto &w : worlds)
            dag |= w.dag;
        if (dag)
            cap = std::min<int64_t>(cap, ((int64_t)Cfg{base}.num("SEND_LAT_NS", 3000) - 1) / D);
        cap = std::max<int64_t>(cap, 1);
        for (int g = 0; g < H + S; g++)
        {
            int64_t a = cap, q = cap;
            int nv = 0, srv = -1;
            for (int p = 0; p < deg[g]; p++)
            {
                int l = in_link[(size_t)g * K + p];
                int from = link_dst[l ^ 1];
                int64_t k = std::max<int64_t>(1, ldelay_g[l] / D);
                a = std::min(a, k);
                if (g < H && from >= H && si[from - H].nvs)
                    nv++, srv = from - H;
                else
                    q = std::min(q, k);
            }
            kb[g] = (uint8_t)a;
            kq[g] = (uint8_t)(g < H && nv == 1 ? q : a);
            if (g < H && nv == 1)
                h_srv[g] = srv;
        }
    }
    vector<int> srv_rs_off(S + 1, 0);
    vector<int64_t> srv_rs;
    {
        vector<vector<int64_t>> rs(S);
        for (int f = 0; f < F; f++)
        {
            FlowInfo &x = fi[f];
            x.fast = h_srv[x.src] >= 0 && h_srv[x.src] == h_srv[x.dst];
            if (x.fast && !x.dyn)
                rs[h_srv[x.src]].push_back(x.start / D);
        }
        for (int x = 0; x < S; x++)
        {
            std::sort(rs[x].begin(), rs[x].end());
            srv_rs_off[x + 1] = srv_rs_off[x] + (int)rs[x].size();
            srv_rs.insert(srv_rs.end(), rs[x].begin(), rs[x].end());
        }
    }
    // ranks ahead (rdma_rank): wide switches whose in-links are all long enough; they run single
    // slots (a window would run slots not ranked yet)
    vector<uint8_t> rk_ok(S, 0);
    if (rank_ahead > 0)
        for (int x = 0; x < S; x++)
        {
            if (deg[H + x] <= KSMALL)
                continue;
            bool ok = true;
            for (int p = 0; p < deg[H + x]; p++)
                ok &= ldelay_g[in_link[(size_t)(H + x) * K + p]] >= (rank_ahead + rank_every + 1) * D;
            rk_ok[x] = ok;
            if (ok)
                kb[H + x] = kq[H + x] = 1;
        }
    s.rk_ok = dev_copy(rk_ok);
    s.rank_ahead = rank_ahead;
    s.rank_every = rank_every;
    for (int g = 0; g < H + S; g++)
        s.any_win |= kb[g] > 1 || kq[g] > 1;
    s.wk = dev_copy(kb);
    s.wq = dev_copy(kq);
    s.h_srv = dev_copy(h_srv);
    s.srv_rs_off = dev_copy(srv_rs_off);
    s.srv_rs = dev_copy(srv_rs);
    s.srv_fact = dev_alloc<int>(S);
    s.hz = dev_alloc<int64_t>(H + S);
    s.fi = dev_copy(fi);
    s.root_chain = dev_copy(root_chain);
    {
        // the roots in ccmp_walk's order of their start chains (short, without tails): the first
        // differing lead (the larger first), a chain that ends first, then the root
        auto leads = [](const Chain &c) {
            vector<uint32_t> v;
            for (int i = 0; i < RUNS && c.r[i].code; i++)
                for (uint32_t k = 0; k < c.r[i].cnt; k++)
                    v.push_back(c.r[i].code >> 3);
            return v;
        };
        vector<vector<uint32_t>> L(F);
        vector<int> idx;
        for (int i = 0; i < F; i++)
            if (root_chain[i].root)
            {
                L[i] = leads(root_chain[i]);
                idx.push_back(i);
            }
        std::sort(idx.begin(), idx.end(), [&](int a, int b) {
            const vector<uint32_t> &x = L[a], &y = L[b];
            for (size_t i = 0; i < x.size() && i < y.size(); i++)
                if (x[i] != y[i])
                    return x[i] > y[i];
            if (x.size() != y.size())
                return x.size() < y.size();
            return root_chain[a].root < root_chain[b].root;
        });
        vector<uint32_t> rank(F, 0);
        for (size_t i = 0; i < idx.size(); i++)
            rank[idx[i]] = i;
        s.root_rank = dev_copy(rank);
    }
    // DAG worlds: children, dependencies, host pairs; messages wait send_lat (SEND_LAT_NS, as
    // AS_SEND_LAT), at least a slot
    int nbi = 1;
    for (auto &w : worlds)
        s.dag |= w.dag;
    if (s.dag)
    {
        s.send_lat = (int64_t)Cfg{base}.num("SEND_LAT_NS", 3000);
        if (s.send_lat < D)
        {
            fprintf(stderr, "SEND_LAT_NS (%lld) must be at least the slot (%lld ns)\n", (long long)s.send_lat, (long long)D);
            return 1;
        }
        vector<int> ch_off(F + 1, 0), dep_left(F, 0), wh_base(W), wh_n(W);
        vector<uint32_t> ch_list;
        vector<int64_t> wp_off(W, 0), dep_t(F, -1);
        // traced DAGs: actions by global cause, timers; the ready flows in their hosts' lists
        vector<int> tbase(W + 1, 0);
        for (int wi = 0; wi < W; wi++)
            tbase[wi + 1] = tbase[wi] + worlds[wi].tdelay.size();
        const int T = tbase[W];
        vector<int> act_off(2 * F + T + 1, 0);
        vector<DAct> act;
        vector<int64_t> tdelay;
        vector<Chain> dep_c(F);
        const int dyn_cap = 256;
        vector<int> dyn((size_t)H * dyn_cap), dyn_n(H, 0), srv_fpend(S, 0);
        {
            vector<vector<DAct>> by(2 * F + T);
            for (int wi = 0; wi < W; wi++)
            {
                World &w = worlds[wi];
                s.traced |= w.traced;
                tdelay.insert(tdelay.end(), w.tdelay.begin(), w.tdelay.end());
                if (!w.traced)
                    continue;
                const int Fw = w.fi.size();
                auto glob = [&](uint32_t ch) {
                    return ch & DACT_TIMER ? DACT_TIMER | (uint32_t)(tbase[wi] + (ch & ~DACT_TIMER)) : (uint32_t)(fbase[wi] + ch);
                };
                for (int c = 0; c < (int)w.acts.size(); c++)
                {
                    int g = c < 2 * Fw ? 2 * fbase[wi] + c : 2 * F + tbase[wi] + (c - 2 * Fw);
                    for (auto &a : w.acts[c])
                        by[g].push_back(DAct{glob(a.child), a.ord});
                }
                for (auto &p : w.pre)
                {
                    int g = fbase[wi] + p.flow, h = fi[g].src;
                    dep_t[g] = p.ts;
                    dep_c[g] = p.c;
                    dep_c[g].root += fbase[wi];
                    if (dyn_n[h] >= dyn_cap)
                    {
                        fprintf(stderr, "%s: more than %d flows ready at the start on host %d\n", w.name.c_str(), dyn_cap, h);
                        return 1;
                    }
                    dyn[(size_t)h * dyn_cap + dyn_n[h]++] = g;
                    if (fi[g].fast)
                        srv_fpend[h_srv[h]]++;
                }
            }
            for (int c = 0; c < 2 * F + T; c++)
            {
                act.insert(act.end(), by[c].begin(), by[c].end());
                act_off[c + 1] = act.size();
            }
        }
        vector<PairNic> pnic;
        vector<uint16_t> pcnt;
        for (int wi = 0; wi < W; wi++)
        {
            World &w = worlds[wi];
            wh_base[wi] = hbase[wi];
            wh_n[wi] = w.H;
            wp_off[wi] = pnic.size();
            for (size_t f = 0; f < w.fi.size(); f++)
            {
                int g = fbase[wi] + f;
                if (w.dag)
                {
                    for (int j = w.ch_off[f]; j < w.ch_off[f + 1]; j++)
                        ch_list.push_back((uint32_t)(((w.ch_list[j] >> 1) + fbase[wi]) << 1 | (w.ch_list[j] & 1)));
                    dep_left[g] = w.ndeps[f];
                }
                ch_off[g + 1] = ch_list.size();
            }
            pnic.insert(pnic.end(), w.pnic.begin(), w.pnic.end());
            pcnt.insert(pcnt.end(), w.pcnt.begin(), w.pcnt.end());
        }
        s.ch_off = dev_copy(ch_off);
        s.ch_list = dev_copy(ch_list);
        s.dep_left = dev_copy(dep_left);
        s.dep_t = dev_copy(dep_t);
        s.dep_c = dev_copy(dep_c);
        s.act_off = dev_copy(act_off);
        s.act = dev_copy(act);
        s.tdelay = dev_copy(tdelay);
        s.tchain = dev_alloc<Chain>(T);
        s.ttime = dev_alloc<int64_t>(T);
        s.tnext = dev_alloc<int>(T);
        s.srv_fpend = dev_copy(srv_fpend);
        s.pnic = dev_copy(pnic);
        s.pcnt = dev_copy(pcnt);
        s.wh_base = dev_copy(wh_base);
        s.wh_n = dev_copy(wh_n);
        s.wp_off = dev_copy(wp_off);
        // a message is filed in the slot of the event that fires it (a SendCallback's: the frame's
        // start, a tx earlier), so it arrives at most (send_lat + the longest tx) / D + 2 slots later
        int64_t txmax = 0;
        for (auto &w : worlds)
            for (uint64_t r : w.lbps)
                txmax = std::max(txmax, tx_ns(P.simai, r, w.P.mtu + 128));
        nbi = (int)((s.send_lat + txmax) / D) + 3 + kw; // (a host's window runs ahead by up to kw - 1 slots)
        s.nbi = nbi;
        s.fnbi = fdiv_make(nbi);
        s.dcap = 4096;
        s.dmsg = dev_alloc<DMsg>((size_t)nbi * s.dcap);
        s.dmsg_n = dev_alloc<unsigned int>(nbi);
        s.hhead = dev_alloc<unsigned int>((size_t)H * nbi);
        s.dyn_cap = dyn_cap;
        s.dyn = dev_copy(dyn);
        s.dyn_n = dev_copy(dyn_n);
    }
    s.qp = dev_alloc<SndQP>(F);
    s.qc = dev_alloc<QPChains>(F);
    s.rx = dev_alloc<RcvQP>(F);
    s.start_list = dev_copy(start_list);
    // per NIC: a host's devices in turn (its flow starts in its first device's Host)
    vector<int> nic_off(H + 1, 0);
    for (int h = 0; h < H; h++)
        nic_off[h + 1] = nic_off[h] + deg[h];
    const int NICS = nic_off[H];
    s.nic_off = dev_copy(nic_off);
    {
        vector<int> l1, lm;
        for (int h = 0; h < H; h++)
            (deg[h] == 1 ? l1 : lm).push_back(h);
        s.n1 = l1.size(), s.nm = lm.size();
        s.hlist1 = dev_copy(l1);
        s.hlistm = dev_copy(lm);
        vector<int> ls, lb;
        s.ks = s.kb = 1;
        for (int x = 0; x < S; x++)
        {
            int d = deg[H + x];
            (d <= KSMALL ? ls : lb).push_back(x);
            int &k = d <= KSMALL ? s.ks : s.kb;
            k = std::max(k, d);
        }
        s.ns_s = ls.size(), s.ns_b = lb.size();
        s.slist_s = dev_copy(ls);
        s.slist_b = dev_copy(lb);
        vector<BlockInfo> bs, bb;
        for (int h : lm)
            bs.push_back({h, deg[h], nic_off[h], in_link[(size_t)h * K]});
        for (int h : l1)
            bs.push_back({h, deg[h], nic_off[h], in_link[(size_t)h * K]});
        for (int x : ls)
            bs.push_back({x, deg[H + x], pbase[x], -1});
        for (int x : lb)
            bb.push_back({x, deg[H + x], pbase[x], -1});
        s.bi_s = dev_copy(bs);
        s.bi_b = bb.empty() ? nullptr : dev_copy(bb);
    }
    {
        vector<Host> hs(NICS);
        for (int i = 0; i < NICS; i++)
        {
            hs[i] = Host{};
            hs[i].tc_at = hs[i].ns_at = -1;
            hs[i].ob_last = -(1ll << 16); // (as s.msg_cnt starts: no slot)
            hs[i].due_min = INT64_MAX;
        }
        for (int h = 0; h < H; h++)
        {
            hs[nic_off[h]].next_start = start_off[h];
            hs[nic_off[h]].start_end = start_off[h + 1];
        }
        s.hs = dev_copy(hs);
    }
    s.h_tc = dev_alloc<Chain>(NICS);
    s.h_ns = dev_alloc<Chain>(NICS);
    s.ackq_cap = 1024;
    s.ackq = dev_alloc<Pkt>((size_t)NICS * s.ackq_cap);
    {
        // queue pairs a NIC has open at once (finished ones leave its group lazily)
        vector<int> per(NICS, 0);
        for (auto &x : fi)
            per[nic_off[x.src] + x.snic]++;
        s.qps_cap = std::max(1, *std::max_element(per.begin(), per.end()));
    }
    s.qps = dev_alloc<int>((size_t)NICS * s.qps_cap);
    {
        vector<Switch> sw(S);
        vector<Port> ps(nports);
        vector<PortCfg> pc;
        for (auto &w : worlds)
            pc.insert(pc.end(), w.pcfg.begin(), w.pcfg.end());
        for (int x = 0; x < S; x++)
        {
            int n = deg[H + x];
            uint32_t node = si[x].seed; // the switch's node id in its world
            sw[x] = Switch{};
            for (int p = 0; p < n; p++)
            {
                sw[x].total_hdrm += pc[pbase[x] + p].hdrm_cap;
                sw[x].max_shift = std::max(sw[x].max_shift, pc[pbase[x] + p].shift);
            }
            sw[x].total_rsrv = RESERVE * n;
            for (int p = 0; p < n; p++)
            {
                Port &pt = ps[pbase[x] + p];
                pt = Port{};
                pt.ob_last = -(1ll << 16); // (as s.msg_cnt starts: no slot)
                pt.tc_at = -1;
                // ConfigEcnStream(port, node * SwitchMmu::pCnt + device)
                pt.rng = nsm_rng::stream_state(1, (uint64_t)node * Pw[s_world[x]].ecn_pcnt + (p + 1), 1);
            }
        }
        s.sw = dev_copy(sw);
        s.ps = dev_copy(ps);
        s.pc = dev_copy(pc);
    }
    s.p_tc = dev_alloc<Chain>(nports);
    s.q0_cap = 64;
    s.q3_cap = q3cap;
    s.q0 = dev_alloc<Pkt>((size_t)nports * s.q0_cap);
    s.q3 = dev_alloc<Pkt>((size_t)nports * s.q3_cap);
    // links: rate classes (tx tables), and the rings of arrival slots. A frame starting in slot k
    // arrives by (k + 1) D - 1 + its tx time + the delay, so at most 1 + (txmax + delay - 1) / D
    // slots later; a slot's bucket holds the frames that can arrive in D at one per shortest frame
    vector<uint64_t> rates;
    vector<LinkInfo> li(L);
    size_t nframes = 0, ncnt = 0;
    vector<int> lsend(L, 0);
    for (int g = 0; g < N; g++)
        for (int p = 0; p < deg[g]; p++)
            lsend[out_link[(size_t)g * K + p]] = g;
    int pend_n = 2;
    {
        uint32_t mtu = 0;
        for (auto &w : worlds)
            mtu = std::max(mtu, w.P.mtu);
        for (int wi = 0; wi < W; wi++)
            for (int l = 0; l < worlds[wi].L; l++)
            {
                LinkInfo &x = li[lbase[wi] + l];
                x.delay = worlds[wi].ldelay[l];
                x.bps = worlds[wi].lbps[l];
                size_t r = std::find(rates.begin(), rates.end(), x.bps) - rates.begin();
                if (r == rates.size())
                    rates.push_back(x.bps);
                x.rc = (uint32_t)r;
                int64_t txmax = tx_ns(P.simai, x.bps, mtu + 128), txmin = tx_ns(P.simai, x.bps, 60);
                // (its sender runs ahead by up to its window - 1 slots; a GPU sends over NVLink only
                // in single slots)
                int snd = lsend[lbase[wi] + l], to = link_dst[lbase[wi] + l];
                int ksnd = snd < H && to >= H && si[to - H].nvs ? kb[snd] : std::max(kb[snd], kq[snd]);
                int64_t nb = 2 + (txmax + x.delay - 1) / D + (ksnd - 1);
                // (back to back: ACKs, 60 bytes; a link that sends them in 0 ns, as NVLink at 2880
                // Gb/s, has no bound but its sender's queue; overflows are reported)
                int64_t cap = txmin == 0 ? MAXF : std::min<int64_t>(MAXF, D / txmin + 2);
                if (nb > 4096)
                {
                    fprintf(stderr, "a link's delay (%lld ns) is too long for the slot (%lld ns)\n", (long long)x.delay, (long long)D);
                    return 1;
                }
                x.nb = (uint16_t)nb;
                x.fnb = fdiv_make(nb);
                x.cap = (uint16_t)cap;
                x.moff = nframes;
                x.coff = (uint32_t)ncnt;
                nframes += (size_t)nb * cap;
                ncnt += nb;
                pend_n = std::max(pend_n, (int)nb);
            }
    }
    s.li = dev_copy(li);
    {
        vector<PortStatic> pst(nports);
        vector<PortCfg> pcv;
        for (auto &w : worlds)
            pcv.insert(pcv.end(), w.pcfg.begin(), w.pcfg.end());
        for (int x = 0; x < S; x++)
            for (int p = 0; p < deg[H + x]; p++)
            {
                PortStatic &q = pst[pbase[x] + p];
                int li_in = in_link[(size_t)(H + x) * K + p], li_out = out_link[(size_t)(H + x) * K + p];
                q.link_in = li_in;
                q.link_out = li_out;
                q.in_delay = (int32_t)li[li_in].delay;
                q.out_delay = (int32_t)li[li_out].delay;
                q.kmin = pcv[pbase[x] + p].kmin;
                q.kmax = pcv[pbase[x] + p].kmax;
                q.in_coff = li[li_in].coff;
                q.in_moff = (uint32_t)li[li_in].moff;
                q.in_nb = li[li_in].nb;
                q.in_fnb = fdiv_make(q.in_nb);
                q.in_cap = li[li_in].cap;
            }
        s.pst = dev_copy(pst);
        vector<int> busy(S, 1); // (every switch runs its first slot)
        s.sw_busy = dev_copy(busy);
    }
    s.msg = dev_alloc<LFrame>(nframes);
    s.frank = dev_copy(vector<int64_t>(nframes, -1));
    {
        vector<long long> mc(ncnt, -(1ll << 16));
        s.msg_cnt = dev_copy(mc);
    }
    pend_n = std::max(pend_n, nbi); // (a DAG message arrives send_lat after its event)
    s.pend_n = pend_n;
    s.fpend = fdiv_make(pend_n);
    s.pend = dev_alloc<unsigned int>(pend_n);
    if (W == 1 && rates.size() > 1)
        printf("slot %lld ns, %zu link rates, %.1f MB of link buffers\n", (long long)D, rates.size(), nframes * sizeof(LFrame) / 1e6);
    s.slot = dev_alloc<int64_t>(1);
    s.done_blocks = dev_alloc<unsigned int>(1);
    s.flows_done = dev_alloc<unsigned int>(1);
    s.activity = dev_alloc<unsigned int>(2);
    {
        vector<unsigned long long> nw(1, ~0ull);
        s.next_wake = dev_copy(nw);
    }
    s.finished = dev_alloc<int>(1);
    s.max_slots = max_slots;
    {
        vector<int64_t> fct(F, -1);
        s.fct = dev_copy(fct);
        s.sent = dev_copy(fct);
    }
    if (!tx_file.empty())
    {
        s.tx_cap = 64ull << 20;
        s.tx = dev_alloc<TxRec>(s.tx_cap);
        s.tx_cnt = dev_alloc<unsigned long long>(1);
        s.tx_from = tx_from;
        s.tx_until = tx_until;
    }
    s.ev = dev_alloc<unsigned long long>(40);
    {
        TailPool tp;
        uint64_t cap = 1ull << tail_log2;
        tp.n = dev_alloc<TailNode>(cap);
        tp.cnt = dev_alloc<unsigned long long>(1);
        tp.mask = cap - 1;
        CK(hipMemcpyToSymbol(HIP_SYMBOL(g_tail), &tp, sizeof(tp)));
    }
    {
        Memo m;
        uint64_t cap = 1ull << 22; // 32 MB
        m.t = dev_alloc<unsigned long long>(cap);
        m.mask = cap - 1;
        CK(hipMemcpyToSymbol(HIP_SYMBOL(g_memo), &m, sizeof(m)));
    }
    {
        vector<int32_t> txt(rates.size() * TXT_N);
        for (size_t r = 0; r < rates.size(); r++)
            for (int b = 0; b < TXT_N; b++)
                txt[r * TXT_N + b] = (int32_t)tx_ns(P.simai, rates[r], b);
        CK(hipMemcpyToSymbol(HIP_SYMBOL(c_txt), txt.data(), sizeof(int32_t) * TXT_N));
        s.txt = dev_copy(txt);
        s.R = rates.size();
    }
    // a block (one wave) per host and per switch; a switch keeps its ports in LDS
    s.spread = spread;
    s.serial = serial || spread < SAFE_LANES;
    // LDS: a host's arrival pool; a switch's ports and int workspace, for the most ports of its kernel
    auto per_sw = [&](int k) { return (size_t)k * sizeof(SwPortLds) + sw_ints(k) * sizeof(int); };
    s.apool = dev_alloc<ArrLds>((size_t)S * POOL);
    s.aidx = dev_alloc<int>((size_t)S * 2 * POOL);
    size_t per_h = HPOOL * sizeof(HArr) + (HHINTS + 1) * sizeof(int) + 8 + MAX_DUE * sizeof(Due);
    if (per_sw(s.kb) > 64 * 1024)
    {
        fprintf(stderr, "a switch's workspace (%zu bytes for %d ports) exceeds LDS\n", per_sw(s.kb), s.kb);
        return 1;
    }
    int BS = spread;
    s.host_blocks = H;

    auto t_start = std::chrono::steady_clock::now();
    int fin = 0;
    long long launches = 0;
    // a graph of 64 slots (fewer launch costs); a slot's kernels run side by side (forked from sa)
    // (sb is not used: HIP gives streams hardware queues in turn, and with the default 4 queues sa
    // and sc made one after the other shared one, which ran rdma_slot and rdma_slot_big in turn,
    // 1.5x slower)
    hipStream_t sa, sb, sc;
    CK(hipStreamCreate(&sa));
    CK(hipStreamCreate(&sb));
    CK(hipStreamCreate(&sc));
    (void)sb;
    // ranks ahead: each launch's rdma_rank joins RM launches later (a late one only leaves ties to
    // the switch), round robin over RM streams (1: joins its own launch)
    const int RM = rank_join;
    vector<hipStream_t> sd(RM);
    vector<hipEvent_t> ejd(RM);
    for (int i = 0; i < RM; i++)
    {
        CK(hipStreamCreate(&sd[i]));
        CK(hipEventCreateWithFlags(&ejd[i], hipEventDisableTiming));
    }
    bool ranks = false;
    for (int x = 0; x < S; x++)
        ranks |= rk_ok[x] != 0;
    const int GS = 64;
    const size_t lds = std::max(per_h, per_sw(s.ks)), lds_b = per_sw(s.kb);
    auto slot_k = s.nm ? rdma_slot<true> : rdma_slot<false>;
    auto persist_k = s.nm ? rdma_persist<true> : rdma_persist<false>;
    CK(hipFuncSetAttribute((const void *)slot_k, hipFuncAttributeMaxDynamicSharedMemorySize, (int)lds));
    CK(hipFuncSetAttribute((const void *)rdma_slot_big, hipFuncAttributeMaxDynamicSharedMemorySize, (int)lds_b));
    hipEvent_t ef, ejc;
    CK(hipEventCreateWithFlags(&ef, hipEventDisableTiming));
    CK(hipEventCreateWithFlags(&ejc, hipEventDisableTiming));
    // two graphs when wide switches may rank ahead: with rdma_rank, and without (its kernels and
    // joins cost launches tens of us); the next 64 slots run with it while the last ones' wide
    // switches met ties their prefixes left open (ev[34]: about a chain walk each, on the slot's
    // critical path) at more than one in 4 slots
    auto capture = [&](bool ranks, hipGraphExec_t *ge) {
    hipGraph_t graph;
    CK(hipStreamBeginCapture(sa, hipStreamCaptureModeGlobal));
    for (int i = 0; i < GS; i++)
    {
        if (s.ns_b)
        {
            CK(hipEventRecord(ef, sa));
            CK(hipStreamWaitEvent(sc, ef, 0));
            if (ranks && i % rank_every == 0)
                CK(hipStreamWaitEvent(sd[i / rank_every % RM], ef, 0));
        }
        // (GPUs with the small switches: a kernel per kind cost more in launches than it saved)
        if (s.n1 + s.ns_s + s.nm > 0)
            slot_k<<<s.n1 + s.ns_s + s.nm, BS, lds, sa>>>();
        if (s.ns_b)
        {
            rdma_slot_big<<<s.ns_b, BS, lds_b, sc>>>();
            CK(hipEventRecord(ejc, sc));
            CK(hipStreamWaitEvent(sa, ejc, 0));
        }
        if (ranks && i % rank_every == 0)
        {
            int r = i / rank_every;
            rdma_rank<<<s.ns_b * rank_every, rank_bs, 0, sd[r % RM]>>>();
            CK(hipEventRecord(ejd[r % RM], sd[r % RM]));
            if (r >= RM - 1)
                CK(hipStreamWaitEvent(sa, ejd[(r + 1) % RM], 0));
        }
    }
    if (ranks)
        for (int i = 0; i < RM; i++)
            CK(hipStreamWaitEvent(sa, ejd[i], 0));
    CK(hipStreamEndCapture(sa, &graph));
    CK(hipGraphInstantiate(ge, graph, nullptr, nullptr, 0));
    };
    CK(hipMemcpyToSymbol(HIP_SYMBOL(c_sim), &s, sizeof(Sim))); // (the kernels' Sim: it does not change from here)
    hipGraphExec_t g_plain, g_rank = nullptr;
    capture(false, &g_plain);
    if (ranks)
        capture(true, &g_rank);
    bool use_rank = false;
    unsigned long long open_ties = 0;
    // persistent: when all H + S blocks are resident at once (the barrier needs every one running).
    // Each role's blocks take a share of the GPU (its blocks over those that fit a CU alone); the
    // shares must leave a tenth free.
    // persistent: rdma_slot's blocks, when all are resident at once (the barrier needs every one
    // running) and there are no wide switches (a second kernel could share the first's hardware
    // queue, and never start)
    if (persist)
    {
        const int nb = s.n1 + s.ns_s + s.nm;
        CK(hipFuncSetAttribute((const void *)persist_k, hipFuncAttributeMaxDynamicSharedMemorySize, (int)lds));
        int dev = 0, per_cu = 0;
        hipDeviceProp_t prop;
        CK(hipGetDevice(&dev));
        CK(hipGetDeviceProperties(&prop, dev));
        CK(hipOccupancyMaxActiveBlocksPerMultiprocessor(&per_cu, (const void *)persist_k, BS, lds));
        // (it pays with a few blocks per multiprocessor: with more, waiting on one barrier loses to
        // launches; 2: forced, for tests)
        bool fits = s.ns_b == 0 && (long long)per_cu * prop.multiProcessorCount >= nb;
        persist = fits && (persist == 2 || nb <= 2 * prop.multiProcessorCount);
        if (W == 1 && persist)
            printf("persistent: %d blocks, %d per CU fit on %d CUs\n", nb, per_cu, prop.multiProcessorCount);
    }
    const int PS = 4096; // (slots per persistent launch)
    while (!fin && persist)
    {
        persist_k<<<s.n1 + s.ns_s + s.nm, BS, lds, sa>>>(PS);
        launches += PS;
        CK(cudaGetLastError());
        CK(hipMemcpyAsync(&fin, s.finished, sizeof(int), hipMemcpyDeviceToHost, sa));
        CK(hipStreamSynchronize(sa));
    }
    while (!fin)
    {
        CK(hipGraphLaunch(use_rank ? g_rank : g_plain, sa));
        launches += GS;
        CK(cudaGetLastError());
        unsigned long long ot;
        CK(hipMemcpyAsync(&fin, s.finished, sizeof(int), hipMemcpyDeviceToHost, sa));
        CK(hipMemcpyAsync(&ot, s.ev + 34, sizeof(ot), hipMemcpyDeviceToHost, sa));
        CK(hipStreamSynchronize(sa));
        use_rank = g_rank && (ot - open_ties) * 4 > (unsigned long long)GS;
        open_ties = ot;
    }
    CK(cudaDeviceSynchronize());
    double wall = std::chrono::duration<double>(std::chrono::steady_clock::now() - t_start).count();

    int64_t slot_end;
    unsigned int done;
    CK(cudaMemcpy(&slot_end, s.slot, sizeof(int64_t), cudaMemcpyDeviceToHost));
    CK(cudaMemcpy(&done, s.flows_done, sizeof(unsigned), cudaMemcpyDeviceToHost));
    vector<int64_t> fct(F);
    CK(cudaMemcpy(fct.data(), s.fct, F * sizeof(int64_t), cudaMemcpyDeviceToHost));
    if (s.dag) // the DAG flows' start times
        CK(cudaMemcpy(fi.data(), s.fi, F * sizeof(FlowInfo), cudaMemcpyDeviceToHost));
    unsigned long long ev[40];
    CK(cudaMemcpy(ev, s.ev, sizeof(ev), cudaMemcpyDeviceToHost));
    for (int wi = 0; wi < W; wi++)
    {
        string path = batch_file.empty() ? out_file : out_dir + "/" + worlds[wi].name + ".fct";
        FILE *fo = fopen(path.c_str(), "w");
        if (!fo)
        {
            fprintf(stderr, "cannot write %s\n", path.c_str());
            return 1;
        }
        for (size_t i = 0; i < worlds[wi].fi.size(); i++)
        {
            int g = fbase[wi] + i;
            if (fct[g] >= 0)
                fprintf(fo, "%zu %lld %lld\n", i, (long long)fi[g].start, (long long)fct[g]);
        }
        fclose(fo);
    }
    if (!sent_file.empty() || (!batch_file.empty() && P.simai))
    {
        vector<int64_t> sent(F);
        CK(cudaMemcpy(sent.data(), s.sent, F * sizeof(int64_t), cudaMemcpyDeviceToHost));
        for (int wi = 0; wi < W; wi++)
        {
            string path = batch_file.empty() ? sent_file : out_dir + "/" + worlds[wi].name + ".sent";
            FILE *fo = fopen(path.c_str(), "w");
            if (!fo)
            {
                fprintf(stderr, "cannot write %s\n", path.c_str());
                return 1;
            }
            for (size_t i = 0; i < worlds[wi].fi.size(); i++)
                if (sent[fbase[wi] + i] >= 0)
                    fprintf(fo, "%zu %lld\n", i, (long long)sent[fbase[wi] + i]);
            fclose(fo);
        }
    }
    printf("flows done %u of %d; simulated %.6f s in %.3f s wall (%lld launches)\n", done, F, (double)slot_end * delay * 1e-9,
           wall, launches);
    if (s.traced && W == 1)
    {
        // the run's end: the last timer fired (a timer fired on the host, by main's actions, is
        // earlier than the flows it installs) or the last completion
        vector<int64_t> tt(worlds[0].tdelay.size());
        CK(cudaMemcpy(tt.data(), s.ttime, tt.size() * sizeof(int64_t), cudaMemcpyDeviceToHost));
        int64_t last = 0;
        for (int64_t t : tt)
            last = std::max(last, t);
        for (int f = 0; f < F; f++)
            if (fct[f] >= 0)
                last = std::max(last, fi[f].start + fct[f]);
        printf("traced DAG: its last event at %lld ns\n", (long long)last);
    }
    printf("drops %llu marks %llu pauses %llu resumes %llu nacks %llu | undecided ties %llu, on lost tails %llu | overflows: tx %llu queue %llu ackq %llu qps %llu due %llu | acks for done QPs %llu\n",
           ev[0], ev[1], ev[2], ev[3], ev[11], ev[8], ev[9], ev[4], ev[5], ev[6], ev[7], ev[12], ev[10]);
    if (s.tx)
    {
        unsigned long long n;
        CK(cudaMemcpy(&n, s.tx_cnt, sizeof(n), cudaMemcpyDeviceToHost));
        if (n > s.tx_cap)
        {
            fprintf(stderr, "txtrace: %llu records, %llu kept\n", n, s.tx_cap);
            n = s.tx_cap;
        }
        vector<TxRec> r(n);
        CK(cudaMemcpy(r.data(), s.tx, n * sizeof(TxRec), cudaMemcpyDeviceToHost));
        std::sort(r.begin(), r.end(), [](const TxRec &a, const TxRec &b) {
            return a.ts != b.ts ? a.ts < b.ts : a.node != b.node ? a.node < b.node : a.dev < b.dev;
        });
        FILE *ft = fopen(tx_file.c_str(), "w");
        const char kinds[] = "DANPR";
        for (auto &x : r)
            fprintf(ft, "%lld %d %d %c %u %u %u %u %u %u\n", (long long)x.ts, x.node, x.dev, kinds[x.kind], x.a, x.b, x.sport, x.seq,
                    x.size, x.ecn);
        fclose(ft);
    }
#ifdef RDMA_PROF
    {
        unsigned long long gp[8];
        CK(hipMemcpyFromSymbol(gp, HIP_SYMBOL(g_prof), sizeof(gp)));
        printf("prof: chain comparisons %llu: from the memo %llu, walked %llu (remembered %llu), walk steps %llu (in tails %llu)\n", gp[0], gp[3], gp[1], gp[4], gp[2], gp[5]);
    }
    printf("prof: slowest host thread %.3f Gcycles, slowest switch thread %.3f Gcycles (summed over slots); switch events %llu, "
           "the busiest switch's per slot summed %llu\n",
           ev[18] * 1e-9, ev[19] * 1e-9, ev[20], ev[21]);
    {
        int khz = 0;
        CK(hipDeviceGetAttribute(&khz, hipDeviceAttributeClockRate, 0));
        printf("prof: the slowest wave of each slot sums to %.3f Gcycles = %.3f s at %d MHz (wall %.3f s, %lld launches)\n", ev[23] * 1e-9,
               ev[23] / (khz * 1e3), khz / 1000, wall, launches);
    }
    printf("prof: switch cycles per event: select %.0f, arrival chain %.0f, tc events %.0f, arrival handling %.0f\n", (double)ev[24] / ev[20],
           (double)ev[25] / ev[20], (double)ev[26] / ev[20], (double)ev[27] / ev[20]);
    printf("prof: switch ties %llu, undecided by the prefixes %llu; switch slots: safe %llu, not pooled %llu\n", ev[28], ev[29], ev[31], ev[30]);
#endif
    bool bad = ev[4] || ev[5] || ev[6] || ev[7] || ev[12] || ev[14] || ev[15] || ev[32] || ev[33] || ev[36];
    if (ev[36])
        fprintf(stderr, "error: a persistent launch's blocks did not all run (--persist=0)\n");
    if (ev[14] || ev[15] || ev[32] || ev[33])
        fprintf(stderr, "error: DAG message overflow %llu, ready-flow overflow %llu, late events %llu, inputs to a window already run %llu\n", ev[14], ev[15], ev[32], ev[33]);
    if (bad)
        fprintf(stderr, "warning: capacity overflow, results are not exact\n");
    if (ev[13])
        fprintf(stderr, "warning: %llu data frames reached receivers after their flows' last byte; if their senders had "
                        "completed, ns-3 would answer them with NACKs (not modelled)\n", ev[13]);
    if (ev[8] || ev[9])
        fprintf(stderr, "warning: ties the chains did not decide exactly (%llu undecided, %llu on lost tails; a larger --tail helps)\n",
                ev[8], ev[9]);
    return bad ? 3 : done == (unsigned)F ? 0 : 2; // (2: flows unfinished at the stop time)
}
