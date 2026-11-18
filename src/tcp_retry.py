"""Default TCP entry point. Retry from pristine inputs, scoped to affected worlds."""
import csv
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def run(binary, args):
    start = time.monotonic()
    sim = 0.0
    total_flows = 0
    world_arg = next((v for v in args if v.startswith("--worlds=")), None)
    lines = None
    if world_arg:
        lines = [line for line in Path(world_arg.split("=", 1)[1]).read_text().splitlines()
                 if line and not line.startswith("#")]
        args = [v for v in args if v != world_arg]
    sizes = [64, 128, 256, 512]
    with tempfile.TemporaryDirectory() as tmp:
        def attempt(ids, level, expanded):
            nonlocal sim, total_flows
            cmd = [str(binary if level == 0 else binary.with_name(f"twins{sizes[level]}")),
                   "--single-pass", *args]
            if expanded:
                cmd.append("--size-active")
            if lines is not None:
                worlds = Path(tmp) / "worlds"
                worlds.write_text("".join(lines[i] + "\n" for i in ids))
                cmd.append(f"--worlds={worlds}")
            t = time.monotonic()
            result = subprocess.run(cmd, capture_output=True, text=True)
            log = result.stdout
            if result.returncode:
                if len(ids) > 1 and "out of memory" in result.stderr.lower():
                    middle = len(ids) // 2
                    return attempt(ids[:middle], level, expanded) and attempt(ids[middle:], level, expanded)
                sys.stderr.write(result.stdout + result.stderr)
                return False
            duration = re.search(r"Simulation Duration: ([\d.]+)s", log)
            if not duration:
                sys.stderr.write(log + result.stderr)
                return False
            sim += float(duration[1])
            def count(name):
                return int(re.search(r"\b" + name + r" (\d+)", log)[1])
            def affected(field):
                if not count(field):
                    return set()
                match = re.search(r"^" + field + r" by world:([ 0-9]*)$", log, re.M)
                return {ids[int(i)] for i in match[1].split()} if match and match[1].strip() else set(ids)
            complete = re.search(r"flows (\d+) completed (\d+)", log)
            if not complete:
                sys.stderr.write(log + result.stderr)
                return False
            if level == 0 and not expanded:
                total_flows += int(complete[1])
            active = affected("active-ovf")
            ties = affected("undecided-ties")
            layout = re.search(r"(\d+)-byte frame", log)
            frame_bytes = int(layout[1]) if layout else sizes[level]
            print(f"TCP pass bytes={frame_bytes} sized_active={int(expanded)} worlds={len(ids)} "
                  f"active_ovf={count('active-ovf')} ties={count('undecided-ties')} "
                  f"wall_s={time.monotonic()-t:.6f}", flush=True)
            if any(count(f) for f in ("ooo-ovf", "rtt-ovf", "ring-ovf", "due-ovf")):
                sys.stderr.write(log + result.stderr)
                return False
            if not active and not ties:
                sys.stdout.write(log.replace(duration[0] + "\n", ""))
                sys.stderr.write(result.stderr)
                return complete[1] == complete[2]
            if (active and expanded) or (ties and level + 1 == len(sizes)):
                sys.stderr.write(log + result.stderr)
                return False
            # Worlds with no retry keep their first-pass outputs. A separate
            # stalled world must still make the batch fail.
            for world in set(ids) - active - ties:
                path = dict(item.split("=", 1) for item in lines[world].split(","))["out"]
                with open(path, newline="") as stream:
                    if any(int(row["completed_ns"]) < 0 for row in csv.DictReader(stream)):
                        sys.stderr.write(f"world {world} has incomplete flows\n")
                        return False
            for group in (active - ties, ties - active, active & ties):
                if group and not attempt(sorted(group), level + int(bool(group & ties)),
                                         expanded or bool(group & active)):
                    return False
            return True
        ok = attempt(list(range(len(lines))) if lines is not None else [0], 0, False)
    if ok and lines is not None:
        print(f"flows {total_flows} completed {total_flows} across {len(lines)} worlds")
    print(f"Simulation Duration: {sim:.6f}s\nTotal TCP wall: {time.monotonic()-start:.6f}s")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run(Path(sys.argv[1]), sys.argv[2:]))
