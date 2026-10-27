// Reference draws of ns-3's UniformRandomVariable with deterministic streams (the ECMP choice of
// Ipv4GlobalRouting with per-packet ECMP), for checking src/rng.h. Build as an ns-3 scratch
// program; prints "u <stream> <i> <bits of RandU01>" and "int <stream> <n> <i> <GetInteger(0, n-1)>".
#include "ns3/core-module.h"

#include <cstdio>
#include <cstring>

using namespace ns3;

int
main(int argc, char* argv[])
{
    CommandLine cmd;
    cmd.Parse(argc, argv);
    for (int64_t stream : {0, 1, 2, 7, 100, 1023, 5000, 65535})
    {
        Ptr<UniformRandomVariable> u = CreateObject<UniformRandomVariable>();
        u->SetStream(stream);
        for (int i = 0; i < 1000; i++)
        {
            double v = u->GetValue(0.0, 1.0);
            uint64_t b;
            memcpy(&b, &v, 8);
            printf("u %lld %d %llu\n", (long long)stream, i, (unsigned long long)b);
        }
        Ptr<UniformRandomVariable> w = CreateObject<UniformRandomVariable>();
        w->SetStream(stream);
        for (int i = 0; i < 1000; i++)
        {
            uint32_t n = 1 + i % 16;
            printf("int %lld %u %d %u\n", (long long)stream, n, i, w->GetInteger(0, n - 1));
        }
    }
    return 0;
}
