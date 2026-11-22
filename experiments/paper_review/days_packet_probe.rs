// Diagnostic observer linked to the frozen native Days libraries.
use days::scenario::compile_config;
use days_executor::{
    run_cuda_with_observations, run_scalar_with_observations, ArrivalDisposition, CudaConfig,
    FlowGeneratorKind, ObservationMode, PacketKind,
};
use std::collections::BTreeMap;
use std::env;

#[derive(Default)]
struct Counts {
    bytes: u64,
    acked: u64,
    data: u64,
    ack: u64,
    retransmissions: u64,
    drops: u64,
    last_data_ns: u64,
    final_ack_ns: u64,
}

fn main() {
    let input = env::args().nth(1).expect("frozen scenario");
    let image = compile_config(input).expect("compile frozen scenario");
    let gpu =
        run_cuda_with_observations(&image, None, CudaConfig::default(), ObservationMode::Full)
            .expect("native CUDA full observation");
    let mut scalar = run_scalar_with_observations(&image, None, ObservationMode::Full)
        .expect("independent native scalar full observation");
    scalar.diagnostics = None;
    assert_eq!(
        gpu.result, scalar,
        "full native CUDA state and observations must equal Scalar"
    );
    let result = &gpu.result;
    let mut rows: BTreeMap<_, Counts> = image
        .flows
        .iter()
        .map(|flow| (flow.id, Counts::default()))
        .collect();
    for host in &result.host_states {
        for generator in &host.generators {
            if let FlowGeneratorKind::Tcp(tcp) = generator.kind {
                let row = rows.get_mut(&generator.flow).unwrap();
                row.bytes = tcp.total_bytes;
                row.acked = tcp.highest_ack;
            }
        }
    }
    let packets: BTreeMap<_, _> = result
        .observed_packets
        .iter()
        .map(|packet| (packet.id, packet))
        .collect();
    for packet in &result.observed_packets {
        let row = rows.get_mut(&packet.flow).unwrap();
        match packet.kind {
            PacketKind::TcpData(header) => {
                row.data += 1;
                row.retransmissions += u64::from(header.retransmission);
            }
            PacketKind::TcpAck(_) => row.ack += 1,
            other => panic!("unexpected packet type {other:?}"),
        }
    }
    for arrival in &result.arrivals {
        let packet = packets
            .get(&arrival.payload)
            .expect("observed arrival packet");
        let row = rows.get_mut(&packet.flow).unwrap();
        if arrival.disposition == ArrivalDisposition::Dropped {
            row.drops += 1;
        }
        match packet.kind {
            PacketKind::TcpData(_) if arrival.disposition == ArrivalDisposition::Delivered => {
                row.last_data_ns = row.last_data_ns.max(arrival.time_ns);
            }
            PacketKind::TcpAck(header)
                if arrival.disposition == ArrivalDisposition::Feedback
                    && header.acknowledgment >= row.bytes =>
            {
                row.final_ack_ns = row.final_ack_ns.max(arrival.time_ns);
            }
            _ => {}
        }
    }
    println!("flow,source,target,demand_bytes,acked_bytes,data_packets,ack_packets,retransmissions,drops,last_data_ns,final_ack_ns");
    for flow in &image.flows {
        let row = &rows[&flow.id];
        println!(
            "{},{},{},{},{},{},{},{},{},{},{}",
            flow.id.0,
            flow.source.0,
            flow.target.0,
            row.bytes,
            row.acked,
            row.data,
            row.ack,
            row.retransmissions,
            row.drops,
            row.last_data_ns,
            row.final_ack_ns
        );
        assert_eq!(row.bytes, row.acked);
        assert!(row.last_data_ns > 0 && row.final_ack_ns > 0);
    }
    assert!(result.pending_events.is_empty());
    eprintln!("native_scalar_cuda_full_equal=true flows={} rounds={} transitions={} pending_events={} summary={:?}",
              rows.len(), gpu.rounds, gpu.transitions, result.pending_events.len(), result.summary);
}
