"""Build the author's ConWeave with a deterministic transmit-side fixture.

The original RxToR pipeline and VOQ still consume, schedule and forward real packets.
The fixture replaces TxToR path selection and disables its two control replies.
"""
import argparse
import json
import os
import subprocess
import time
from pathlib import Path


def instrument(source):
    text = source.read_text()
    anchor = "    // get SlbControlTag from packet"
    fixture = """    // Experiment only: exactly one flow, one prescribed epoch and path change.
    const char *mode = std::getenv("TWINS_CW_FIXTURE");
    if (mode && ch.l3Prot == 0x11 && m_switch_id == srcToRId) {
        static uint64_t tailTime = 0;
        static bool tailSeen = false;
        const bool transition = std::string(mode) != "control";
        const bool tail = transition && ch.udp.seq == 2000 && !tailSeen;
        const bool phase1 = transition && ch.udp.seq > 2000;
        if (tail) {
            tailSeen = true;
            tailTime = now.GetNanoSeconds();
        }
        uint32_t firstPort = phase1 ? 3 : 2;
        if (std::string(mode) == "timeout" && tail) {
            std::printf("CW %lld %u drop %u %u\\n",
                        (long long)now.GetNanoSeconds(), m_switch_id, ch.udp.seq, firstPort);
            return;
        }
        uint32_t path = 0;
        SetOutPortToPath(path, 0, firstPort);
        SetOutPortToPath(path, 1, 2);
        conweaveDataTag.SetPathId(path);
        conweaveDataTag.SetHopCount(0);
        conweaveDataTag.SetEpoch(1);
        conweaveDataTag.SetPhase(phase1);
        conweaveDataTag.SetTimestampTx(now.GetNanoSeconds());
        conweaveDataTag.SetTimestampTail(tailTime);
        conweaveDataTag.SetFlagData(tail ? ConWeaveDataTag::TAIL : ConWeaveDataTag::DATA);
        p->AddPacketTag(conweaveDataTag);
        std::printf("CW %lld %u route %u %u\\n",
                    (long long)now.GetNanoSeconds(), m_switch_id, ch.udp.seq, firstPort);
        DoSwitchSend(p, ch, firstPort, ch.udp.pg);
        return;
    }

"""
    rewrites = {
        anchor: fixture + anchor,
        "if (m_pathAwareRerouting) {\n                    if": "if (!mode && m_pathAwareRerouting) {\n                    if",
        "                    SendReply(p, ch, ConWeaveReplyTag::TAIL,\n":
            "                    if (!mode) SendReply(p, ch, ConWeaveReplyTag::TAIL,\n",
        "                        voq->second.RescheduleFlush(":
            """                            if (mode) std::printf("CW %lld %u schedule %u %llu\\n",
                                (long long)now.GetNanoSeconds(), m_switch_id, ch.udp.seq,
                                (unsigned long long)(now.GetNanoSeconds() + rx_md.timeExpectedToFlush));
                        voq->second.RescheduleFlush(""",
        "                            voq.Set(rx_md.pkt_flowkey, ch.dip,":
            """                            if (mode) std::printf("CW %lld %u schedule %u %llu\\n",
                                (long long)now.GetNanoSeconds(), m_switch_id, ch.udp.seq,
                                (unsigned long long)(now.GetNanoSeconds() + rx_md.timeExpectedToFlush));
                            voq.Set(rx_md.pkt_flowkey, ch.dip,""",
        "                    m_voqMap[rx_md.pkt_flowkey].Enqueue(p);":
            """                    if (mode) std::printf("CW %lld %u hold %u %u\\n",
                        (long long)now.GetNanoSeconds(), m_switch_id, ch.udp.seq,
                        m_voqMap[rx_md.pkt_flowkey].getQueueSize() + 1);
                    m_voqMap[rx_md.pkt_flowkey].Enqueue(p);""",
        "    m_historyVOQSize.push_back(voqSize);":
            """    if (std::getenv("TWINS_CW_FIXTURE")) std::printf("CW %lld %u flush %u 0\\n",
        (long long)Simulator::Now().GetNanoSeconds(), m_switch_id, voqSize);
    m_historyVOQSize.push_back(voqSize);""",
    }
    for original, replacement in rewrites.items():
        if text.count(original) != 1:
            raise ValueError(f"author source did not match anchor {original!r}")
        text = text.replace(original, replacement)
    return "#include <cstdlib>\n#include <cstdio>\n" + text


def execute(argv, output, env=None):
    started = time.monotonic()
    with output.open("w") as stream:
        rc = subprocess.run(["timeout", "180", *argv], cwd=output.parent, env=env,
                            stdout=stream, stderr=subprocess.STDOUT).returncode
    return dict(command=list(map(str, argv)), rc=rc, wall_s=time.monotonic() - started)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for path in ("stock", "routing", "driver", "inputs", "out"):
        parser.add_argument(path, type=lambda value: Path(value).resolve())
    args = parser.parse_args()
    args.out.mkdir()
    routing = args.out / "conweave-observed.cc"
    routing.write_text(instrument(args.routing))
    libraries = args.stock / "build"
    driver = args.out / "reference-driver.cc"
    source = args.driver.read_text()
    anchor = "    // topology_file\n    bool found_topo2bdpMap"
    assert source.count(anchor) == 1
    driver.write_text(source.replace(anchor, '    topo2bdpMap["conweave-transition"] = 104000;\n' + anchor))
    binary = args.out / "reference"
    # The interposed translation unit and shared library would otherwise
    # both construct and destroy the same exported std::vector.
    argv = ["g++", "-O3", "-DNDEBUG", "-std=c++11", "-Dm_historyVOQSize=m_fixtureHistoryVOQSize", f"-I{libraries}",
            f"-I{args.routing.parent}", driver, routing, "-Wl,--export-dynamic",
            "-Wl,--no-as-needed", *sorted(libraries.glob("libns3.19-*-optimized.so")),
            "-Wl,--as-needed", "-lpthread", "-lrt", "-o", binary]
    build = execute(argv, args.out / "build.log")
    (args.out / "build.json").write_text(json.dumps(build, indent=2) + "\n")
    if build["rc"]:
        raise SystemExit(build["rc"])
    for mode in ("control", "tail", "timeout"):
        run = args.out / mode
        run.mkdir()
        config = (args.inputs / "conweave.cfg").read_text()
        config += f"\nTOPOLOGY_FILE {args.inputs / 'conweave-transition.topo'}\nFLOW_FILE {args.inputs / 'flow.native'}\n"
        (run / "config").write_text(config)
        env = dict(os.environ, LD_LIBRARY_PATH=str(libraries), TWINS_CW_FIXTURE=mode,
                   TWINS_IRN_LOW_NS="100000", TWINS_TXTRACE="wire.raw")
        receipt = execute([binary, "config"], run / "run.log", env)
        (run / "run.json").write_text(json.dumps(receipt, indent=2) + "\n")
        print(mode, receipt["rc"], f"{receipt['wall_s']:.3f}", flush=True)


if __name__ == "__main__":
    main()
