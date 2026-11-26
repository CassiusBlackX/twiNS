"""Isolate cross-pod input adaptation and device counters from production trees."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import shutil
import subprocess
import sys

from crosspod import ROOT, replace, sha
from crosspod_connections import baseline
from pfc_sustained import save


GEDES_CONNECTIONS = r'''
  // Each row supplies a single application transfer to a separate, already-open TCP connection.
  static void LoadIndependentConnections(const std::vector<Node*>& nodes, std::vector<int64_t>& starts)
  {
    std::ifstream input(flow_file_path);
    std::string line;
    if (!std::getline(input, line))
      throw std::runtime_error("missing independent connection workload header");
    if (!line.empty() && line.back() == '\r')
      line.pop_back();
    if (line != "node,dst,flow_size,start_ns")
      throw std::runtime_error("invalid independent connection workload header");
    while (std::getline(input, line))
    {
      int a, b;
      long long bytes, start;
      if (sscanf(line.c_str(), "%d,%d,%lld,%lld", &a, &b, &bytes, &start) != 4 ||
          a < 0 || b < 0 || a == b || (size_t)a >= nodes.size() || (size_t)b >= nodes.size() ||
          bytes <= 0 || bytes % 1460 || start < 0)
        throw std::runtime_error("invalid independent connection row: " + line);
      auto *from = GetTCPProtocol(nodes[a]);
      auto *to = GetTCPProtocol(nodes[b]);
      const auto from_index = from->tcp_cons_num;
      const auto to_index = to->tcp_cons_num;
      ConnectTCPConnection(nodes[a], 0, 0, nodes[b], 0, 0);
      if (from->tcp_cons_num != from_index + 1 || to->tcp_cons_num != to_index + 1)
        throw std::runtime_error("exceeded native GeDES connection capacity");
      Flow flow{};
      flow.flow_size = bytes;
      flow.timestamp = start;
      flow.tiimestamp_end = -1;
      CopyDataToGPUQueue(from->tcp_cons[from_index]->flows, &flow, 1);
      starts.push_back(start);
    }
    if (starts.empty())
      throw std::runtime_error("no independent connections");
    std::sort(starts.begin(), starts.end());
  }
'''


def adapt_gedes(tree):
    cpp = tree / 'code/c++/src'
    topology = cpp / 'components/topology.cc'
    replace(topology, '#include <map>', '#include <map>\n#include <stdexcept>')
    replace(topology, '  void Topology::BuildTCPConnections()\n  {',
            GEDES_CONNECTIONS + '\n  void Topology::BuildTCPConnections()\n  {\n'
            '    if (getenv("GEDES_INDEPENDENT_CONNECTIONS")) {\n'
            '      LoadIndependentConnections(m_nodes, m_flow_start_instants);\n      return;\n    }')
    replace(topology, '    CreateFlows(0);',
            '    if (!getenv("GEDES_INDEPENDENT_CONNECTIONS"))\n      CreateFlows(0);')
    # The frozen preset had one connection per node and fixed start-queue capacity. The native
    # timers read this queue, so size it for the explicit starts on either input mode.
    replace(topology, 'GPUQueue<int64_t> *flow_start_instants = create_gpu_queue<int64_t>(100);',
            'GPUQueue<int64_t> *flow_start_instants = create_gpu_queue<int64_t>(\n'
            '        std::max<size_t>(100, m_flow_start_instants.size()));')
    sender = cpp / 'transport/tcp_controller.cu'
    replace(sender, '            if (send_packet_num <= 0)\n            {\n                break;\n            }',
            '            if (send_packet_num <= 0)\n            {\n'
            '                if (remaining_capacity <= 0 || used_packet_num >= MAX_TRANSMITTED_PACKET_NUM + MAX_GENERATED_PACKET_NUM)\n'
            '                    break;\n                continue;\n            }')
    result = cpp / 'transport/tcp_controller.cc'
    replace(result, 'int tcp_con_num = std::accumulate(tcp_cons_num_per_node, tcp_cons_num_per_node + kernel_param_cpu.node_num, 0);',
            'int tcp_con_num = kernel_param_cpu.node_num;')
    replace(result, '      for (int j = 0; j < tcp_con_num; j++)\n      {\n        cudaMemcpy(&con, cons[j * 10], sizeof(TCPConnection), cudaMemcpyDeviceToHost);',
            '      for (int node = 0; node < tcp_con_num; node++)\n'
            '        for (int connection = 0; connection < tcp_cons_num_per_node[node]; connection++)\n'
            '      {\n        cudaMemcpy(&con, cons[node * MAX_TCP_CONNECTION_NUM + connection], '
            'sizeof(TCPConnection), cudaMemcpyDeviceToHost);')

    switch = cpp / 'link_layer/switch.cu'
    replace(switch, '__global__ void set_audit_ecmp(uint32_t *p) { g_audit_ecmp = p; }',
            '__device__ int g_audit_route_hosts;\n'
            '__global__ void set_audit_ecmp(uint32_t *p, int hosts) { g_audit_ecmp = p; g_audit_route_hosts = hosts; }')
    replace(switch, '    return ((g_audit_ecmp[src] >> (aggregation ? 8 : 0)) & 255) + half_ft_k;',
            '''    uint32_t route = g_audit_ecmp[src];
    if (g_audit_route_hosts)
    {
        uint32_t dst;
        memcpy(&dst, frame->dst_mac, sizeof(uint32_t));
        route = g_audit_ecmp[(size_t)src * g_audit_route_hosts + dst];
        if (route == UINT32_MAX)
            return rotating;
    }
    return ((route >> (aggregation ? 8 : 0)) & 255) + half_ft_k;''')
    replace(switch, '        std::vector<uint32_t> h(host_num, 0);',
            '        const bool pair = getenv("GEDES_PAIR_ROUTES") != nullptr;\n'
            '        std::vector<uint32_t> h(pair ? (size_t)host_num * host_num : host_num, pair ? UINT32_MAX : 0);')
    replace(switch, '                    h[v[0]] = (uint32_t)v[6], n++;',
            '''                {
                    if (pair && (v[1] < 0 || v[1] >= host_num)) continue;
                    h[pair ? (size_t)v[0] * host_num + v[1] : v[0]] = (uint32_t)v[6];
                    n++;
                }''')
    replace(switch, '        cudaMalloc(&d, sizeof(uint32_t) * host_num);\n'
            '        cudaMemcpy(d, h.data(), sizeof(uint32_t) * host_num, cudaMemcpyHostToDevice);\n'
            '        set_audit_ecmp<<<1, 1>>>(d);',
            '        cudaMalloc(&d, sizeof(uint32_t) * h.size());\n'
            '        cudaMemcpy(d, h.data(), sizeof(uint32_t) * h.size(), cudaMemcpyHostToDevice);\n'
            '        set_audit_ecmp<<<1, 1>>>(d, pair ? host_num : 0);')
    main = tree / 'main.cc'
    replace(main, '    topology.RecordFlowResults(output_file_name);',
            '    std::cout << "strict_stop native_finished=" << topology.IsFinished()\n'
            '              << " last_slot_exclusive_ns=" << (executed_slots * 1000) << std::endl;\n'
            '    topology.RecordFlowResults(output_file_name);')


def adapt_gedes_receive(tree):
    """Use the stock TCP state machine on the selected connection, with separate ACK slots."""
    tcp = tree / 'code/c++/src/transport/tcp_controller.cu'
    replace(tcp, '''    memcpy(ack_packet->src_port, &con->src_port, 2);
    memcpy(ack_packet->dst_port, &con->dst_port, 2);''', '''    // GeDES stores destination and source ports in this order for receive lookup.
    memcpy(ack_packet->src_port, &con->dst_port, 2);
    memcpy(ack_packet->dst_port, &con->src_port, 2);''')
    replace(tcp, '''__device__ int handle_non_ack_packets(GPUQueue<VDES::TCPPacket *> *recv_queue, int ack_num, VDES::TCPConnection **cons, int con_num)
{
    int packet_num = recv_queue->size;

    VDES::TCPConnection *con = cons[0];''', '''__device__ int handle_non_ack_packets(GPUQueue<VDES::TCPPacket *> *recv_queue, int ack_num,
                                      VDES::TCPConnection **cons, int con_num, int selected)
{
    int packet_num = recv_queue->size;
    VDES::TCPConnection *con = cons[selected];''')
    replace(tcp, '''        VDES::TCPPacket *p = recv_queue->get_element(i);

        memcpy(&seq, p->sequence_number, 4);
        uint32_t seq_end = seq + 1460;''', '''        VDES::TCPPacket *p = recv_queue->get_element(i);

        if (con_num > 1 && get_tcp_connection_id(p, cons, con_num) != selected)
        {
            expired_packet_num++;
            continue;
        }
        memcpy(&seq, p->sequence_number, 4);
        uint32_t seq_end = seq + 1460;''')
    replace(tcp, 'send_ack_packets(VDES::TCPParams *params, int non_ack_num,',
            'send_ack_packets(VDES::TCPParams *params, const int *non_ack_num,')
    replace(tcp, '\n                VDES::TCPPacket *ack_packet = alloc_packets[j];\n',
            '\n                VDES::TCPPacket *ack_packet = alloc_packets[ack_response + j];\n')
    replace(tcp, '\n        else if (non_ack_num > 0)\n', '\n        else if (non_ack_num[i] > 0)\n')
    replace(tcp, '\n                VDES::TCPPacket *ack_packet = alloc_packets[0];\n',
            '\n                VDES::TCPPacket *ack_packet = alloc_packets[ack_response];\n')
    replace(tcp, '''        int expired_packet_num = handle_non_ack_packets(recv_queue, ack_num, cons, con_num);
        int non_ack_num = recv_queue->size - ack_num - expired_packet_num;''', '''        int non_ack_num[MAX_TCP_CONNECTION_NUM];
        for (int i = 0; i < con_num; i++)
        {
            int skipped = handle_non_ack_packets(recv_queue, ack_num, cons, con_num, i);
            non_ack_num[i] = recv_queue->size - ack_num - skipped;
        }''')


def counter_report(label, counters, fields):
    return f'''
#ifdef FOLLOWUP_WORK
void Report{label}()
{{
    unsigned long long record[{len(fields)}];
    if (cudaMemcpyFromSymbol(record, {counters}, sizeof(record)) != cudaSuccess)
        abort();
    printf("strict_work {" ".join(f'{field}=%llu' for field in fields)}\\n", {", ".join(f'record[{i}]' for i in range(len(fields)))});
}}
#endif
'''


def counter_header(path, label, device_name, fields):
    first, body = path.read_text().split('\n', 1)
    assert first.startswith('#include ')
    path.write_text(first + f'\n#ifdef FOLLOWUP_WORK\n__device__ unsigned long long {device_name}[{len(fields)}];\n#endif\n'
                    + counter_report(label, device_name, fields) + body)


def instrument_gedes(tree):
    cpp = tree / 'code/c++/src'
    tcp = cpp / 'transport/tcp_controller.cu'
    fields = ('original_data', 'retransmission', 'ack', 'rto_recovery', 'rto_with_outstanding',
              'fast_recovery', 'mixed_sequence_segment')
    counter_header(tcp, 'TcpWork', 'g_tcp_work', fields)
    replace(tree / 'code/c++/include/tcp_controller.h', '\n        uint64_t acked_bytes;\n',
            '\n        uint64_t acked_bytes;\n#ifdef FOLLOWUP_WORK\n        uint64_t followup_highest_sequence;\n#endif\n')
    replace(tcp, '        // congestion avoidance\n        con->ssthresh', '''#ifdef FOLLOWUP_WORK
        if (rto_timeout)
        {
            atomicAdd(g_tcp_work + 3, 1ull);
            if (con->una > con->snd) atomicAdd(g_tcp_work + 4, 1ull);
        }
        if (ack_repeat_over_3_times) atomicAdd(g_tcp_work + 5, 1ull);
#endif
        // congestion avoidance
        con->ssthresh''')
    fragment = '__device__ void fill_ack_packet(VDES::TCPConnection *con, VDES::TCPPacket *ack_packet, int64_t *timestamp)\n{'
    replace(tcp, fragment, fragment + '\n#ifdef FOLLOWUP_WORK\n    atomicAdd(g_tcp_work + 2, 1ull);\n#endif')
    replace(tcp, '    memcpy(packet->sequence_number, &con->una, 4);\n    con->una += payload_len;',
            '''    memcpy(packet->sequence_number, &con->una, 4);
#ifdef FOLLOWUP_WORK
    atomicAdd(g_tcp_work + (con->una < con->followup_highest_sequence), 1ull);
    if (con->una < con->followup_highest_sequence && con->una + payload_len > con->followup_highest_sequence)
        atomicAdd(g_tcp_work + 6, 1ull);
    if (con->una + payload_len > con->followup_highest_sequence)
        con->followup_highest_sequence = con->una + payload_len;
#endif
    con->una += payload_len;''')
    for path, label, field, insert, code in (
        (cpp / 'link_layer/switch.cu', 'SwitchWork', 'switch_drops', '        params->drop_frame_num[tid] = drop_frame_num;',
         '        if (drop_frame_num) atomicAdd(g_switch_work, (unsigned long long)drop_frame_num);'),
        (cpp / 'channel/p2p_channel.cu', 'ChannelWork', 'link_transmission_admissions',
         '        egress->append_elements(ingress, transmit_num);',
         '        if (transmit_num) atomicAdd(g_channel_work, (unsigned long long)transmit_num);'),
    ):
        name = f'g_{label.removesuffix("Work").lower()}_work'
        counter_header(path, label, name, (field,))
        replace(path, f'\n{insert}\n', f'\n#ifdef FOLLOWUP_WORK\n{code}\n#endif\n{insert}\n')
    main = tree / 'main.cc'
    replace(main, 'int main(int argc, char *argv[])', '''#ifdef FOLLOWUP_WORK
void ReportTcpWork();
void ReportSwitchWork();
void ReportChannelWork();
#endif
int main(int argc, char *argv[])''')
    replace(main, '    topology.RecordFlowResults(output_file_name);', '''#ifdef FOLLOWUP_WORK
    ReportTcpWork();
    ReportSwitchWork();
    ReportChannelWork();
#endif
    topology.RecordFlowResults(output_file_name);''')


def instrument_twins(tree):
    src = tree / 'src'
    fields = ('original_data', 'ack', 'other_control', 'syn', 'fin_only', 'data_with_fin', 'link_transmission_starts')
    step = src / 'step.cuh'
    marker = '// ------------------------------------------------------------------ egress FIFO'
    replace(step, marker, f'__device__ unsigned long long g_tcp_work[{len(fields)}];\n\n' + marker)
    replace(step, '            sent++;\n            link_free = end;',
            '            atomicAdd(g_tcp_work + 6, 1ull);\n            sent++;\n            link_free = end;')
    replace(step, '    c.nic->enqueue(fr, now);', '''    if (flags & F_DATA)
    {
        if (flags & F_FIN) atomicAdd(g_tcp_work + 5, 1ull);
    }
    else if (flags & (F_SYN | F_FIN))
    {
        atomicAdd(g_tcp_work + 2, 1ull);
        atomicAdd(g_tcp_work + ((flags & F_SYN) ? 3 : 4), 1ull);
    }
    else if (flags & F_ACK) atomicAdd(g_tcp_work + 1, 1ull);
    else atomicAdd(g_tcp_work + 2, 1ull);
    c.nic->enqueue(fr, now);''')
    replace(step, '    if (F.rto_at[f] < 0)\n    {',
            '    if (!retx) atomicAdd(g_tcp_work, 1ull);\n    if (F.rto_at[f] < 0)\n    {')
    main = src / 'main.cu'
    function = counter_report('TcpWork', 'kfull::g_tcp_work', fields).replace('#ifdef FOLLOWUP_WORK\n', '').replace('#endif\n', '')
    function = function.replace('    printf(', f'''#ifndef TWINS_LINK_TIMING
    unsigned long long basic[{len(fields)}];
    if (cudaMemcpyFromSymbol(basic, kbasic::g_tcp_work, sizeof(basic)) != cudaSuccess)
        abort();
    for (int i = 0; i < {len(fields)}; ++i)
        record[i] += basic[i];
#endif
    printf(''')
    replace(main, 'int main(int argc, char **argv)', function + '\nint main(int argc, char **argv)')
    replace(main, '    report_exit_snapshot(s, out_rec);', '    report_exit_snapshot(s, out_rec);\n    ReportTcpWork();')


def build(tree, target, flags, receipt_dir, label):
    build_dir = tree / f'build-{label}'
    with (receipt_dir / f'{label}.log').open('wb') as log:
        subprocess.run(['cmake', '-S', tree, '-B', build_dir, '-G', 'Ninja', '-DCMAKE_CUDA_ARCHITECTURES=120',
                        '-DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc', *flags], stdout=log, stderr=subprocess.STDOUT,
                       check=True, timeout=180)
        subprocess.run(['cmake', '--build', build_dir, '--target', target, '-j2'], stdout=log, stderr=subprocess.STDOUT,
                       check=True, timeout=600)
    return build_dir / target


def prepare_builds(out):
    out.mkdir()
    sources = out / 'sources'
    sources.mkdir()
    ge = sources / 'GeDES'
    original_ge = baseline() / 'GeDES'
    shutil.copytree(original_ge, ge, ignore=shutil.ignore_patterns('cmake', 'build'))
    adapt_gedes(ge)
    adapt_gedes_receive(ge)
    instrument_gedes(ge)
    twins = sources / 'twins'
    twin_source = ROOT / 'exit_snapshot/20261123T085429Z/builds/twins'
    shutil.copytree(twin_source, twins, ignore=shutil.ignore_patterns('cmake', 'build'))
    instrument_twins(twins)
    with ThreadPoolExecutor(max_workers=2) as pool:
        twin = pool.submit(build, twins, 'twins', ['-DTWINS_GPU_BACKEND=CUDA'], out, 'twins-diagnostic')
        ge_args = ['-DGEDES_GPU_BACKEND=CUDA', '-DCMAKE_CXX_FLAGS=-DENABLE_DCTCP=0']
        clean = build(ge, 'GeDES', [*ge_args, '-DCMAKE_CUDA_FLAGS=-DENABLE_DCTCP=0'], out, 'gedes-clean')
        flags = '-DENABLE_DCTCP=0 -DFOLLOWUP_WORK=1'
        diagnostic = build(ge, 'GeDES', [f'-DCMAKE_CXX_FLAGS={flags}', f'-DCMAKE_CUDA_FLAGS={flags}', ge_args[0]],
                           out, 'gedes-diagnostic')
        binaries = [clean, diagnostic, twin.result()]
    save(out / 'binaries.json', {str(path): sha(path) for path in binaries})


if __name__ == '__main__':
    prepare_builds(Path(sys.argv[1]).resolve())
