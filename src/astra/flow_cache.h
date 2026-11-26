#pragma once

#include <fcntl.h>
#include <memory>

// The cache is a normal DAG with comment metadata. The input key includes
// the executable because the stub's transfer enumeration can change across builds.
static uint64_t flow_cache_hash(uint64_t hash, const char *begin, size_t count)
{
    for (size_t i = 0; i < count; ++i)
        hash = (hash ^ uint8_t(begin[i])) * 1099511628211ull;
    return hash;
}

constexpr uint64_t FLOW_CACHE_SEED = 14695981039346656037ull;

static bool flow_cache_file_hash(const char *path, uint64_t *hash)
{
    std::ifstream in(path, std::ios::binary);
    if (!in) return false;
    *hash = FLOW_CACHE_SEED;
    char buffer[1 << 16];
    while (in.read(buffer, sizeof(buffer)) || in.gcount())
        *hash = flow_cache_hash(*hash, buffer, in.gcount());
    return in.eof() && !in.bad();
}

template <class Add>
static bool flow_cache_read(const string &path, const string &header, Add add, int &count)
{
    std::ifstream in(path);
    string line;
    if (!std::getline(in, line) || line != header) return false;
    uint64_t hash = FLOW_CACHE_SEED;
    count = 0;
    while (std::getline(in, line))
    {
        if (line.rfind("# end ", 0) == 0)
            return line == "# end " + std::to_string(count) + " " + std::to_string(hash) &&
                   in.peek() == std::char_traits<char>::eof() && !in.bad();
        unsigned id, src, dst;
        unsigned long long size;
        int consumed = 0;
        if (sscanf(line.c_str(), "flow %u %u %u %llu%n", &id, &src, &dst, &size, &consumed) != 4 ||
            size_t(consumed) != line.size() || id != unsigned(count) || !size ||
            src > INT_MAX || dst > INT_MAX || count == INT_MAX)
            return false;
        hash = flow_cache_hash(hash, line.data(), line.size());
        add(count++, int(src), int(dst), uint64_t(size));
    }
    return false;
}

static bool flow_cache_write(FILE *file, const string &header, const vector<astra::Flow> &flows)
{
    if (fprintf(file, "%s\n", header.c_str()) < 0) return false;
    char line[128];
    uint64_t hash = FLOW_CACHE_SEED;
    for (size_t i = 0; i < flows.size(); ++i)
    {
        int len = snprintf(line, sizeof(line), "flow %zu %d %d %llu", i, flows[i].src, flows[i].dst,
                           (unsigned long long)flows[i].size);
        if (len < 0 || size_t(len) >= sizeof(line) || fprintf(file, "%s\n", line) < 0)
            return false;
        hash = flow_cache_hash(hash, line, len);
    }
    return fprintf(file, "# end %zu %llu\n", flows.size(), (unsigned long long)hash) >= 0;
}
