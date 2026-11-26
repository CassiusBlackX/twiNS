"""Create logging-only setup observers; they are separate from formal timed binaries."""
from pathlib import Path
import sys
root,dest=map(Path,sys.argv[1:]);dest.mkdir(exist_ok=True)
clock='''
    auto phase_tick = std::chrono::steady_clock::now();
    auto phase_observe = [&](const char *name) {
        auto now = std::chrono::steady_clock::now();
        std::cerr << "setup_phase " << name << " seconds="
                  << std::chrono::duration<double>(now-phase_tick).count() << '\\n';
        phase_tick = now;
    };
'''
s='#include <iostream>\n'+(root/'src/main.cu').read_text()
s=s.replace('int main(int argc, char **argv)\n{','int main(int argc, char **argv)\n{'+clock)
s=s.replace('    // ---- flows: messages','    phase_observe("topology_and_routes");\n    // ---- flows: messages',1)
s=s.replace('    memory_phase("before-capture");','    phase_observe("flows_and_device_allocation");\n    memory_phase("before-capture");',1)
s=s.replace('    auto t0 = std::chrono::high_resolution_clock::now();','    phase_observe("capture_and_instantiate");\n    auto t0 = std::chrono::high_resolution_clock::now();',1)
(dest/'phase-gpu.cu').write_text(s)
s=(root/'validation/ns3/twins-ref.cc').read_text()
s=s.replace('int main(int argc, char *argv[])\n{','int main(int argc, char *argv[])\n{'+clock)
s=s.replace('    Ipv4GlobalRoutingHelper::PopulateRoutingTables();','    phase_observe("arguments_graph_and_stack");\n    Ipv4GlobalRoutingHelper::PopulateRoutingTables();\n    phase_observe("routing");',1)
s=s.replace('    auto t0 = std::chrono::steady_clock::now();','    phase_observe("applications");\n    auto t0 = std::chrono::steady_clock::now();',1)
(dest/'phase-ref.cc').write_text(s)
