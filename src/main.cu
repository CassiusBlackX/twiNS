// TwiNS driver: topology (FatTree or leaf-spine), flows, worlds, slot loop, per-flow CSV in the format of the
// ns-3 comparison scenario (validation/ns3/twins-ref.cc): src,dst,flow_size,start_ns,established_ns,completed_ns
#include <algorithm>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <map>
#include <sstream>
#include <string>
#include <type_traits>
#include <vector>

#include "model.h"

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

// the two variants of the model (see step.cuh)
#define TWINS_GENERAL 0
#define TWINS_NS kbasic
#include "step.cuh"
#include "kernel.cuh"
#undef TWINS_GENERAL
#undef TWINS_NS
#define TWINS_GENERAL 1
#define TWINS_NS kfull
#include "step.cuh"
#include "kernel.cuh"
using kfull::Due;


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

// a whole connection on the host (for set-up and reports); the device keeps it as SndRec + RcvRec
struct FlowRec
{
#define NS_REC(T, n, v) T n;
    NS_FLOW_FIELDS(NS_REC)
#undef NS_REC
};

static void split(const std::vector<FlowRec> &r, std::vector<SndRec> &a, std::vector<RcvRec> &b)
{
    a.resize(r.size());
    b.resize(r.size());
    for (size_t i = 0; i < r.size(); i++)
    {
#define NS_CP_A(T, n, v) a[i].n = r[i].n;
#define NS_CP_B(T, n, v) b[i].n = r[i].n;
        NS_STATIC_FIELDS(NS_CP_A)
        NS_SND_FIELDS(NS_CP_A)
        NS_STATIC_FIELDS(NS_CP_B)
        NS_RCV_FIELDS(NS_CP_B)
#undef NS_CP_A
#undef NS_CP_B
    }
}

struct FlowIn
{
    int src, dst;
    int64_t size, start_ns;
    int64_t conn = -1; // connection id from the file (-1: its own connection)
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
        else if (v.size() == 5)
            out.push_back({(int)v[0], (int)v[1], v[2], v[3], v[4]});
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
    std::string topo = "fattree", worlds_file, topo_file, dump_routes, fail_spec;
    int ecn_k = RED_K, queue_pkts = QDISC_CAP, fin = 0, spray = 0;
    long long rng_seed = 1, rng_run = 1;
    bool force_general = false;
    bool no_compact = false; // --general: the general model (sent list, kernel) for everything (tests)
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
        else if (a.rfind("--topofile=", 0) == 0)
            topo_file = val("--topofile=");
        else if (a.rfind("--fail=", 0) == 0)
            fail_spec = val("--fail=");
        else if (a.rfind("--dump_routes=", 0) == 0)
            dump_routes = val("--dump_routes=");
        else if (a.rfind("--leaves=", 0) == 0)
            leaves = std::stoi(val("--leaves="));
        else if (a.rfind("--spines=", 0) == 0)
            spines = std::stoi(val("--spines="));
        else if (a.rfind("--hpl=", 0) == 0)
            hpl = std::stoi(val("--hpl="));
        else if (a.rfind("--fin=", 0) == 0)
            fin = std::stoi(val("--fin="));
        else if (a == "--general")
            no_compact = true;
        else if (a.rfind("--spray=", 0) == 0)
            spray = std::stoi(val("--spray="));
        else if (a.rfind("--RngSeed=", 0) == 0)
            rng_seed = std::stoll(val("--RngSeed="));
        else if (a.rfind("--RngRun=", 0) == 0)
            rng_run = std::stoll(val("--RngRun="));
        else if (a.rfind("--graph_k=", 0) == 0)
            graph_k = std::stoi(val("--graph_k="));
        else
        {
            fprintf(stderr, "unknown argument %s\n", a.c_str());
            return 1;
        }
    }
    bool ls = topo == "leafspine", is_graph = topo == "graph";
    // a topology file: hosts, switches, links in installation order (workloads/topo.py)
    int g_hosts = 0, g_switches = 0;
    std::vector<std::pair<int, int>> g_links; // endpoints: host h -> h, switch j -> -1 - j
    if (is_graph)
    {
        std::ifstream in(topo_file);
        if (!in)
        {
            fprintf(stderr, "cannot open %s\n", topo_file.c_str());
            return 1;
        }
        std::string key;
        auto node = [](const std::string &x) { return x[0] == 'h' ? std::stoi(x.substr(1)) : -1 - std::stoi(x.substr(1)); };
        while (in >> key)
        {
            if (key == "hosts")
                in >> g_hosts;
            else if (key == "switches")
                in >> g_switches;
            else if (key == "link")
            {
                std::string a, b;
                in >> a >> b;
                g_links.push_back({node(a), node(b)});
            }
            else
                std::getline(in, key);
        }
    }
    bool bad_topo = is_graph ? (g_hosts < 1 || g_switches < 1)
                    : ls  ? (leaves < 1 || spines < 1 || hpl < 1 || std::max(hpl + spines, leaves) > BLOCK || leaves * hpl > 65535)
                          : (topo != "fattree" || k < 4 || k > 32 || (k & (k - 1)));
    if ((flow_file.empty() && worlds_file.empty()) || bad_topo || (tcp != "dctcp" && tcp != "newreno" && tcp != "cubic"))
    {
        fprintf(stderr, "usage: twins (--k=<4..32> | --topo=leafspine --leaves=L --spines=S --hpl=N | --topo=graph --topofile=F)\n"
                        "         (--flows=<csv> [--out=csv] [--tcp=dctcp|newreno|cubic] [--ecnK=30] [--queue=200] [--fin=0|1]\n"
                        "          [--ecmp_from=csv] [--spray=1 [--RngSeed=1 --RngRun=1]]\n"
                        "          | --worlds=<file>: one world per line, flows=..,tcp=..,ecnK=..,queue=..,fin=..,ecmp_from=..,out=..)\n"
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
        int fin = 0;
    };
    auto cc_of = [](const std::string &t) -> int { return t == "dctcp" ? CC_DCTCP : t == "newreno" ? CC_NEWRENO : t == "cubic" ? CC_CUBIC : -1; };
    std::vector<World> worlds;
    if (worlds_file.empty())
        worlds.push_back({flow_file, ecmp_from, output, (uint8_t)cc_of(tcp), ecn_k, queue_pkts, fin});
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
            w.fin = fin;
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
                else if (key == "fin")
                    w.fin = std::stoi(v);
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
    // graph: the ports of each node in link order, the hosts' switches, the equal-cost tables
    std::vector<int> g_deg, g_hsw, g_hport;
    std::vector<std::vector<std::pair<int, int>>> g_adj; // switch -> (port, neighbour)
    std::vector<uint32_t> g_rtoff;
    std::vector<uint8_t> g_rtn, g_rtports, g_portdn;
    std::vector<int64_t> g_failt;
    if (is_graph)
    {
        g_deg.assign(g_switches, 0);
        g_hsw.assign(g_hosts, -1);
        g_hport.assign(g_hosts, 0);
        g_adj.assign(g_switches, {});
        for (auto &l : g_links)
        {
            if (l.second >= 0 || (l.first >= 0 && g_hsw[l.first] >= 0))
            {
                fprintf(stderr, "%s: hosts need exactly one link, as its first endpoint\n", topo_file.c_str());
                return 1;
            }
            int b = -1 - l.second, pb = g_deg[b]++;
            if (l.first >= 0)
            {
                g_hsw[l.first] = b;
                g_hport[l.first] = pb;
            }
            else
            {
                int a = -1 - l.first, pa = g_deg[a]++;
                g_adj[a].push_back({pa, b});
                g_adj[b].push_back({pb, a});
            }
        }
        for (int h = 0; h < g_hosts; h++)
            if (g_hsw[h] < 0)
            {
                fprintf(stderr, "%s: host %d has no link\n", topo_file.c_str(), h);
                return 1;
            }
        k = 0;
        for (int d : g_deg)
            k = std::max(k, d);
        if (k > BLOCK)
        {
            fprintf(stderr, "%s: a switch has %d ports (at most %d)\n", topo_file.c_str(), k, BLOCK);
            return 1;
        }
        // link failures: T:sA:sB[:up], the first link between two switches; epoch i follows the
        // i-th event in time order
        struct Fail
        {
            int64_t t;
            int a, pa, b, pb;
            bool up;
        };
        std::vector<Fail> fails;
        for (std::stringstream fs(fail_spec); fs.good();)
        {
            std::string item;
            std::getline(fs, item, ',');
            if (item.empty())
                continue;
            std::vector<std::string> v;
            std::stringstream is(item);
            for (std::string x; std::getline(is, x, ':');)
                v.push_back(x);
            if (v.size() < 3)
            {
                fprintf(stderr, "--fail: T:sA:sB[:up]\n");
                return 1;
            }
            Fail fl{std::stoll(v[0]) * 1000, std::stoi(v[1].substr(1)), -1, std::stoi(v[2].substr(1)), -1, v.size() > 3 && v[3] == "up"};
            for (auto &pn : g_adj[fl.a])
                if (pn.second == fl.b && fl.pa < 0)
                    fl.pa = pn.first;
            for (auto &pn : g_adj[fl.b])
                if (pn.second == fl.a && fl.pb < 0)
                    fl.pb = pn.first;
            if (fl.pa < 0)
            {
                fprintf(stderr, "--fail: no link between s%d and s%d\n", fl.a, fl.b);
                return 1;
            }
            fails.push_back(fl);
        }
        std::stable_sort(fails.begin(), fails.end(), [](const Fail &x, const Fail &y) { return x.t < y.t; });
        int NE = (int)fails.size() + 1;
        s.n_fail = NE - 1;
        std::vector<int64_t> fail_t;
        for (auto &fl : fails)
            fail_t.push_back(fl.t);
        // shortest paths (hop count) to every switch e: from x, the ports towards a neighbour one
        // hop closer, in interface (port) order, as Ipv4GlobalRouting installs equal-cost routes;
        // per epoch, without the links that are down
        g_rtoff.assign((size_t)NE * g_switches * g_switches, 0);
        g_rtn.assign((size_t)NE * g_switches * g_switches, 0);
        g_portdn.assign((size_t)NE * g_switches * k, 0);
        std::vector<uint8_t> dn((size_t)g_switches * k, 0); // port down now
        for (int ep = 0; ep < NE; ep++)
        {
            if (ep > 0)
            {
                const Fail &fl = fails[ep - 1];
                dn[(size_t)fl.a * k + fl.pa] = dn[(size_t)fl.b * k + fl.pb] = fl.up ? 0 : 1;
            }
            std::copy(dn.begin(), dn.end(), g_portdn.begin() + (size_t)ep * g_switches * k);
            for (int e = 0; e < g_switches; e++)
            {
                std::vector<int> dist(g_switches, -1), todo{e};
                dist[e] = 0;
                for (size_t i = 0; i < todo.size(); i++)
                    for (auto &pn : g_adj[todo[i]])
                        if (!dn[(size_t)todo[i] * k + pn.first] && dist[pn.second] < 0)
                        {
                            dist[pn.second] = dist[todo[i]] + 1;
                            todo.push_back(pn.second);
                        }
                for (int x = 0; x < g_switches; x++)
                {
                    if (x == e)
                        continue;
                    if (dist[x] < 0)
                    {
                        fprintf(stderr, "%s: switch %d cannot reach switch %d (epoch %d)\n", topo_file.c_str(), x, e, ep);
                        return 1;
                    }
                    std::vector<int> ports;
                    for (auto &pn : g_adj[x])
                        if (!dn[(size_t)x * k + pn.first] && dist[pn.second] == dist[x] - 1)
                            ports.push_back(pn.first);
                    std::sort(ports.begin(), ports.end());
                    size_t r = ((size_t)ep * g_switches + x) * g_switches + e;
                    g_rtoff[r] = (uint32_t)g_rtports.size();
                    g_rtn[r] = (uint8_t)ports.size();
                    for (int p : ports)
                        g_rtports.push_back((uint8_t)p);
                }
            }
        }
        g_failt = fail_t;
        if (!dump_routes.empty())
        {
            // in the format of the scenario's DUMP_ROUTES: ns-3 node, host, output interfaces
            FILE *o = fopen(dump_routes.c_str(), "w");
            for (int x = 0; x < g_switches; x++)
                for (int h = 0; h < g_hosts; h++)
                {
                    fprintf(o, "route %d %d", g_hosts + x, h);
                    int e = g_hsw[h];
                    if (e == x)
                        fprintf(o, " %d", g_hport[h] + 1);
                    else
                    {
                        size_t r = (size_t)x * g_switches + e;
                        for (int i = 0; i < g_rtn[r]; i++)
                            fprintf(o, " %d", g_rtports[g_rtoff[r] + i] + 1);
                    }
                    fprintf(o, "\n");
                }
            fclose(o);
        }
        s.topo = TOPO_GRAPH;
        s.H1 = g_hosts;
        s.S1 = g_switches;
        s.ecmp_n = 1;
    }
    else if (ls)
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

    // ---- flows: messages (rows of the flow files) over connections. A connection is a row
    // without a conn column, or the rows of one conn id (same hosts, later starts, in file order).
    std::vector<FlowIn> flows; // messages, by world in file order
    std::vector<int> wfirst(W + 1), cfirst(W + 1);
    std::vector<int> msg_conn;                     // message -> connection
    std::vector<std::vector<int>> conn_msgs;       // connection -> messages
    for (int w = 0; w < W; w++)
    {
        wfirst[w] = (int)flows.size();
        cfirst[w] = (int)conn_msgs.size();
        std::map<int64_t, int> by_id;
        for (FlowIn f : load_flows(worlds[w].flows, H1))
        {
            f.src += w * H1;
            f.dst += w * H1;
            int m = (int)flows.size(), c;
            if (f.conn < 0 || !by_id.count(f.conn))
            {
                c = (int)conn_msgs.size();
                conn_msgs.push_back({});
                if (f.conn >= 0)
                    by_id[f.conn] = c;
            }
            else
            {
                c = by_id[f.conn];
                const FlowIn &first = flows[conn_msgs[c][0]];
                if (first.src != f.src || first.dst != f.dst || f.start_ns < flows[conn_msgs[c].back()].start_ns)
                {
                    fprintf(stderr, "%s: the flows of connection %lld need the same hosts and later starts\n",
                            worlds[w].flows.c_str(), (long long)f.conn);
                    return 1;
                }
            }
            conn_msgs[c].push_back(m);
            msg_conn.push_back(c);
            flows.push_back(f);
        }
    }
    wfirst[W] = (int)flows.size();
    cfirst[W] = (int)conn_msgs.size();
    int M = (int)flows.size(), F = (int)conn_msgs.size();
    s.M = M;
    s.F = F;
    // messages renumbered so that each connection's are consecutive (in its order)
    std::vector<int> mnew(M), mold;
    for (int c = 0; c < F; c++)
        for (int m : conn_msgs[c])
        {
            mnew[m] = (int)mold.size();
            mold.push_back(m);
        }
    // per-host queues of application events in ns-3's order at equal times: message writes
    // (scheduled in main) before connection starts (scheduled as the nodes initialize)
    std::vector<int> order(M);
    for (int i = 0; i < M; i++)
        order[i] = mnew[i];
    auto is_first = [&](int i) { return conn_msgs[msg_conn[i]][0] == i; };
    {
        std::vector<int> byold(M);
        for (int i = 0; i < M; i++)
            byold[i] = i;
        std::stable_sort(byold.begin(), byold.end(), [&](int a, int b) {
            if (flows[a].src != flows[b].src)
                return flows[a].src < flows[b].src;
            if (flows[a].start_ns != flows[b].start_ns)
                return flows[a].start_ns < flows[b].start_ns;
            bool fa = is_first(a), fb = is_first(b);
            if (fa != fb)
                return !fa;
            return fa ? msg_conn[a] < msg_conn[b] : a < b;
        });
        for (int i = 0; i < M; i++)
            order[i] = mnew[byold[i]];
    }
    std::vector<int> pend_next(H, 0), pend_end(H, 0);
    {
        int i = 0;
        for (int h = 0; h < H; h++)
        {
            pend_next[h] = i;
            while (i < M && flows[mold[order[i]]].src == h)
                i++;
            pend_end[h] = i;
        }
    }
    {
        std::vector<int> conn(M), mfw(W);
        std::vector<uint32_t> size(M), end(M);
        std::vector<int64_t> start(M), neg(M, -1);
        for (int c = 0; c < F; c++)
        {
            uint64_t off = 0;
            for (int m : conn_msgs[c])
            {
                off += flows[m].size;
                if (off >= 0xFFFFFFFFull)
                {
                    fprintf(stderr, "connection %d carries 4 GB or more\n", c);
                    return 1;
                }
                int j = mnew[m];
                conn[j] = c;
                size[j] = (uint32_t)flows[m].size;
                end[j] = (uint32_t)off;
                start[j] = flows[m].start_ns * 1000;
            }
        }
        for (int w = 0; w < W; w++)
            mfw[w] = wfirst[w] == M ? M : mnew[wfirst[w]];
        s.mg.conn = dupload(conn);
        s.mg.size = dupload(size);
        s.mg.end = dupload(end);
        s.mg.start = dupload(start);
        s.mg.est = dupload(neg);
        s.mg.done = dupload(neg);
        s.mg_first_of_world = dupload(mfw);
    }
    // ---- connection state
    std::vector<FlowRec> recs(F);
    for (auto &r : recs)
    {
#define NS_INIT(T, n, v) r.n = (T)(v);
        NS_FLOW_FIELDS(NS_INIT)
#undef NS_INIT
    }
    std::vector<uint64_t> conn_bytes(F, 0);
    {
        for (int c = 0; c < F; c++)
        {
            const FlowIn &first = flows[conn_msgs[c][0]];
            recs[c].src = first.src;
            recs[c].dst = first.dst;
            recs[c].m_first = mnew[conn_msgs[c][0]];
            recs[c].m_cnt = (int)conn_msgs[c].size();
            recs[c].fin_on = (uint8_t)(worlds[first.src / H1].fin != 0);
            for (int m : conn_msgs[c])
                recs[c].r_size += (uint32_t)flows[m].size;
            recs[c].simple = (uint8_t)(conn_msgs[c].size() == 1 && !no_compact);
            force_general |= no_compact;
            for (int m : conn_msgs[c])
                conn_bytes[c] += flows[m].size;
        }
        // ECMP hashes: from an ns-3 run of the same flows (identical paths; a connection's rows
        // carry its hashes), or a hash of the connection id and direction (statistically equivalent)
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
            int i = wfirst[w], n = wfirst[w + 1];
            while (std::getline(in, line) && i < n)
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
                if (is_first(i))
                {
                    recs[msg_conn[i]].hash_data = (uint32_t)std::stoul(v[6]);
                    recs[msg_conn[i]].hash_ack = (uint32_t)std::stoul(v[7]);
                }
                i++;
            }
            if (i != n)
            {
                fprintf(stderr, "%s: %d rows for %d flows\n", ecmp_from.c_str(), i - wfirst[w], n - wfirst[w]);
                exit(1);
            }
        }
    }
    // per connection: the sent list and the RTT history (one entry per segment in flight; a
    // connection's segments are at most its bytes / MSS plus one per message), and the receiver's
    // out-of-order blocks (at most one per two segments), each capped (overflows: rtt-ovf, ooo-ovf)
    if (ooo_segs <= 0)
        ooo_segs = 512;
    if (rtt_ring <= 0)
        rtt_ring = 65536;
    {
        std::vector<uint64_t> ioff(F), ooff(F);
        std::vector<uint32_t> icap(F), ocap(F);
        uint64_t it = 0, ot = 0;
        for (int i = 0; i < F; i++)
        {
            uint64_t segs = (conn_bytes[i] + MSS - 1) / MSS + recs[i].m_cnt + 1;
            icap[i] = (uint32_t)std::min<uint64_t>(segs, rtt_ring);
            // blocks (the general receiver), or bitmap words (the lean one: up to 32768 segments)
            ocap[i] = (uint32_t)std::max(std::min<uint64_t>(segs / 2 + 2, ooo_segs), std::min<uint64_t>((segs + 31) / 32, 1024));
            ioff[i] = it;
            ooff[i] = ot;
            it += icap[i];
            ot += ocap[i];
        }
        for (int i = 0; i < F; i++)
        {
            recs[i].it_off = ioff[i];
            recs[i].it_cap = icap[i];
            recs[i].oo_off = ooff[i];
            recs[i].oo_cap = ocap[i];
        }
        s.fl.it_start = dalloc<uint32_t>(it);
        s.fl.it_fl = dalloc<uint8_t>(it);
        s.fl.h_seq = dalloc<uint32_t>(it);
        s.fl.h_cnt = dalloc<uint32_t>(it);
        s.fl.h_t = dalloc<int64_t>(it);
        s.fl.oo_s = dalloc<uint32_t>(ot);
        s.fl.oo_e = dalloc<uint32_t>(ot);
    }
    {
        std::vector<SndRec> sr;
        std::vector<RcvRec> rr;
        split(recs, sr, rr);
        s.fl.snd = dupload(sr);
        s.fl.rcv = dupload(rr);
    }
    // the global view: static and sender fields in the sender records, receiver fields in the
    // receiver records
#define NS_BASE_S(T, n, v) s.fl.n.base = reinterpret_cast<char *>(s.fl.snd) + offsetof(SndRec, n);
#define NS_BASE_R(T, n, v) s.fl.n.base = reinterpret_cast<char *>(s.fl.rcv) + offsetof(RcvRec, n);
    NS_STATIC_FIELDS(NS_BASE_S)
    NS_SND_FIELDS(NS_BASE_S)
    NS_RCV_FIELDS(NS_BASE_R)
#undef NS_BASE_S
#undef NS_BASE_R
    s.fl.dchain = dalloc<uint16_t>((size_t)F * CHAIN);
    s.fl.rchain = dalloc<uint16_t>((size_t)F * CHAIN);
    s.fl.schain = dalloc<uint16_t>((size_t)F * CHAIN);
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
    s.hs.pend_msg = dupload(order);
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
        if (is_graph)
        {
            for (int h = 0; h < H1; h++)
            {
                host_in[hb + h] = sw_link(sb + g_hsw[h], g_hport[h]);
                sw_in[(size_t)(sb + g_hsw[h]) * k + g_hport[h]] = hb + h;
            }
            for (int x = 0; x < S1; x++)
                for (auto &pn : g_adj[x])
                {
                    // the neighbour's port on this link: the one whose neighbour is x, in order
                    int y = pn.second, nth = 0;
                    for (auto &q : g_adj[x])
                        if (q.second == y && q.first < pn.first)
                            nth++;
                    for (auto &q : g_adj[y])
                        if (q.second == x && nth-- == 0)
                        {
                            sw_in[(size_t)(sb + x) * k + pn.first] = sw_link(sb + y, q.first);
                            break;
                        }
                }
            continue;
        }
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
    if (is_graph)
    {
        std::vector<uint8_t> hp(g_hport.begin(), g_hport.end());
        s.host_sw = dupload(g_hsw);
        s.host_port = dupload(hp);
        s.rt_off = dupload(g_rtoff);
        s.rt_n = dupload(g_rtn);
        s.rt_ports = dupload(g_rtports);
        s.port_dn = dupload(g_portdn);
        s.fail_t = dupload(g_failt);
    }
    else if (!fail_spec.empty())
    {
        fprintf(stderr, "--fail needs --topo=graph\n");
        return 1;
    }
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
    s.ev = dalloc<unsigned long long>(32);
    s.ties_w = dalloc<unsigned long long>(W);
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
    if (M == 0)
        *h_finished = 1;
    Due *due = dalloc<Due>((size_t)H * MAX_DUE);
    size_t per_sw = (size_t)k * 2 * MAX_TX;
    int64_t *sw_ts = dalloc<int64_t>((size_t)S * per_sw);
    uint32_t *sw_ref = dalloc<uint32_t>((size_t)S * per_sw);
    uint8_t *sw_up = spray ? dalloc<uint8_t>((size_t)S * per_sw) : nullptr;
    s.sw_rng = nullptr;
    if (spray)
    {
        // per-packet ECMP: the scenario gives node i's Ipv4GlobalRouting the stream i; switches are
        // nodes H1.. of their world
        std::vector<nsm_rng::State> st(S);
        for (int sw = 0; sw < S; sw++)
            st[sw] = nsm_rng::stream_state((uint32_t)rng_seed, (uint64_t)(H1 + sw % S1), (uint64_t)rng_run);
        s.sw_rng = dupload(st);
    }

    // the lean kernel unless a general feature is needed
    bool basic = !is_graph && !spray && !force_general;
    for (int c = 0; c < F && basic; c++)
        basic = recs[c].simple && !recs[c].fin_on;
    if (getenv("TWINS_VERBOSE"))
        printf("kernel: %s\n", basic ? "basic" : "general");
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
        if (basic)
            kbasic::ns_slot<<<grid, BLOCK, 0, stream>>>(reinterpret_cast<kbasic::Due *>(due), sw_ts, sw_ref, sw_up);
        else
            kfull::ns_slot<<<grid, BLOCK, 0, stream>>>(due, sw_ts, sw_ref, sw_up);
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
    std::vector<unsigned long long> ev = ddown(s.ev, 32);
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
           " | retx by state open %llu disorder %llu cwr %llu recovery %llu loss %llu | max-inflight %llu segs rtt-ovf %llu ring-ovf %llu"
           " | undecided-ties %llu\n",
           M, fd, sec, ev[0], ev[1], ev[2], ev[3], ev[4], ev[5], ev[6], ev[7], ev[8], ev[9], ev[10], ev[11], ev[12], ev[13], ev[14], ev[19], ev[25]);
    if (ev[5] || ev[6] || ev[7] || ev[14] || ev[19])
        fprintf(stderr, "warning: a capacity overflowed (active/ooo/due/rtt/ring-ovf); the results may differ from ns-3\n");
    if (ev[25])
        fprintf(stderr, "warning: %llu ties between simultaneous events were undecided by their recorded histories (%d runs); "
                        "the results may differ from ns-3: rerun with twins128 (longer histories)\n",
                ev[25], CHAIN_RUNS);

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
    std::vector<FlowRec> out_rec(F);
    {
        std::vector<SndRec> sr = ddown(s.fl.snd, F);
        std::vector<RcvRec> rr = ddown(s.fl.rcv, F);
        for (int i = 0; i < F; i++)
        {
#define NS_CP_A(T, n, v) out_rec[i].n = sr[i].n;
#define NS_CP_B(T, n, v) out_rec[i].n = rr[i].n;
            NS_STATIC_FIELDS(NS_CP_A)
            NS_SND_FIELDS(NS_CP_A)
            NS_RCV_FIELDS(NS_CP_B)
#undef NS_CP_A
#undef NS_CP_B
        }
    }
    auto field = [&](auto member) {
        std::vector<std::remove_reference_t<decltype(out_rec[0].*member)>> v(F);
        for (int i = 0; i < F; i++)
            v[i] = out_rec[i].*member;
        return v;
    };
    std::vector<int64_t> est = ddown(s.mg.est, M), done = ddown(s.mg.done, M);
    if (fd < (unsigned)M)
    {
        auto st = field(&FlowRec::st), cong = field(&FlowRec::cong);
        auto una = field(&FlowRec::una), nxt = field(&FlowRec::nxt), high = field(&FlowRec::high), cwnd = field(&FlowRec::cwnd);
        auto ssth = field(&FlowRec::ssthresh), rcv = field(&FlowRec::rcv_nxt), dup = field(&FlowRec::dupacks), sk = field(&FlowRec::sacked_n);
        auto rto_at = field(&FlowRec::rto_at);
        auto tail = field(&FlowRec::tail), items = field(&FlowRec::it_n), rec = field(&FlowRec::recover);
        auto mdn = field(&FlowRec::m_dn), mcnt = field(&FlowRec::m_cnt);
        auto nsnd = ddown(s.hs.n_snd, H), nrcv = ddown(s.hs.n_rcv, H);
        int shown = 0;
        for (int c = 0; c < F && shown < 6; c++)
            if (mdn[c] < mcnt[c])
            {
                printf("stuck connection %d %d->%d messages %d/%d tail %u st %d cong %d una %u nxt %u high %u items %u cwnd %u ssth %u dup %u "
                       "sacked %u recover %u rto_at %lld rcv_nxt %u | n_snd %d n_rcv %d\n",
                       c, recs[c].src, recs[c].dst, mdn[c], mcnt[c], tail[c], st[c], cong[c], una[c], nxt[c], high[c], items[c], cwnd[c],
                       ssth[c], dup[c], sk[c], rec[c], (long long)rto_at[c], rcv[c], nsnd[recs[c].src], nrcv[recs[c].dst]);
                shown++;
            }
    }
    for (int w = 0; w < W; w++)
    {
        std::ofstream o(worlds[w].out);
        o << "src,dst,flow_size,start_ns,established_ns,completed_ns\n";
        for (int i = wfirst[w]; i < wfirst[w + 1]; i++)
        {
            int j = mnew[i];
            o << flows[i].src - w * H1 << "," << flows[i].dst - w * H1 << "," << flows[i].size << "," << flows[i].start_ns << ","
              << (est[j] >= 0 ? est[j] / 1000 : -1) << "," << (done[j] >= 0 ? done[j] / 1000 : -1) << "\n";
        }
    }
    if (W > 1)
    {
        printf("worlds %d\n", W);
        if (ev[25])
        {
            // the worlds to rerun with twins128
            std::vector<unsigned long long> tw = ddown(s.ties_w, W);
            printf("undecided-ties by world:"); // (bench/sweep.py reruns these with twins128)
            for (int w = 0; w < W; w++)
                if (tw[w])
                    printf(" %d", w);
            printf("\n");
        }
    }
    return 0;
}
