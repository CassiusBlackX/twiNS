// Optional TCP graph-link fields shared with the standalone ns-3 scenario.
#pragma once
#include <charconv>
#include <cstdint>
#include <sstream>
#include <stdexcept>
#include <string>

struct LinkTiming
{
    int64_t bps = 100000000000;
    int64_t delay_ps = 1000000;
};

inline LinkTiming read_link_timing(std::istream &input)
{
    std::string line;
    std::getline(input, line);
    std::istringstream fields(line.substr(0, line.find('#')));
    LinkTiming timing;
    std::string rate, delay, extra;
    if (!(fields >> rate))
        return timing;
    auto positive = [](const std::string &s, int64_t &out) {
        auto end = s.data() + s.size();
        auto result = std::from_chars(s.data(), end, out);
        return result.ec == std::errc{} && result.ptr == end && out > 0;
    };
    if (!(fields >> delay) || fields >> extra || !positive(rate, timing.bps) || !positive(delay, timing.delay_ps))
        throw std::invalid_argument("link expects endpoints followed by positive rate_bps and delay_ps");
    return timing;
}

// ns-3.46.1 computes Seconds(Q64.64(bits) / rate), then rounds to its ps tick.
// Split out whole seconds before scaling the fractional part to fit 128 bits.
inline int64_t link_serialization_ps(int bytes, int64_t bps)
{
    using Wide = unsigned __int128;
    uint64_t bits = uint64_t(bytes) * 8;
    Wide fraction = (Wide(bits % bps) << 64) / bps;
    return int64_t(bits / bps * 1000000000000ull +
                   ((fraction * 1000000000000ull + (Wide(1) << 63)) >> 64));
}
