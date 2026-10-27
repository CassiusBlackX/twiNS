// Reference values for src/nsmath.h, from ns-3 itself (build as an ns-3 scratch program):
//   inv <hi> <lo>                      raw Q64.64 of int64x64_t::Invert(10^12)
//   sec <ps> <bits>                    Time(ps).GetSeconds() (resolution PS)
//   pow <xbits> <ybits> <bits>         std::pow(x, y) as ns-3 calls it (glibc libm)
#include "ns3/core-module.h"

#include <cmath>
#include <cstdio>
#include <cstring>
#include <random>

using namespace ns3;

static uint64_t
bits(double d)
{
    uint64_t u;
    std::memcpy(&u, &d, 8);
    return u;
}

int
main(int argc, char* argv[])
{
    Time::SetResolution(Time::PS);
    int64x64_t inv = int64x64_t::Invert(1000000000000ULL);
    std::printf("inv %lld %llu\n", (long long)inv.GetHigh(), (unsigned long long)inv.GetLow());
    std::mt19937_64 rng(12345);
    for (int i = 0; i < 2000000; i++)
    {
        int64_t ps;
        switch (i % 4)
        {
        case 0: ps = (int64_t)(rng() % 10000000000000ULL); break;           // up to 10 s
        case 1: ps = (int64_t)(rng() % 100000000000ULL); break;             // up to 100 ms
        case 2: ps = (int64_t)(rng() % 1000000000ULL) * 1000; break;        // whole ns
        default: ps = -(int64_t)(rng() % 10000000000000ULL); break;         // negative
        }
        std::printf("sec %lld %llu\n", (long long)ps, (unsigned long long)bits(PicoSeconds(ps).GetSeconds()));
    }
    std::uniform_real_distribution<double> u01(0, 1);
    for (int i = 0; i < 3000000; i++)
    {
        double x, y;
        switch (i % 3)
        {
        case 0: x = (double)(rng() % 100000) / 0.4; y = 1 / 3.; break;    // Cubic's K
        case 1: x = u01(rng) * std::pow(10, (int)(rng() % 8) - 4); y = 3; break; // Cubic's offs^3
        default: x = std::exp(u01(rng) * 80 - 40); y = u01(rng) * 20 - 10; break;
        }
        std::printf("pow %llu %llu %llu\n", (unsigned long long)bits(x), (unsigned long long)bits(y),
                    (unsigned long long)bits(std::pow(x, y)));
    }
    return 0;
}
