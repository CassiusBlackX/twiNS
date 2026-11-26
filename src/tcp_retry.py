"""Default TCP entry point. Retry from pristine inputs, scoped to affected worlds."""
import csv
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def initial_history_level(args):
    options = dict(arg[2:].split("=", 1) for arg in args if arg.startswith("--") and "=" in arg)
    if options.get("topo") != "graph" or options.get("tcp", "dctcp") != "dctcp":
        return 0
    if "worlds" in options or "topofile" not in options:
        return 0
    hosts = 0
    general_timing = False
    try:
        for line in Path(options["topofile"]).read_text().splitlines():
            fields = line.split("#", 1)[0].split()
            if fields and fields[0] == "hosts":
                hosts = int(fields[1])
            elif fields and fields[0] == "link" and len(fields) > 3:
                general_timing |= tuple(map(int, fields[3:])) != (100000000000, 1000000)
    except (OSError, ValueError, IndexError):
        return 0
    if not 128 <= hosts <= 1024:
        return 0
    return 3 if general_timing else 2


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
    initial = next((v for v in args if v.startswith("--start-history=")), None)
    level = 0
    if initial:
        profiles = [str(size) for size in sizes]
        value = initial.split("=", 1)[1]
        if value not in profiles:
            raise ValueError("--start-history accepts 64, 128, 256 or 512")
        level = profiles.index(value)
        args = [v for v in args if v != initial]
    elif lines is None:
        level = initial_history_level(args)
    initial_level = level
    fragment_arg = next((v for v in args if v.startswith("--fragment-timers=")), None)
    fragment_capacity = int(fragment_arg.split("=", 1)[1]) if fragment_arg else 256
    if fragment_arg:
        args = [v for v in args if v != fragment_arg]
    auto_oom_retried = False
    transport = dict(arg[2:].split("=", 1) for arg in args if arg.startswith("--tcp-") and "=" in arg)
    requested = ("tcp-sack", "tcp-timestamps")

    def option_world(world):
        effective = transport.copy()
        if lines is not None:
            effective.update(item.split("=", 1) for item in lines[world].split(",") if "=" in item)
        return any(effective.get(key) == "1" for key in requested)

    def extended(ids):
        return "--topo=graph" in args and any(option_world(world) for world in ids)

    with tempfile.TemporaryDirectory() as tmp:
        def attempt(ids, level, expanded, fragment_cap=fragment_capacity):
            nonlocal sim, total_flows, auto_oom_retried
            if level == len(sizes):
                target = binary.with_name("twins2048-options-links")
            else:
                target = binary if level == 0 else binary.with_name(f"twins{sizes[level]}")
                if "--qtrace-options" in args or any(option_world(world) for world in ids):
                    target = target.with_name(target.name + "-options")
            cmd = [str(target), "--single-pass", *args]
            cmd.append(f"--fragment-timers={fragment_cap}")
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
                if initial is None and not auto_oom_retried and level == initial_level and level > 0 and "out of memory" in result.stderr.lower():
                    auto_oom_retried = True
                    return attempt(ids, 0, expanded, fragment_cap)
                if len(ids) > 1 and "out of memory" in result.stderr.lower():
                    middle = len(ids) // 2
                    return attempt(ids[:middle], level, expanded, fragment_cap) and attempt(ids[middle:], level, expanded, fragment_cap)
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
            if level == initial_level and not expanded and fragment_cap == fragment_capacity:
                total_flows += int(complete[1])
            active = affected("active-ovf") | affected("due-ovf")
            fragment = affected("fragment-timer-ovf")
            ties = affected("undecided-ties")
            layout = re.search(r"\bframe_bytes=(\d+)", log)
            frame_bytes = int(layout[1]) if layout else 0
            print(f"TCP pass bytes={frame_bytes} sized_active={int(expanded)} worlds={len(ids)} "
                  f"active_ovf={count('active-ovf')} due_ovf={count('due-ovf')} "
                  f"fragment_timer_ovf={count('fragment-timer-ovf')} fragment_cap={fragment_cap} ties={count('undecided-ties')} "
                  f"wall_s={time.monotonic()-t:.6f}", flush=True)
            if active or ties:
                for line in log.splitlines():
                    if line.startswith(("TCP arrival ring", "TCP compared runs", "TCP heterogeneous")):
                        print(line, flush=True)
            if any(count(f) for f in ("ooo-ovf", "rtt-ovf", "ring-ovf")):
                sys.stderr.write(log + result.stderr)
                return False
            if fragment:
                if fragment_cap > (1 << 29):
                    sys.stderr.write(log + result.stderr)
                    return False
                return attempt(ids, level, expanded, fragment_cap * 2)
            if not active and not ties:
                sys.stdout.write(log.replace(duration[0] + "\n", ""))
                sys.stderr.write(result.stderr)
                return complete[1] == complete[2]
            if (active and expanded) or (ties and level + 1 >= len(sizes) + bool(extended(ties))):
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
                                         expanded or bool(group & active), fragment_cap):
                    return False
            return True
        ok = attempt(list(range(len(lines))) if lines is not None else [0], level, False)
    if ok and lines is not None:
        print(f"flows {total_flows} completed {total_flows} across {len(lines)} worlds")
    print(f"Simulation Duration: {sim:.6f}s\nTotal TCP wall: {time.monotonic()-start:.6f}s")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run(Path(sys.argv[1]), sys.argv[2:]))
