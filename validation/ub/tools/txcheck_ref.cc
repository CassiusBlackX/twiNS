// ns-3 side of the tx-time check: prints rate bytes txtime_ps (ns-3-ub Time resolution PS).
// Build as a scratch program in the ns-3-ub tree.
#include "ns3/core-module.h"
#include "ns3/network-module.h"
#include "ns3/hash.h"
#include <cstdio>
using namespace ns3;
int main()
{
    Time::SetResolution(Time::PS);
    const char* rates[] = {"25Gbps", "40Gbps", "50Gbps", "100Gbps", "200Gbps", "400Gbps",
                           "800Gbps", "1200Gbps", "1600Gbps", "56Gbps", "112Gbps", "3200Gbps"};
    for (auto r : rates) {
        DataRate d(r);
        for (uint32_t b = 1; b <= 70000; b += (b < 9000 ? 1 : 97))
            printf("%llu %u %lld\n", (unsigned long long)d.GetBitRate(), b,
                   (long long)d.CalculateBytesTxTime(b).GetPicoSeconds());
    }
    // time strings
    const char* ts[] = {"20ns", "+5ns", "1ns", "0.5ns", "1.5us", "100ns", "25600ns", "+500ns"};
    for (auto t : ts)
        printf("T %s %lld\n", t, (long long)Time(t).GetPicoSeconds());
    // Hash64 of pseudo-random byte strings of length 1..40 (17 = UB ECMP key)
    uint64_t x = 88172645463325252ull;
    for (int i = 0; i < 20000; ++i) {
        uint8_t b[40];
        int len = 1 + i % 40;
        printf("H %d", len);
        for (int j = 0; j < len; ++j) { x ^= x << 13; x ^= x >> 7; x ^= x << 17; b[j] = x & 0xff; printf(" %d", b[j]); }
        printf(" %llu\n", (unsigned long long)Hash64((const char*)b, len));
    }
    return 0;
}
