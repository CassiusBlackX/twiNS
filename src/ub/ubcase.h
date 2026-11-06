// Reading an ns-3-ub case directory (scratch/README_en.md "Case directory"): node.csv,
// topology.csv, routing_table.csv, traffic.csv, network_attribute.txt. Only the attribute subset
// the model implements is accepted; anything else that is set to a non-default value stops the run.
#pragma once
#include "ubtime.h"
#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <fstream>
#include <map>
#include <set>
#include <sstream>
#include <string>
#include <vector>

namespace ub {

struct NodeCfg {
    bool sw = false;
    int ports = 0;
    int64_t alloc_ps = 5000;   // UbSwitchAllocator::AllocationTime
    int64_t fwd_ps = 0;        // UbSwitch::InPortProcessingDelay
};

struct LinkCfg {
    int n1, p1, n2, p2;
    uint64_t bps;
    int64_t delay_ps;
};

struct RouteCfg {                // one (node, dstNode, dstPort) entry, ns-3's next-hop index form
    std::vector<uint16_t> shortest, other;
};

struct TaskCfg {
    int id, src, dst;
    uint64_t size;
    std::string op;
    int pri;
    int64_t delay_ps;
    int phase;
    std::vector<int> deps;
};

struct Attrs {
    std::map<std::string, std::string> kv;   // "ns3::Class::Attr" or "UB_*" -> value
    std::set<std::string> used;
    std::string get(const std::string &k, const std::string &def)
    {
        used.insert(k);
        auto it = kv.find(k);
        return it == kv.end() ? def : it->second;
    }
    bool getb(const std::string &k, bool def)
    {
        std::string v = get(k, def ? "true" : "false");
        return v == "true" || v == "1";
    }
    long long geti(const std::string &k, long long def) { return std::stoll(get(k, std::to_string(def))); }
};

struct Case {
    std::vector<NodeCfg> nodes;
    std::vector<LinkCfg> links;
    std::map<std::tuple<int, int, int>, RouteCfg> exact;   // (node, dst, dstPort)
    std::map<std::pair<int, int>, RouteCfg> primary;        // (node, dst)
    std::vector<TaskCfg> tasks;
    Attrs attr;
};

inline std::vector<std::string> split(const std::string &s, char c)
{
    std::vector<std::string> r;
    std::string cur;
    for (char ch : s) {
        if (ch == c) {
            r.push_back(cur);
            cur.clear();
        } else if (ch != '\r')
            cur += ch;
    }
    r.push_back(cur);
    return r;
}
inline std::string trim(std::string s)
{
    while (!s.empty() && isspace((unsigned char)s.back()))
        s.pop_back();
    size_t i = 0;
    while (i < s.size() && isspace((unsigned char)s[i]))
        ++i;
    return s.substr(i);
}
inline std::pair<int, int> range(const std::string &s)
{
    auto p = s.find("..");
    if (p == std::string::npos)
        return {std::stoi(s), std::stoi(s)};
    return {std::stoi(s.substr(0, p)), std::stoi(s.substr(p + 2))};
}
inline bool data_line(const std::string &l)
{
    return !(l.empty() || l[0] == '#' || l.find_first_not_of(" \t\r") == std::string::npos);
}

inline void load_case(const std::string &dir, Case &c)
{
    std::string line;
    // network_attribute.txt
    {
        std::ifstream f(dir + "/network_attribute.txt");
        if (!f)
            throw std::runtime_error("no network_attribute.txt in " + dir);
        while (std::getline(f, line)) {
            line = trim(line);
            if (line.empty() || line[0] == '#')
                continue;
            std::istringstream is(line);
            std::string kind, key, val;
            is >> kind >> key;
            std::getline(is, val);
            val = trim(val);
            if (val.size() >= 2 && val.front() == '"' && val.back() == '"')
                val = val.substr(1, val.size() - 2);
            c.attr.kv[key] = val;
        }
    }
    // node.csv
    {
        std::ifstream f(dir + "/node.csv");
        std::getline(f, line);
        auto hdr = split(trim(line), ',');
        int ia = -1, ifw = -1;
        for (size_t i = 0; i < hdr.size(); ++i) {
            if (trim(hdr[i]) == "allocationDelay") ia = i;
            if (trim(hdr[i]) == "forwardDelay") ifw = i;
        }
        bool legacy4 = hdr.size() == 4 && ifw == 3 && ia < 0; // legacy forwardDelay -> AllocationTime
        int64_t defAlloc = parse_time_ps(c.attr.get("ns3::UbSwitchAllocator::AllocationTime", "5ns"));
        int64_t defFwd = parse_time_ps(c.attr.get("ns3::UbSwitch::InPortProcessingDelay", "0ns"));
        while (std::getline(f, line)) {
            if (!data_line(line))
                continue;
            auto v = split(trim(line), ',');
            auto [a, b] = range(trim(v[0]));
            NodeCfg n;
            n.sw = trim(v[1]) == "SWITCH";
            n.ports = std::stoi(v[2]);
            n.alloc_ps = defAlloc;
            n.fwd_ps = defFwd;
            if (legacy4) {
                if (v.size() > 3 && !trim(v[3]).empty()) n.alloc_ps = parse_time_ps(trim(v[3]));
            } else {
                if (ia >= 0 && (int)v.size() > ia && !trim(v[ia]).empty()) n.alloc_ps = parse_time_ps(trim(v[ia]));
                if (ifw >= 0 && (int)v.size() > ifw && !trim(v[ifw]).empty()) n.fwd_ps = parse_time_ps(trim(v[ifw]));
            }
            if ((int)c.nodes.size() <= b)
                c.nodes.resize(b + 1);
            for (int i = a; i <= b; ++i)
                c.nodes[i] = n;
        }
    }
    // topology.csv
    {
        std::ifstream f(dir + "/topology.csv");
        std::getline(f, line);
        while (std::getline(f, line)) {
            if (!data_line(line))
                continue;
            auto v = split(trim(line), ',');
            LinkCfg l{std::stoi(v[0]), std::stoi(v[1]), std::stoi(v[2]), std::stoi(v[3]),
                      parse_rate_bps(trim(v[4])), parse_time_ps(trim(v[5]))};
            c.links.push_back(l);
        }
    }
    // routing_table.csv: rows grouped by metric (std::map, ascending); the lowest metric is the
    // shortest group, all others form one non-shortest group; ports sorted and unique
    // (UbUtils::AddRoutingTable, UbNextHopIndexBuilder NormalizePorts)
    {
        std::ifstream f(dir + "/routing_table.csv");
        std::getline(f, line);
        typedef std::map<uint32_t, std::vector<uint16_t>> MP;
        std::map<std::tuple<int, int, int>, MP> ex;
        std::map<std::pair<int, int>, MP> pr;
        while (std::getline(f, line)) {
            if (!data_line(line))
                continue;
            auto v = split(line, ',');
            auto [n0, n1] = range(trim(v[0]));
            auto [d0, d1] = range(trim(v[1]));
            if (n0 != n1 || d0 != d1)
                throw std::runtime_error("routing_table.csv: range rows not supported yet");
            int dp = std::stoi(v[2]);
            std::istringstream po(v[3]), me(v[4]);
            std::vector<int> ports, mets;
            int x;
            while (po >> x) ports.push_back(x);
            while (me >> x) mets.push_back(x);
            if (mets.size() != ports.size())
                throw std::runtime_error("routing_table.csv: ports/metrics length mismatch");
            for (size_t i = 0; i < ports.size(); ++i) {
                ex[{n0, d0, dp}][mets[i]].push_back(ports[i]);
                pr[{n0, d0}][mets[i]].push_back(ports[i]);
            }
        }
        auto cls = [](const MP &m) {
            RouteCfg r;
            bool first = true;
            for (auto &[met, ps] : m) {
                auto &dst = first ? r.shortest : r.other;
                dst.insert(dst.end(), ps.begin(), ps.end());
                first = false;
            }
            for (auto *v : {&r.shortest, &r.other}) {
                std::sort(v->begin(), v->end());
                v->erase(std::unique(v->begin(), v->end()), v->end());
            }
            return r;
        };
        for (auto &[k, m] : ex) c.exact[k] = cls(m);
        for (auto &[k, m] : pr) c.primary[k] = cls(m);
    }
    // traffic.csv
    {
        std::ifstream f(dir + "/traffic.csv");
        std::getline(f, line);
        while (std::getline(f, line)) {
            if (!data_line(line))
                continue;
            auto v = split(line, ',');
            if (v.size() != 9)
                throw std::runtime_error("traffic.csv: only the 9-column form is supported");
            TaskCfg t;
            t.id = std::stoi(v[0]);
            t.src = std::stoi(v[1]);
            t.dst = std::stoi(v[2]);
            t.size = std::stoull(v[3]);
            t.op = trim(v[4]);
            t.pri = std::stoi(v[5]);
            t.delay_ps = parse_time_ps(trim(v[6]));
            t.phase = std::stoi(v[7]);
            std::istringstream ds(v[8]);
            int d;
            while (ds >> d) t.deps.push_back(d);
            c.tasks.push_back(t);
        }
    }
}

} // namespace ub
