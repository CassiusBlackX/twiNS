// nvl_island_bench: how fast one CPU core runs a server's NVLink part (docs/ai/speed.md, lever C).
// NOT exact: a sequential event loop with SimAI's NVLink behaviour (QPs paced at line rate within
// their window, an ACK per data frame, the NVSwitch a FIFO per egress port, tx = floor(bits / rate)),
// fed the recorded flows within servers (twins-ai --nvl-record's F.fct: id start fct src dst size).
// Every event carries a chain and pushes onto it, as TwiNS's events do. It reports the events per
// server (to compare with the GPU's counts) and the time per event, with the servers spread over
// P threads.
//
// build: g++ -O3 -march=native -std=c++17 -pthread nvl_island_bench.cc -o nvl_island_bench
// usage: nvl_island_bench FLOWS TOPO THREADS
#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <deque>
#include <fstream>
#include <map>
#include <queue>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

struct Chain
{
    uint32_t code[12];
    uint64_t tail, hash, anc[4];
    uint32_t root, pad;
};
static inline void cpush(Chain &d, uint32_t code, const Chain &p)
{
    d = p;
    if (d.code[0] == code)
        d.code[1]++;
    else
    {
        memmove(d.code + 2, d.code, 40);
        d.code[0] = code;
        d.code[1] = 1;
    }
    d.hash = (p.hash ^ code) * 0x9E3779B97F4A7C15ull;
    for (int i = 3; i > 0; i--)
        d.anc[i] = p.anc[i - 1];
    d.anc[0] = p.hash;
}

enum Kind : uint8_t { START, H_ARR, H_TC, H_NS, S_ARR, S_TC };
struct Frame
{
    int flow;
    uint32_t seq, size;
    uint8_t ack;
    int dst; // host (local index)
};
struct Ev
{
    int64_t t;
    uint64_t n; // insertion order (ties)
    Kind k;
    int ent, port;
    Frame f;
    Chain c;
    bool operator<(const Ev &o) const { return t != o.t ? t > o.t : n > o.n; }
};

struct Flow
{
    int src, dst;
    uint64_t size;
    int64_t start;
};

struct Server
{
    std::vector<Flow> flows;
    int nh = 8;
    // results
    uint64_t events = 0;
    int64_t end = 0;
    double sec = 0;
    void run()
    {
        const double bps = 2880e9;
        const int64_t delay = 25;
        const uint32_t mtu = 9000, hdr = 58, ack_size = 60;
        const uint64_t win = 127000;
        auto tx = [&](uint32_t bytes) { return (int64_t)(bytes * 8e9 / bps); };
        struct QP
        {
            uint64_t size, nxt = 0, una = 0;
            int64_t avail = 0;
            bool done = false;
        };
        std::vector<QP> qp(flows.size());
        std::vector<uint64_t> expected(flows.size(), 0);
        std::vector<std::vector<int>> hq(nh); // a host's QPs
        std::vector<std::deque<Frame>> ackq(nh);
        std::vector<int> rr(nh, 0);
        std::vector<char> hbusy(nh, 0), ns_pending(nh, 0);
        std::vector<std::deque<Frame>> sq(nh); // NVSwitch egress queues (to each host)
        std::vector<char> sbusy(nh, 0);
        std::priority_queue<Ev> pq;
        uint64_t n = 0;
        Chain root = {};
        auto push = [&](int64_t t, Kind k, int ent, int port, const Frame &f, const Chain &par, uint32_t code) {
            Ev e;
            e.t = t;
            e.n = n++;
            e.k = k;
            e.ent = ent;
            e.port = port;
            e.f = f;
            cpush(e.c, code, par);
            pq.push(e);
        };
        for (size_t i = 0; i < flows.size(); i++)
        {
            qp[i].size = flows[i].size;
            push(flows[i].start, START, flows[i].src, 0, Frame{(int)i, 0, 0, 0, 0}, root, 1);
        }
        // the host's egress: an ACK first, else the next QP round robin that may send now
        auto h_send = [&](int h, int64_t now, const Chain &cur) {
            if (hbusy[h])
                return;
            Frame f;
            if (!ackq[h].empty())
            {
                f = ackq[h].front();
                ackq[h].pop_front();
            }
            else
            {
                int m = hq[h].size(), pick = -1;
                int64_t soon = INT64_MAX;
                for (int j = 0; j < m; j++)
                {
                    int q = hq[h][(rr[h] + j) % m];
                    QP &x = qp[q];
                    if (x.done || x.nxt >= x.size || x.nxt - x.una >= win)
                        continue;
                    if (x.avail <= now)
                    {
                        pick = (rr[h] + j) % m;
                        break;
                    }
                    soon = std::min(soon, x.avail);
                }
                if (pick < 0)
                {
                    if (soon != INT64_MAX && !ns_pending[h])
                    {
                        ns_pending[h] = 1;
                        push(soon, H_NS, h, 0, Frame{}, cur, 7);
                    }
                    return;
                }
                rr[h] = (pick + 1) % m;
                int q = hq[h][pick];
                QP &x = qp[q];
                uint32_t pay = (uint32_t)std::min<uint64_t>(mtu, x.size - x.nxt);
                f = Frame{q, (uint32_t)x.nxt, pay + hdr, 0, flows[q].dst};
                x.nxt += pay;
                x.avail = now + tx(pay + hdr);
            }
            hbusy[h] = 1;
            int64_t t = tx(f.size);
            push(now + t, H_TC, h, 0, Frame{}, cur, 3);
            push(now + t + delay, S_ARR, 0, h, f, cur, 5);
        };
        auto s_send = [&](int p, int64_t now, const Chain &cur) {
            if (sbusy[p] || sq[p].empty())
                return;
            Frame f = sq[p].front();
            sq[p].pop_front();
            sbusy[p] = 1;
            int64_t t = tx(f.size);
            push(now + t, S_TC, 0, p, Frame{}, cur, 4);
            push(now + t + delay, H_ARR, p, 0, f, cur, 6);
        };
        auto t0 = std::chrono::steady_clock::now();
        while (!pq.empty())
        {
            Ev e = pq.top();
            pq.pop();
            events++;
            end = e.t;
            switch (e.k)
            {
            case START:
                hq[e.ent].push_back(e.f.flow);
                h_send(e.ent, e.t, e.c);
                break;
            case H_NS:
                ns_pending[e.ent] = 0;
                h_send(e.ent, e.t, e.c);
                break;
            case H_TC:
                hbusy[e.ent] = 0;
                h_send(e.ent, e.t, e.c);
                break;
            case S_ARR:
                sq[e.f.dst].push_back(e.f);
                s_send(e.f.dst, e.t, e.c);
                break;
            case S_TC:
                sbusy[e.port] = 0;
                s_send(e.port, e.t, e.c);
                break;
            case H_ARR:
                if (!e.f.ack)
                {
                    int q = e.f.flow;
                    if (e.f.seq == expected[q])
                        expected[q] += e.f.size - hdr;
                    ackq[e.ent].push_back(Frame{q, (uint32_t)expected[q], ack_size, 1, flows[q].src});
                }
                else
                {
                    QP &x = qp[e.f.flow];
                    if (e.f.seq > x.una)
                        x.una = e.f.seq;
                    if (x.una >= x.size)
                        x.done = true;
                }
                h_send(e.ent, e.t, e.c);
                break;
            }
        }
        sec = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
    }
};

int main(int argc, char **argv)
{
    if (argc < 4)
    {
        fprintf(stderr, "usage: %s FLOWS TOPO THREADS\n", argv[0]);
        return 1;
    }
    // the topology: SimAI's format; a host's server is the NVSwitch it links to at the NVLink rate
    std::ifstream tf(argv[2]);
    std::string line;
    std::getline(tf, line);
    int N, G;
    std::istringstream(line) >> N >> G;
    std::getline(tf, line);
    int H = 0;
    {
        std::istringstream h(line);
        int first;
        h >> first;
        H = first; // the first switch id: hosts come first
    }
    std::map<int, int> srv_of; // host -> NVSwitch
    while (std::getline(tf, line))
    {
        int a, b;
        char rate[64];
        if (sscanf(line.c_str(), "%d %d %63s", &a, &b, rate) == 3 && std::string(rate) == "2880Gbps" && a < H)
            srv_of[a] = b;
    }
    std::map<int, Server> servers;
    std::map<int, std::vector<int>> members;
    for (auto &x : srv_of)
        members[x.second].push_back(x.first);
    std::ifstream ff(argv[1]);
    long long g, st, fct, sz;
    int src, dst;
    while (std::getline(ff, line))
        if (sscanf(line.c_str(), "%lld %lld %lld %d %d %lld", &g, &st, &fct, &src, &dst, &sz) == 6)
        {
            int s = srv_of.at(src);
            auto &m = members[s];
            int ls = std::find(m.begin(), m.end(), src) - m.begin(), ld = std::find(m.begin(), m.end(), dst) - m.begin();
            servers[s].nh = m.size();
            servers[s].flows.push_back(Flow{ls, ld, (uint64_t)sz, st});
        }
    std::vector<Server *> list;
    for (auto &x : servers)
        list.push_back(&x.second);
    const int P = atoi(argv[3]);
    auto t0 = std::chrono::steady_clock::now();
    std::vector<std::thread> th;
    for (int p = 0; p < P; p++)
        th.emplace_back([&, p] {
            for (size_t i = p; i < list.size(); i += P)
                list[i]->run();
        });
    for (auto &t : th)
        t.join();
    double wall = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
    uint64_t ev = 0;
    double cpu = 0;
    int64_t end = 0;
    for (auto *s : list)
        ev += s->events, cpu += s->sec, end = std::max(end, s->end);
    printf("%zu servers, %zu flows; %.1f M events (%.1f M per server), simulated to %.3f ms\n", list.size(),
           [&] { size_t n = 0; for (auto *s : list) n += s->flows.size(); return n; }(), ev / 1e6, ev / 1e6 / list.size(), end / 1e6);
    printf("%d threads: wall %.2f s; %.3f us per event on a core (summed thread time %.2f s)\n", P, wall, cpu / ev * 1e6, cpu);
    return 0;
}
