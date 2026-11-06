# UB SuperPoD load balancing: packet spray across planes, ports and paths (study notes)

The task: a packet-spray-based load-balancing and congestion-control scheme for a fully pooled UB
SuperPoD. It must stay balanced under link sub-health, traffic conflicts and plane imbalance.

Targets:
- point to point ≥ 95%;
- all-to-all ≥ 90%;
- mixed ≥ 90% per device type;
- after a single plane or port fault, or lane loss, a bandwidth loss < 1/N.

Code:
- ns-3-ub branch `lb-study`, local only, never pushed: `scratch/lb-study/gen_superpod.py`
  (cases) and `lb_metrics.py` (utilisation).
- Runs: 9950X, `~/Videos/netsim/lb`.
- Cases: `out/ub/lb/`.

## Scenario (chosen to be realistic, not favourable)

- **Pod:** 64 NPUs, 8 CPUs, 4 NIC/DPUs and 4 storage devices; 8 planes, one 400 Gbps UB switch per
  plane (single tier); 20 ns links.
- **Ports per device (heterogeneous plane reach):**

  | device | ports / planes |
  |---|---|
  | NPU | 8, one per plane |
  | CPU | 2, planes 0–1 |
  | NIC/DPU | 4, planes 0–3 |
  | storage | 4, planes 4–7 |

- **Traffic:**
  - p2p: an NPU permutation, 64 MB each.
  - all-to-all: 1 MB per NPU pair.
  - mixed: the all-to-all, plus CPU→NPU H2D (8 MB), NPU←storage KVCache reads (4 MB), and 512 control
    messages of 64 B–4 KB, all at once.
- **Sub-health (static, i.e. the state after detection):**
  - one NPU port at half rate (lane loss);
  - one plane down (N = 8).
  - Dynamic failure at a time t needs the `link_event.csv` patch; still to do.
- **Metric:** a device's transmitted wire bytes / (its active window × the sum of its port rates).
  The 0.2–0.4% over 1.0 comes from how the window is defined.

## Baselines (unmodified ns-3-ub mechanisms), 2026-11-07

| case | per-flow (one TP, one plane) | TP group, 64 KB segments | TP group, 4 KB segments |
|---|---|---|---|
| p2p healthy | 12.7% (3202 Gbps is the 8-port line rate) | 100% | – |
| a2a healthy | 62% (min 44%) | 100% | – |
| a2a, NPU0 port0 at 1/2 rate | – | NPU0 1603 Gbps (−50%; only 6.25% of capacity lost) | NPU0 3001 Gbps (−6.2%) |
| a2a, plane 3 down | – | 2415 Gbps (loss 24.7% > 1/8) | 2805 Gbps (loss 12.5% = 1/8) |
| p2p, plane 3 down | – | 2788 Gbps (loss 12.9%) | – |
| mixed | NPU 62%, CPU 68%, storage 59% | NPU 100%, CPU 86%, storage 78% | NPU 100%, CPU 89%, storage 69% |

## Findings so far

1. **Per-flow pinning** (the RDMA-style "one QP per port" when a task picks one TP) wastes 7/8 of a
   device on p2p and collides on all-to-all.
2. **The TP group spreads 64 KB segments over the planes** but does not weigh them by rate. A
   half-rate port halves the device; a lost plane costs about twice 1/N. This is the bandwidth loss
   the requirement rules out.
3. **Per-packet granularity reaches the 1/N optimum.** Spreading with the existing per-member
   backpressure (two segments in flight per TP) at 4 KB does so on the fault cases. But each 4 KB
   then carries its own transaction response; the ns-3-ub run was 4–5x slower and the wire
   overhead is higher. The clean design is per-packet spraying inside one transport connection
   (a multi-port TP), with the member choice weighted by available rate and backlog.
4. **Mixed traffic needs a real upper bound before "90%" means anything.** Storage and CPU ports are
   partly limited by NPU-ingress contention, not by balancing. Next: compute each device's max-min
   fair share for the scenario and report utilisation against it.

## Next

- `lb-study` C++ work:
  - `link_event.csv` for runtime rate change, fail and recover, with routing exclusion;
  - a multi-port TP (per-packet member choice: weighted by rate, credit- and backlog-aware);
  - DCQCN's line rate read per port;
  - rate-normalised ADAPTIVE for two-tier planes.
- Scenarios: two-tier planes (256 NPUs), dynamic faults (near and far end, read and write),
  ring collectives.
- Port the scheme to TwiNS-UB: exact against the modified ns-3-ub, then sweeps.
