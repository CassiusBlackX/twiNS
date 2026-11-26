#include <iostream>
#include "ns3/core-module.h"

int main()
{
    for (bool timer_first : {true, false})
    {
        int arrived = 0;
        auto entry = [&](char event) {
            std::cout << int(timer_first) << ' ' << event << ' ' << ns3::Simulator::Now().GetNanoSeconds() << '\n';
            if (++arrived == 2)
                ns3::Simulator::Schedule(ns3::NanoSeconds(1), [&] {
                    std::cout << int(timer_first) << " send " << ns3::Simulator::Now().GetNanoSeconds() << '\n';
                });
        };
        ns3::Simulator::Schedule(ns3::NanoSeconds(25), [&, e = timer_first ? 't' : 'r'] { entry(e); });
        ns3::Simulator::Schedule(ns3::NanoSeconds(25), [&, e = timer_first ? 'r' : 't'] { entry(e); });
        ns3::Simulator::Run();
        ns3::Simulator::Destroy();
    }
}
