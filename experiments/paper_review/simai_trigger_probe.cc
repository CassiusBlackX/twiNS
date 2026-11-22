#include <cstdint>
#include <iostream>
#include <list>
#include <mutex>
#include <thread>
#include "ns3/core-module.h"
#include "ns3/default-simulator-impl.h"
#include "ns3/network-module.h"
#include "ns3/internet-module.h"
#include "ns3/point-to-point-module.h"
#include "ns3/switch-node.h"
#include "ns3/qbb-net-device.h"
#include "ns3/qbb-channel.h"
#include "ns3/pause-header.h"
#include "ns3/simple-seq-ts-header.h"
#include "ns3/int-header.h"
using namespace ns3;

class ObservableDevice : public QbbNetDevice {
public:
    bool IsPaused(unsigned queue) const { return m_paused[queue]; }
};

static Ptr<Packet> PfcPacket(uint32_t duration) {
    auto packet = Create<Packet>();
    packet->AddHeader(PauseHeader(duration, 0, 3));
    Ipv4Header ip;
    ip.SetProtocol(0xfe);
    ip.SetSource(Ipv4Address("10.0.0.1"));
    ip.SetDestination(Ipv4Address("255.255.255.255"));
    ip.SetPayloadSize(packet->GetSize());
    ip.SetTtl(1);
    packet->AddHeader(ip);
    PppHeader ppp;
    ppp.SetProtocol(0x21);
    packet->AddHeader(ppp);
    return packet;
}

int main(int argc, char** argv) {
    std::cout << std::unitbuf;
    std::string mode = argc > 1 ? argv[1] : "uid";
    if (mode == "uid") {
        auto simulator = DynamicCast<DefaultSimulatorImpl>(Simulator::GetImplementation());
        simulator->m_uid = uint64_t(UINT32_MAX) - 1;
        std::cout << "uid_bytes " << sizeof(simulator->m_uid) << "\n";
        for (int i = 0; i < 4; ++i) {
            auto event = Simulator::Schedule(NanoSeconds(100), [i]() {
                std::cout << "executed " << i << " at " << Simulator::Now().GetNanoSeconds() << "\n";
            });
            std::cout << "scheduled " << i << " uid " << event.GetUid() << "\n";
        }
        Simulator::Run();
        Simulator::Destroy();
        return 0;
    }
    auto node = CreateObject<SwitchNode>();
    auto device = CreateObject<ObservableDevice>();
    device->SetAttribute("DataRate", DataRateValue(DataRate("100Gbps")));
    node->AddDevice(device);
    device->SetQueue(CreateObject<BEgressQueue>());
    device->Attach(CreateObject<QbbChannel>());
    if (mode == "pfc") {
        device->Receive(PfcPacket(100));
        std::cout << "pause_received state " << device->IsPaused(3) << "\n";
        device->Receive(PfcPacket(0));
        std::cout << "resume_received state " << device->IsPaused(3) << "\n";
        return device->IsPaused(3) ? 1 : 0;
    }
    if (mode == "int") {
        IntHeader::mode = IntHeader::NORMAL;
        node->SetAttribute("CcMode", UintegerValue(3));
        auto packet = Create<Packet>(128);
        SimpleSeqTsHeader seq;
        seq.SetSeq(0x100010001ull);
        seq.SetPG(3);
        packet->AddHeader(seq);
        UdpHeader udp;
        udp.SetSourcePort(10000);
        udp.SetDestinationPort(10001);
        packet->AddHeader(udp);
        Ipv4Header ip;
        ip.SetProtocol(17);
        ip.SetPayloadSize(packet->GetSize());
        packet->AddHeader(ip);
        PppHeader ppp;
        ppp.SetProtocol(0x21);
        packet->AddHeader(ppp);
        auto index = device->GetIfIndex();
        std::cout << "packet_bytes " << packet->GetSize() << " seq_header_bytes "
                  << seq.GetSerializedSize() << " int_bytes " << IntHeader::GetStaticSize() << "\n";
        node->SwitchNotifyDequeue(index, 0, packet);
        packet->RemoveHeader(ppp);
        packet->RemoveHeader(ip);
        packet->RemoveHeader(udp);
        SimpleSeqTsHeader actual;
        packet->RemoveHeader(actual);
        std::cout << "seq " << actual.GetSeq() << " pg " << actual.GetPG()
                  << " hops " << actual.ih.nhop << " rate " << actual.ih.hop[0].GetLineRate() << "\n";
        return 0;
    }
    return 2;
}
