// twins-astra-stub TOPO WORKLOAD [NIC_GBPS]: astra-sim on a network without congestion: counts flows
// and timers, and measures the system layer's CPU time per callback.
#include <cstdio>
#include <cstdlib>
#include <unistd.h>

#include "astra/frontend.h"

int main(int argc, char **argv)
{
    if (argc < 3)
    {
        fprintf(stderr, "usage: twins-astra-stub TOPO WORKLOAD [NIC_GBPS]\n");
        return 1;
    }
    std::vector<astra::Flow> flows;
    auto st = astra::stub_run(argv[1], argv[2], argc > 3 ? atof(argv[3]) : 400, &flows);
    printf("%zu flows, %llu timers, %llu callbacks, finished %d, simulated %.6f s; init %.3f s, run %.3f s: %.2f us per callback, %.2f per flow\n",
           flows.size(), (unsigned long long)st.timers, (unsigned long long)st.callbacks, (int)st.finished, st.simulated_ns * 1e-9, st.init_s, st.run_s,
           st.run_s * 1e6 / st.callbacks, st.run_s * 1e6 / flows.size());
    fflush(stdout);
    _exit(0);
}
