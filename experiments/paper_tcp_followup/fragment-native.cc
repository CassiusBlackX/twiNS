// Exercise the installed ns-3 IPv4 implementation through its public receive path.
#include "ns3/core-module.h"
#include "ns3/internet-module.h"
#include "ns3/network-module.h"
#include "ns3/point-to-point-module.h"
#include <iostream>
using namespace ns3;

static Ptr<Ipv4L3Protocol> receiver;
static Ptr<NetDevice> incoming;
static Ipv4Address source, destination;
static Ptr<Packet> whole;
static uint32_t delivered = 0, expired = 0;
static void InjectWithId(bool head, uint16_t identification)
{
    auto packet = whole->CreateFragment(head ? 0 : 16, head ? 16 : 8);
    Ipv4Header ip;
    ip.SetSource(source);
    ip.SetDestination(destination);
    ip.SetProtocol(17);
    ip.SetIdentification(identification);
    ip.SetTtl(64);
    ip.SetPayloadSize(packet->GetSize());
    ip.SetFragmentOffset(head ? 0 : 16);
    if (head) ip.SetMoreFragments(); else ip.SetLastFragment();
    packet->AddHeader(ip);
    std::cout << "INJECT " << Simulator::Now().GetPicoSeconds() << ' ' << head << std::endl;
    receiver->Receive(incoming, packet, 0x0800, incoming->GetAddress(), incoming->GetAddress(), NetDevice::PACKET_HOST);
}
static void Inject(bool head) { InjectWithId(head, 42); }
static void Receive(Ptr<Socket> socket)
{
    while (auto packet = socket->Recv())
    {
        ++delivered;
        std::cout << "DELIVER " << Simulator::Now().GetPicoSeconds() << ' ' << packet->GetSize() << std::endl;
    }
}
static void Drop(const Ipv4Header& ip, Ptr<const Packet> packet, Ipv4L3Protocol::DropReason reason, Ptr<Ipv4>, uint32_t)
{
    if (reason == Ipv4L3Protocol::DROP_FRAGMENT_TIMEOUT)
    {
        ++expired;
        std::cout << "EXPIRE " << Simulator::Now().GetPicoSeconds() << ' ' << ip.GetIdentification()
                  << ' ' << packet->GetSize() << std::endl;
    }
}
static void Tx(Ptr<const Packet> packet)
{
    std::cout << "WIRE " << Simulator::Now().GetPicoSeconds() << ' ' << packet->GetSize() << std::endl;
}
int main(int argc, char** argv)
{
    int64_t offset = -1;
    bool late = false, missing = false, stale = false;
    CommandLine cmd;
    cmd.AddValue("offset", "Tail arrival relative to reassembly expiry, ps", offset);
    cmd.AddValue("late", "Create the tail event after the native expiry event", late);
    cmd.AddValue("missing", "Never send the tail", missing);
    cmd.AddValue("stale", "Complete the first datagram while a second remains incomplete", stale);
    cmd.Parse(argc, argv);
    Time::SetResolution(Time::PS);
    NodeContainer nodes;
    nodes.Create(2);
    InternetStackHelper stack;
    stack.Install(nodes);
    PointToPointHelper link;
    link.SetDeviceAttribute("DataRate", StringValue("100Gbps"));
    link.SetChannelAttribute("Delay", TimeValue(MicroSeconds(1)));
    auto devices = link.Install(nodes);
    Ipv4AddressHelper addresses;
    addresses.SetBase("10.0.0.0", "255.255.255.0");
    auto interfaces = addresses.Assign(devices);
    source = interfaces.GetAddress(0);
    destination = interfaces.GetAddress(1);
    receiver = nodes.Get(1)->GetObject<Ipv4L3Protocol>();
    incoming = devices.Get(1);
    receiver->TraceConnectWithoutContext("Drop", MakeCallback(&Drop));
    incoming->TraceConnectWithoutContext("PhyTxBegin", MakeCallback(&Tx));
    auto socket = Socket::CreateSocket(nodes.Get(1), UdpSocketFactory::GetTypeId());
    socket->Bind(InetSocketAddress(destination, 9999));
    socket->SetRecvCallback(MakeCallback(&Receive));
    whole = Create<Packet>(16);
    UdpHeader udp;
    udp.SetSourcePort(5555);
    udp.SetDestinationPort(9999);
    whole->AddHeader(udp);
    if (stale)
    {
        Simulator::Schedule(Seconds(1), &InjectWithId, true, uint16_t(41));
        Simulator::Schedule(MilliSeconds(1500), &Inject, true);
        Simulator::Schedule(Seconds(2), &InjectWithId, false, uint16_t(41));
    }
    else Simulator::Schedule(Seconds(1), &Inject, true);
    if (!missing && !stale)
    {
        Time tail = Seconds(31) + PicoSeconds(offset);
        if (late)
            Simulator::Schedule(Seconds(30), [tail] { Simulator::Schedule(tail - Simulator::Now(), &Inject, false); });
        else
            Simulator::Schedule(tail, &Inject, false);
    }
    Simulator::Stop(Seconds(62));
    Simulator::Run();
    std::cout << "RESULT " << delivered << ' ' << expired << std::endl;
    Simulator::Destroy();
}
