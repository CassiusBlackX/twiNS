#include <algorithm>
#include <cstdint>
#include <fstream>
#include <iostream>
#include <map>
#include <unordered_map>
#include <vector>

struct Sample
{
    int64_t start, arrival;
    uint64_t tail_a, tail_b;
    int link, long_chain;
};
static_assert(sizeof(Sample) == 40);

struct Endpoint
{
    int64_t time;
    uint32_t index;
    bool start;
};

int main(int argc, char **argv)
{
    if (argc != 4)
        return 1;
    const int64_t slot_ps = std::stoll(argv[2]);
    const int64_t frame_bytes = std::stoll(argv[3]);
    if (slot_ps <= 0 || frame_bytes <= 0)
        return 1;
    std::ifstream input(argv[1], std::ios::binary | std::ios::ate);
    const auto length = input.tellg();
    if (length < 0 || length % sizeof(Sample))
        return 1;
    std::vector<Sample> samples(length / sizeof(Sample));
    input.seekg(0);
    if (!input.read(reinterpret_cast<char *>(samples.data()), length))
        return 1;
    std::vector<Endpoint> events;
    events.reserve(2 * samples.size());
    std::unordered_map<int64_t, uint64_t> by_slot;
    uint64_t wide = 0, bucket_max = 0;
    for (uint32_t i = 0; i < samples.size(); ++i)
    {
        const Sample &s = samples[i];
        if (s.start >= s.arrival)
            return 1;
        events.push_back({s.start, i, true});
        events.push_back({s.arrival, i, false});
        if (s.long_chain)
        {
            ++wide;
            bucket_max = std::max(bucket_max, ++by_slot[s.arrival / slot_ps]);
        }
    }
    std::sort(events.begin(), events.end(), [](const Endpoint &a, const Endpoint &b) {
        return a.time < b.time || (a.time == b.time && a.start < b.start);
    });
    std::map<std::pair<uint64_t, uint64_t>, uint64_t> suffixes;
    uint64_t inflight = 0, long_inflight = 0, peak = 0, long_peak = 0, max_distinct = 0;
    uint64_t max_duplicates = 0, distinct_at_long_peak = 0, long_at_peak = 0;
    for (const auto &event : events)
    {
        const auto &s = samples[event.index];
        if (event.start)
            ++inflight;
        else
            --inflight;
        if (s.long_chain)
        {
            const auto key = std::make_pair(s.tail_a, s.tail_b);
            if (event.start)
            {
                ++long_inflight;
                ++suffixes[key];
            }
            else
            {
                --long_inflight;
                auto it = suffixes.find(key);
                if (it == suffixes.end())
                    return 1;
                if (!--it->second)
                    suffixes.erase(it);
            }
        }
        if (inflight > peak || (inflight == peak && long_inflight > long_at_peak))
        {
            peak = inflight;
            long_at_peak = long_inflight;
        }
        if (long_inflight > long_peak)
        {
            long_peak = long_inflight;
            distinct_at_long_peak = suffixes.size();
        }
        max_distinct = std::max(max_distinct, uint64_t(suffixes.size()));
        max_duplicates = std::max(max_duplicates, long_inflight - suffixes.size());
    }
    std::cout << "{\n"
              << "  \"writes\": " << samples.size() << ",\n"
              << "  \"wide_writes\": " << wide << ",\n"
              << "  \"wide_layout_write_bytes\": " << samples.size() * frame_bytes << ",\n"
              << "  \"peak_inflight\": " << peak << ",\n"
              << "  \"wide_at_peak_inflight\": " << long_at_peak << ",\n"
              << "  \"peak_wide_inflight\": " << long_peak << ",\n"
              << "  \"distinct_suffixes_at_first_wide_peak\": " << distinct_at_long_peak << ",\n"
              << "  \"peak_distinct_exact_suffix_fingerprints\": " << max_distinct << ",\n"
              << "  \"peak_duplicate_suffixes\": " << max_duplicates << ",\n"
              << "  \"peak_wide_writes_per_arrival_slot\": " << bucket_max << "\n}\n";
}
