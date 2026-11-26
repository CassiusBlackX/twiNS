// The probe executes the production fragment handlers, FIFO, event histories and tie comparator.
// UDP delivery is observed at the reassembly boundary instead of entering the TCP state machine.
#define main twins_original_main
#include "../../src/main.cu"
#undef main

__global__ void fragment_probe(Sim s, const Frame *input, int count)
{
    if (blockIdx.x || threadIdx.x) return;
    kfull::Due due[16];
    int nd = 0, id = 0, received = 0;
    kfull::Fifo nic{};
    nic.ring = s.hs.ring;
    nic.last_chain = s.hs.last_chain;
    nic.slot_end = INT64_MAX;
    nic.red_k = 30;
    nic.qcap = 6;
    nic.id = 1;
    nic.s = &s;
    kfull::HostCtx c{&s, &nic, 1, 0, 0, {}, due, &nd, &id, INT64_MAX};
    while (received < count || id < nd)
    {
        const int64_t arrival = received < count ? input[received].ts : INT64_MAX;
        const int64_t timeout = id < nd ? due[id].at : INT64_MAX;
        const int64_t now = arrival < timeout ? arrival : timeout;
        nic.advance(now);
        if (timeout < arrival || (timeout == arrival &&
            kfull::timer_before_arrival(s, s.fl, due[id], input[received], 1)))
        {
            const auto event = due[id++];
            const auto *chain = kfull::due_chain(s, event, 1);
            for (int i = 0; i < CHAIN; ++i) c.echain[i] = chain[i];
            if (kfull::due_current(c, event)) kfull::on_fragment_timeout(c, event.flow, now);
        }
        else
        {
            const auto &packet = input[received++];
            printf("INJECT %lld %d\n", (long long)now, packet.fragment == IP_HEAD);
            kfull::chain_push(c.echain, kfull::arrival_code(packet), packet.chain);
            kfull::on_fragment(c, packet, now);
        }
    }
    printf("RESULT %llu %llu\n", s.ev[38], s.ev[37]);
    printf("FLAGS %llu %llu %llu %llu\n", s.ev[5], s.ev[7], s.ev[25], s.ev[36]);
}

int main(int argc, char **argv)
{
    const int64_t offset = argc > 1 ? std::stoll(argv[1]) : -1;
    const bool late = argc > 2 && std::string(argv[2]) == "late";
    const bool missing = argc > 2 && std::string(argv[2]) == "missing";
    const bool stale = argc > 2 && std::string(argv[2]) == "stale";
    Sim s{};
    s.H = s.H1 = s.L = 2;
    s.F = 1;
    s.ecmp_n = 1;
    s.ring = 8;
    s.slot_ps = SLOT_PS;
    s.max_tx = 300;
    s.banks = 4;
    s.msg = dalloc<Frame>(size_t(s.banks) * s.L * s.max_tx);
    s.msg_cnt = dupload(std::vector<long long>(s.banks * s.L, -(1ll << 16)));
    s.rate_index = dalloc<int>(2);
    std::vector<int64_t> serialization(DATA_FRAME + 1);
    for (int i = 0; i <= DATA_FRAME; ++i) serialization[i] = i * PS_PER_BYTE;
    s.tx_ps = dupload(serialization);
    s.delay_ps = dupload(std::vector<int64_t>(2, PROP_PS));
    s.last_arrival_slot = dalloc<unsigned long long>(1);
    s.hs.ring = dalloc<Frame>(8);
    s.hs.last_chain = dalloc<ChainWord>(CHAIN);
    s.hs.timer_sequence = dalloc<uint64_t>(2);
    s.fragment_timer_cap = 4;
    s.fragment_timers = dalloc<FragmentTimer>(8);
    s.fragment_head = dalloc<uint64_t>(2);
    s.fragment_tail = dalloc<uint64_t>(2);
    s.fragment_timer_ovf_w = dalloc<unsigned long long>(1);
    s.due_offset = dupload(std::vector<uint64_t>{0,16,32});
    s.due_ovf_w = dalloc<unsigned long long>(1);
    s.active_ovf_w = dalloc<unsigned long long>(1);
    s.ties_w = dalloc<unsigned long long>(1);
    s.ev = dalloc<unsigned long long>(40);
    s.fl.snd = dalloc<SndRec>(1);
    s.fl.src.base = reinterpret_cast<char *>(s.fl.snd) + offsetof(SndRec, src);
    s.fl.dst.base = reinterpret_cast<char *>(s.fl.snd) + offsetof(SndRec, dst);
    s.fl.ip_pair = dalloc<uint32_t>(2);
    s.fl.pairs = dupload(std::vector<FragmentPair>{{0,63,0}});
    s.fl.fragments = dalloc<FragmentEntry>(64);
    s.fl.icmp_hash = dalloc<uint32_t>(1);
    std::vector<Frame> input(stale ? 3 : missing ? 1 : 2);
    for (int i = 0; i < int(input.size()); ++i)
    {
        auto &packet = input[i];
        packet.ts = stale ? (i == 0 ? 1000000000000ll : i == 1 ? 1500000000000ll : 2000000000000ll)
                          : i == 0 ? 1000000000000ll : 31000000000000ll + offset;
        packet.arrival_lead = i == 1 && late ? 1000000000000ll + offset : packet.ts;
        const bool head = stale ? i < 2 : i == 0;
        packet.fragment = head ? IP_HEAD : IP_TAIL;
        packet.ip_id = stale && i != 1 ? 41 : 42;
        packet.len = head ? 38 : 30;
        packet.tcp_payload = 16;
        kfull::chain_set_root(packet.chain, i + 1);
    }
    auto *packets = dupload(input);
    fragment_probe<<<1,1>>>(s, packets, input.size());
    CHECK(cudaGetLastError());
    CHECK(cudaDeviceSynchronize());
}
