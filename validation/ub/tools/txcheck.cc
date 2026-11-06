// TwiNS side of the tx-time check: reads txcheck_ref output on stdin, recomputes, reports mismatches.
#include "ub/ubtime.h"
#include "ub/ubhash.h"
#include <sstream>
#include <cstdio>
#include <cstring>
int main()
{
    char line[256];
    long n = 0, bad = 0;
    while (fgets(line, sizeof line, stdin)) {
        if (line[0] == 'H') {
            std::istringstream is(line + 2);
            int len; is >> len; uint8_t b[64];
            for (int j = 0; j < len; ++j) { int v; is >> v; b[j] = v; }
            unsigned long long ref; is >> ref;
            unsigned long long m = ub::ns3_hash64(b, len);
            if (m != ref) { if (bad < 20) printf("hash len %d ref %llu got %llu\n", len, ref, m); ++bad; }
            ++n;
            continue;
        }
        if (line[0] == 'T') {
            char s[64];
            long long v;
            sscanf(line, "T %63s %lld", s, &v);
            long long m = ub::parse_time_ps(s);
            if (m != v) { printf("time %s ref %lld got %lld\n", s, v, m); ++bad; }
            ++n;
            continue;
        }
        unsigned long long bps; unsigned b; long long ps;
        if (sscanf(line, "%llu %u %lld", &bps, &b, &ps) != 3) continue;
        long long m = ub::tx_ps(bps, b);
        if (m != ps) { if (bad < 20) printf("bps %llu bytes %u ref %lld got %lld\n", bps, b, ps, m); ++bad; }
        ++n;
    }
    printf("%ld checked, %ld mismatches\n", n, bad);
    return bad != 0;
}
