// twins-rdma: runs TwiNS's RDMA model (see rdma.h) on a topology file and a flow file, with the
// parameters of an HPCC config file, like validation/hpcc/rdma-ref.cc:
//
//   twins-rdma CONFIG --topo=F --flows=F --out=F [--txtrace=F --from=ns --until=ns]
//              [--rate=100Gbps --delay=1000] [--q3cap=N] [--tail=log2] [--serial] [KEY=VALUE ...]
//   twins-rdma CONFIG --batch=JOBFILE --outdir=DIR [--path=DIR:DIR...] [options] [KEY=VALUE ...]
//
// --batch: every line "name topo flows [KEY=VALUE ...]" of the job file is a world of its own
// (its overrides on top of CONFIG's and the command line's), run side by side with the others;
// world name's results go to DIR/name.fct. The worlds share the link rate and delay.
// --q3cap: frames a switch port's queue holds (default 4096; an overflow is reported)
//
// --tail: the ring of chain tails holds 2^log2 nodes (default 24, 400 MB); ties decided on an
// overwritten tail are reported as "on lost tails"
//
// out: "id start_ns fct_ns" per flow, by id.
#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
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

static void host_push(Chain *c, int64_t lead, int ord) { cpush(c, ccode(lead, ord), Chain(*c)); }

// One network: its parameters, topology, routes and flows, numbered within it.
struct World
{
    string name;
    Params P;
    int H = 0, S = 0, N = 0, L = 0, K = 1;
    vector<int> deg, out_link, in_link, link_dst, link_dport; // [N K] with the world's K
    vector<Route> rt;                                         // [S H]
    vector<FlowInfo> fi;
    vector<int> start_list, start_off;
    int64_t max_slots = 0;
};

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
    {
        // KMAX_MAP n rate k ...: kmin and kmax in KB (ConfigEcn multiplies by 1000)
        auto lookup = [&](const char *key, double *out) {
            if (!cfg.has(key))
                return false;
            const vector<string> &v = config.at(key);
            int n = atoi(v[0].c_str());
            for (int i = 0; i < n; i++)
                if (strtoull(v[1 + 2 * i].c_str(), 0, 10) == P.bps)
                {
                    *out = atof(v[2 + 2 * i].c_str());
                    return true;
                }
            return false;
        };
        double kmin, kmax, pmax;
        if (!lookup("KMIN_MAP", &kmin) || !lookup("KMAX_MAP", &kmax) || !lookup("PMAX_MAP", &pmax))
        {
            fprintf(stderr, "%s: KMIN/KMAX/PMAX_MAP: no entry for %llu bps\n", w.name.c_str(), (unsigned long long)P.bps);
            return false;
        }
        P.kmin = (uint32_t)kmin * 1000;
        P.kmax = (uint32_t)kmax * 1000;
        P.pmax = pmax;
    }
    if (P.cc == 1 && (P.alpha_int < delay || P.dec_int < delay || P.rp_int < delay))
    {
        fprintf(stderr, "%s: DCQCN timer intervals must be at least the link delay\n", w.name.c_str());
        return false;
    }
    if (cfg.u32("GLOBAL_T", 1) != 1)
    {
        fprintf(stderr, "%s: GLOBAL_T 0 is not modelled\n", w.name.c_str());
        return false;
    }
    w.max_slots = (int64_t)(cfg.num("SIMULATOR_STOP_TIME", 10.0) * 1e9) / delay;

    // ---- topology
    int &H = w.H, &S = w.S;
    vector<std::pair<int, int>> links;
    {
        std::ifstream f(topo_file);
        if (!f)
        {
            fprintf(stderr, "cannot open %s\n", topo_file.c_str());
            return false;
        }
        string a, b, x;
        while (f >> x)
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
            }
        }
    }
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
        if (deg[h] != 1)
        {
            fprintf(stderr, "%s: host %d has %d links (one expected)\n", w.name.c_str(), h, deg[h]);
            return false;
        }
    w.out_link.assign((size_t)N * K, -1);
    w.in_link.assign((size_t)N * K, -1);
    w.link_dst.assign(L, 0);
    w.link_dport.assign(L, 0);
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
            nbr[a][b] = pa;
            nbr[b][a] = pb;
        }
    }
    // routes (CalculateRoute: BFS from each host over switches, neighbours by id)
    vector<vector<vector<int>>> next_hop(S, vector<vector<int>>(H)); // [switch][host] -> next nodes
    vector<vector<int64_t>> hop_n(N, vector<int64_t>(H, -1));         // links from node to host
    for (int h = 0; h < H; h++)
    {
        vector<int> q = {h}, dis(N, -1);
        dis[h] = 0;
        for (size_t i = 0; i < q.size(); i++)
        {
            int now = q[i], d = dis[now];
            for (auto &e : nbr[now])
            {
                int next = e.first;
                if (dis[next] < 0)
                {
                    dis[next] = d + 1;
                    if (next >= H)
                        q.push_back(next);
                }
                if (d + 1 == dis[next] && next >= H)
                    next_hop[next - H][h].push_back(now);
            }
        }
        for (int n = 0; n < N; n++)
            hop_n[n][h] = dis[n];
    }
    w.rt.assign((size_t)S * H, Route{});
    for (int x = 0; x < S; x++)
        for (int h = 0; h < H; h++)
        {
            Route &r = w.rt[(size_t)x * H + h];
            if (next_hop[x][h].size() > (size_t)RT_PORTS)
            {
                fprintf(stderr, "%s: switch %d has %zu equal-cost ports towards host %d (at most %d)\n", w.name.c_str(), H + x,
                        next_hop[x][h].size(), h, RT_PORTS);
                return false;
            }
            r.n = next_hop[x][h].size();
            for (int i = 0; i < r.n; i++)
                r.port[i] = nbr[H + x][next_hop[x][h][i]];
        }
    // maxRtt, maxBdp: delay * 2 + txDelay over host pairs (uniform links)
    uint64_t max_rtt = 0, max_bdp = 0;
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
        uint64_t per_hop_tx = (uint64_t)P.mtu * 1000000000ull * 8 / P.bps;
        max_rtt = (uint64_t)far * (uint64_t)delay * 2 + (uint64_t)far * per_hop_tx;
        max_bdp = max_rtt * P.bps / 1000000000ull / 8;
    }
    P.base_rtt = max_rtt;
    P.win = cfg.u32("HAS_WIN", 1) ? (uint32_t)max_bdp : 0;

    // ---- flows (rows in start order, stable; per host pair ports from 10000)
    vector<FlowInfo> &fi = w.fi;
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
        if (x.src >= H || x.dst >= H)
        {
            fprintf(stderr, "%s: a flow between %d and %d: not hosts\n", w.name.c_str(), x.src, x.dst);
            return false;
        }
    vector<int> order(F);
    for (int i = 0; i < F; i++)
        order[i] = i;
    std::stable_sort(order.begin(), order.end(), [&](int a, int b) { return fi[a].start < fi[b].start; });
    {
        std::map<std::pair<int, int>, uint32_t> port;
        // ScheduleFlowInputs runs once per start time and starts that time's flows; each
        // StartApplication descends from it through Node::AddApplication's Application::Start
        int64_t prev_t = 0;
        int prev_n = 0, k = 0;
        for (int i = 0; i < F;)
        {
            int j = i;
            while (j < F && fi[order[j]].start == fi[order[i]].start)
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
                x.start_chain = c;
            }
            prev_t = t;
            prev_n = j - i;
            k++;
            i = j;
        }
    }
    w.start_list.assign(F, 0);
    w.start_off.assign(H + 1, 0);
    for (int i = 0; i < F; i++)
        w.start_off[fi[order[i]].src + 1]++;
    for (int h = 0; h < H; h++)
        w.start_off[h + 1] += w.start_off[h];
    {
        vector<int> pos(w.start_off.begin(), w.start_off.end() - 1);
        for (int i = 0; i < F; i++)
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
    string topo_file, flow_file, out_file = "fct.txt", tx_file, rate_s = "100Gbps", batch_file, out_dir = ".", data_dirs = "";
    int64_t delay = 1000, tx_from = 0, tx_until = INT64_MAX;
    int q3cap = 4096, tail_log2 = 24, spread = 32, serial = 0;
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
            spread = atoi(v.c_str());
        else if (k == "--serial")
            serial = 1;
        else if (k.compare(0, 2, "--") != 0 && eq != string::npos)
            set_override(base, k, v);
        else
        {
            fprintf(stderr, "unknown argument %s\n", argv[i]);
            return 1;
        }
    }
    const uint64_t bps = parse_rate(rate_s);

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
            worlds.emplace_back();
            worlds.back().name = name;
            if (!build_world(worlds.back(), cfg, find_file(topo), find_file(flows), bps, delay))
                return 1;
        }
        if (worlds.empty())
        {
            fprintf(stderr, "%s: no jobs\n", batch_file.c_str());
            return 1;
        }
    }
    const int W = worlds.size();

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
    vector<SwInfo> si(S);
    vector<FlowInfo> fi(F);
    vector<int> start_list(F), start_off(H + 1, 0), h_world(H), s_world(S);
    vector<Params> Pw(W);
    int64_t max_slots = 0;
    for (int wi = 0; wi < W; wi++)
    {
        World &w = worlds[wi];
        Pw[wi] = w.P;
        max_slots = std::max(max_slots, w.max_slots);
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
        for (int x = 0; x < w.S; x++)
        {
            SwInfo &z = si[sbase[wi] + x];
            z.seed = (uint32_t)(w.H + x);
            z.hbase = hbase[wi];
            z.rt_off = rt.size();
            rt.insert(rt.end(), w.rt.begin() + (size_t)x * w.H, w.rt.begin() + (size_t)(x + 1) * w.H);
            s_world[sbase[wi] + x] = wi;
        }
        for (size_t f = 0; f < w.fi.size(); f++)
        {
            FlowInfo x = w.fi[f];
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
            start_list[fbase[wi] + f] = fbase[wi] + w.start_list[f];
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
    s.fi = dev_copy(fi);
    s.qp = dev_alloc<SndQP>(F);
    s.qc = dev_alloc<QPChains>(F);
    s.rx = dev_alloc<RcvQP>(F);
    s.start_list = dev_copy(start_list);
    {
        vector<Host> hs(H);
        for (int h = 0; h < H; h++)
        {
            hs[h] = Host{};
            hs[h].tc_at = hs[h].ns_at = -1;
            hs[h].next_start = start_off[h];
            hs[h].start_end = start_off[h + 1];
        }
        s.hs = dev_copy(hs);
    }
    s.h_tc = dev_alloc<Chain>(H);
    s.h_ns = dev_alloc<Chain>(H);
    s.ackq_cap = 1024;
    s.ackq = dev_alloc<Pkt>((size_t)H * s.ackq_cap);
    {
        // queue pairs a host has open at once (finished ones leave its group lazily)
        vector<int> per(H, 0);
        for (auto &x : fi)
            per[x.src]++;
        s.qps_cap = std::max(1, *std::max_element(per.begin(), per.end()));
    }
    s.qps = dev_alloc<int>((size_t)H * s.qps_cap);
    {
        vector<Switch> sw(S);
        vector<Port> ps(nports);
        uint32_t headroom = (uint32_t)(P.bps * (uint64_t)delay / 8 / 1000000000ull * 3);
        for (int x = 0; x < S; x++)
        {
            int n = deg[H + x];
            uint32_t node = si[x].seed; // the switch's node id in its world
            sw[x] = Switch{};
            sw[x].headroom = headroom;
            sw[x].total_hdrm = headroom * n;
            sw[x].total_rsrv = RESERVE * n;
            for (int p = 0; p < n; p++)
            {
                Port &pt = ps[pbase[x] + p];
                pt = Port{};
                pt.tc_at = -1;
                // ConfigEcnStream(port, node * SwitchMmu::pCnt + device)
                pt.rng = nsm_rng::stream_state(1, (uint64_t)node * 257 + (p + 1), 1);
            }
        }
        s.sw = dev_copy(sw);
        s.ps = dev_copy(ps);
    }
    s.p_tc = dev_alloc<Chain>(nports);
    s.q0_cap = 64;
    s.q3_cap = q3cap;
    s.q0 = dev_alloc<Pkt>((size_t)nports * s.q0_cap);
    s.q3 = dev_alloc<Pkt>((size_t)nports * s.q3_cap);
    s.msg = dev_alloc<LFrame>((size_t)NB * L * MAXF);
    {
        vector<long long> mc((size_t)NB * L, -(1ll << 16));
        s.msg_cnt = dev_copy(mc);
    }
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
    }
    if (!tx_file.empty())
    {
        s.tx_cap = 64ull << 20;
        s.tx = dev_alloc<TxRec>(s.tx_cap);
        s.tx_cnt = dev_alloc<unsigned long long>(1);
        s.tx_from = tx_from;
        s.tx_until = tx_until;
    }
    s.ev = dev_alloc<unsigned long long>(32);
    {
        TailPool tp;
        uint64_t cap = 1ull << tail_log2;
        tp.n = dev_alloc<TailNode>(cap);
        tp.cnt = dev_alloc<unsigned long long>(1);
        tp.mask = cap - 1;
        CK(hipMemcpyToSymbol(HIP_SYMBOL(g_tail), &tp, sizeof(tp)));
    }
    {
        vector<int32_t> txt(TXT_N);
        for (int b = 0; b < TXT_N; b++)
            txt[b] = (int32_t)tx_ns(P.bps, b);
        CK(hipMemcpyToSymbol(HIP_SYMBOL(c_txt), txt.data(), sizeof(int32_t) * TXT_N));
    }
    // a block (one wave) per host and per switch; a switch keeps its ports in LDS
    s.spread = spread;
    s.serial = serial || spread < SAFE_LANES;
    size_t per_sw = (size_t)K * sizeof(SwPortLds) + sw_ints(K) * sizeof(int);
    s.apool = dev_alloc<ArrLds>((size_t)S * POOL);
    s.aidx = dev_alloc<int>((size_t)S * 2 * POOL);
    size_t per_h = HPOOL * sizeof(HArr) + 4 * sizeof(int);
    if (per_sw > 64 * 1024)
    {
        fprintf(stderr, "a switch's workspace (%zu bytes for %d ports) exceeds LDS\n", per_sw, K);
        return 1;
    }
    int BS = spread;
    s.host_blocks = H;

    auto t_start = std::chrono::steady_clock::now();
    int fin = 0;
    long long launches = 0;
    // a graph of 64 slots (fewer launch costs)
    hipStream_t sa;
    CK(hipStreamCreate(&sa));
    const int GS = 64;
    size_t lds = std::max(per_h, per_sw);
    if (lds > 48 * 1024)
        CK(hipFuncSetAttribute((const void *)rdma_slot, hipFuncAttributeMaxDynamicSharedMemorySize, (int)lds));
    hipGraph_t graph;
    hipGraphExec_t gexec;
    CK(hipStreamBeginCapture(sa, hipStreamCaptureModeGlobal));
    for (int i = 0; i < GS; i++)
        rdma_slot<<<H + S, BS, lds, sa>>>(s);
    CK(hipStreamEndCapture(sa, &graph));
    CK(hipGraphInstantiate(&gexec, graph, nullptr, nullptr, 0));
    while (!fin)
    {
        CK(hipGraphLaunch(gexec, sa));
        launches += GS;
        CK(cudaGetLastError());
        CK(hipMemcpyAsync(&fin, s.finished, sizeof(int), hipMemcpyDeviceToHost, sa));
        CK(hipStreamSynchronize(sa));
    }
    CK(cudaDeviceSynchronize());
    double wall = std::chrono::duration<double>(std::chrono::steady_clock::now() - t_start).count();

    int64_t slot_end;
    unsigned int done;
    CK(cudaMemcpy(&slot_end, s.slot, sizeof(int64_t), cudaMemcpyDeviceToHost));
    CK(cudaMemcpy(&done, s.flows_done, sizeof(unsigned), cudaMemcpyDeviceToHost));
    vector<int64_t> fct(F);
    CK(cudaMemcpy(fct.data(), s.fct, F * sizeof(int64_t), cudaMemcpyDeviceToHost));
    unsigned long long ev[32];
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
    printf("flows done %u of %d; simulated %.6f s in %.3f s wall (%lld launches)\n", done, F, (double)slot_end * delay * 1e-9,
           wall, launches);
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
    bool bad = ev[4] || ev[5] || ev[6] || ev[7] || ev[12];
    if (bad)
        fprintf(stderr, "warning: capacity overflow, results are not exact\n");
    if (ev[13])
        fprintf(stderr, "warning: %llu data frames reached receivers after their flows' last byte; if their senders had "
                        "completed, ns-3 would answer them with NACKs (not modelled)\n", ev[13]);
    if (ev[8] || ev[9])
        fprintf(stderr, "warning: ties the chains did not decide exactly (%llu undecided, %llu on lost tails; a larger --tail helps)\n",
                ev[8], ev[9]);
    return done == (unsigned)F && !bad ? 0 : 2;
}
