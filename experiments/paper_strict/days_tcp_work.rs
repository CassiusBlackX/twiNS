//! Read TCP work from the native Days Scalar observation lane.
use days::scenario::compile_config;
use days_executor::{
    FlowGeneratorKind, ObservationMode, PacketKind, TcpTransitionInput, run_scalar_with_observations,
};
use std::collections::BTreeMap;
use std::env;
use std::fmt::{self, Write};

struct Fingerprint(u64);

impl Write for Fingerprint {
    fn write_str(&mut self, s: &str) -> fmt::Result {
        for byte in s.bytes() {
            self.0 = (self.0 ^ u64::from(byte)).wrapping_mul(1099511628211);
        }
        Ok(())
    }
}

fn main() {
    let input = env::args().nth(1).expect("frozen Days scenario");
    let image = compile_config(input).expect("compile frozen workload");
    let summary = run_scalar_with_observations(&image, None, ObservationMode::Summary)
        .expect("terminal state");
    let full = run_scalar_with_observations(&image, None, ObservationMode::Full)
        .expect("packet observation");
    assert_eq!(full.summary, summary.summary);
    assert_eq!(full.host_states, summary.host_states);
    assert_eq!(full.pending_events, summary.pending_events);
    let mut hash = Fingerprint(14695981039346656037);
    write!(&mut hash, "{summary:#?}").expect("state hash");

    let mut demand = BTreeMap::new();
    let mut complete = 0;
    let mut acked_payload = 0;
    let mut completed_payload = 0;
    for state in summary.host_states.iter().flat_map(|host| &host.generators) {
        let FlowGeneratorKind::Tcp(tcp) = state.kind else { panic!("unexpected generator"); };
        demand.insert(state.flow, tcp.total_bytes);
        acked_payload += tcp.highest_ack;
        if tcp.highest_ack == tcp.total_bytes {
            complete += 1;
            completed_payload += tcp.total_bytes;
        }
    }
    let mut original = 0;
    let mut retx = 0;
    let mut ack = 0;
    let mut control = 0;
    for packet in &full.observed_packets {
        match packet.kind {
            PacketKind::TcpData(header) if header.retransmission => retx += 1,
            PacketKind::TcpData(_) => original += 1,
            PacketKind::TcpAck(_) => ack += 1,
            _ => control += 1,
        }
    }
    let transitions = &full.diagnostics.as_ref().expect("full diagnostics").tcp_transitions;
    let mut last_complete = None;
    let mut rto = 0;
    for transition in transitions {
        match transition.input {
            TcpTransitionInput::Timeout { .. } => rto += 1,
            TcpTransitionInput::NewAck { acknowledgment, .. }
                if acknowledgment == demand[&transition.flow] => {
                    last_complete = last_complete.max(Some(transition.key.time_ns));
                }
            _ => (),
        }
    }
    let lower = full.departures.iter().map(|p| p.time_ns)
        .chain(full.arrivals.iter().map(|p| p.time_ns))
        .chain(transitions.iter().map(|p| p.key.time_ns)).max().expect("recorded events");
    let endpoint = |horizon| {
        run_scalar_with_observations(&image, Some(horizon), ObservationMode::Summary)
            .expect("bounded scalar run") == summary
    };
    let mut last_event = lower;
    if !endpoint(lower + 1) {
        let (mut lo, mut hi) = (lower + 1, image.stop_time_ns);
        while lo < hi {
            let mid = lo + (hi - lo) / 2;
            if endpoint(mid + 1) { hi = mid; } else { lo = mid + 1; }
        }
        last_event = lo;
    }
    assert!(endpoint(last_event + 1) && !endpoint(last_event));
    let latest_complete = last_complete.map_or_else(|| "null".to_owned(), |v| v.to_string());
    println!(concat!("{{\"state_fnv1a64\":\"{:016x}\",\"flows\":{},\"complete\":{},",
                      "\"acked_payload_bytes\":{},\"completed_payload_bytes\":{},",
                      "\"last_application_completion_ns\":{},\"last_executed_event_ns\":{},",
                      "\"pending_events\":{},\"stop_time_ns\":{},\"original_data\":{},\"retransmission\":{},",
                      "\"ack\":{},\"other_control\":{},\"link_transmission_completions\":{},",
                      "\"native_departed_packets\":{},\"native_sourced_packets\":{},",
                      "\"accepted_rto\":{},\"dropped_packets\":{}}}"),
             hash.0, demand.len(), complete, acked_payload, completed_payload, latest_complete, last_event,
             summary.pending_events.len(), image.stop_time_ns, original, retx, ack, control,
             full.departures.len(), full.summary.departed_packets, full.summary.sourced_packets,
             rto, full.summary.dropped_packets);
}
