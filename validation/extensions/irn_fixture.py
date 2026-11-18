"""Prepare identical within-rack clean and congested inputs for the pinned ConWeave fork."""
import argparse
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument("reference", type=Path)
p.add_argument("out", type=Path)
a = p.parse_args()
topology = "leaf_spine_128_100G_OS2.txt"
lines = (a.reference / "config" / topology).read_text().splitlines()
nodes, switches, _ = map(int,lines[0].split())
hosts = nodes - switches
def node(i):
    return f"h{i}" if i < hosts else f"s{i-hosts}"
a.out.mkdir(parents=True, exist_ok=False)
links = [list(map(int,l.split()[:2])) for l in lines[2:2+int(lines[0].split()[2])] if l.strip()]
(a.out/"graph").write_text(f"hosts {hosts}\nswitches {switches}\n" +
                            "".join(f"link {node(i)} {node(j)}\n" for i,j in links))
(a.out/topology).write_text("\n".join(lines)+"\n")
cases = {
    "one": [(0, 1, 24000)],
    "cross": [(0, 32, 21001), (1, 32, 23009), (0, 48, 31033)],
    "moderate": [(i, 15, 64000) for i in range(15)],
    "incast": [(i, 15, 150000) for i in range(15)],
    "scale": [(i, (i // 16) * 16 + 15, 150000) for i in range(128) if i % 16 != 15],
    "ecn": [(i, 95, 64000) for i in range(24)],
}
for case, inputs in cases.items():
    d = a.out / case
    d.mkdir()
    (d/"flows.csv").write_text("src,dst,flow_size,start_ns\n" + "".join(
        f"{s},{r},{sz},2000000000\n" for s,r,sz in inputs))
    (d/"ref.flows").write_text(f"{len(inputs)}\n" + "".join(
        f"{s} {r} 3 {sz} 2.0\n" for s,r,sz in inputs))
    config = dict(CC_MODE=1, LB_MODE=0, ENABLE_PFC=0, ENABLE_IRN=1, ENABLE_QCN=0,
                  SW_MONITORING_INTERVAL=100000, FLOWGEN_START_TIME=2.0,FLOWGEN_STOP_TIME=2.002,
                  BUFFER_SIZE=1, HAS_WIN=0, VAR_WIN=0, RANDOM_SEED=1, LOAD=10,
                  ALPHA_RESUME_INTERVAL=1, RATE_DECREASE_INTERVAL=4, RP_TIMER=300,
                  EWMA_GAIN=0.00390625,RATE_AI="50Mb/s",RATE_HAI="100Mb/s",
                  MIN_RATE="100Mb/s", RATE_BOUND=1, L2_CHUNK_SIZE=4000, L2_ACK_INTERVAL=1,
                  PACKET_PAYLOAD_SIZE=1000, KMIN_MAP="1 100000000000 1600",
                  KMAX_MAP="1 100000000000 1600",PMAX_MAP="1 100000000000 0",
                  ACK_HIGH_PRIO=1,FAST_RECOVERY_TIMES=1,CLAMP_TARGET_RATE=0,
                  TOPOLOGY_FILE="../"+topology, FLOW_FILE="ref.flows",
                  FCT_OUTPUT_FILE="reference.fct")
    if case == "ecn":
        config.update(ENABLE_QCN=1, KMIN_MAP="1 100000000000 5",
                      KMAX_MAP="1 100000000000 18", PMAX_MAP="1 100000000000 0.5")
    for key in ("FLOW_INPUT_FILE","CNP_OUTPUT_FILE","PFC_OUTPUT_FILE","QLEN_MON_FILE","VOQ_MON_FILE",
                "VOQ_MON_DETAIL_FILE","UPLINK_MON_FILE","CONN_MON_FILE","EST_ERROR_MON_FILE"):
        config[key] = key.lower()+".txt"
    (d/"config").write_text("".join(f"{k} {v}\n" for k,v in config.items()))
