"""Build a read-only scheduler observer for the author's frozen IRN driver."""
import argparse
import subprocess
from pathlib import Path


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("driver", "libraries", "out"):
        p.add_argument(name, type=lambda value: Path(value).resolve())
    args = p.parse_args()
    args.out.mkdir()
    src = args.driver.read_text()
    app = "        appCon.Start(Seconds(Time(0)));"
    declaration = "    // schedule the next time to run this function"
    schedule = """        Simulator::Schedule(Seconds(flow_input.start_time) - Simulator::Now(), &ScheduleFlowInputs,
                            infile);"""
    for anchor in (app, declaration, schedule):
        assert src.count(anchor) == 1, anchor
    src = src.replace(app, app + """
        printf("NATIVE_ROOT %u %u %u %u %u %llu\\n", flow_input.idx, src, dst, sport, dport,
               (unsigned long long)Simulator::Now().GetTimeStep());""")
    src = src.replace(schedule, """        const Time at = Seconds(flow_input.start_time);
        const EventId group = Simulator::Schedule(at - Simulator::Now(), &ScheduleFlowInputs, infile);
        printf("NATIVE_GROUP %u %llu %llu %u\\n", flow_input.idx,
               (unsigned long long)Simulator::Now().GetTimeStep(),
               (unsigned long long)at.GetTimeStep(), group.GetUid());""")
    source = args.out / "reference-observed.cc"
    source.write_text(src)
    cmd = ["g++", "-O3", "-DNDEBUG", "-std=c++11", f"-I{args.libraries}", source,
           "-Wl,--no-as-needed", *sorted(args.libraries.glob("libns3.19-*-optimized.so")),
           "-Wl,--as-needed", "-lpthread", "-lrt", "-o", args.out / "reference"]
    with (args.out / "build.log").open("w") as stream:
        subprocess.run(cmd, stdout=stream, stderr=subprocess.STDOUT, check=True)


if __name__ == "__main__":
    main()
