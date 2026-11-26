"""Default TCP entry point. Retry from pristine inputs, scoped to affected worlds."""
import csv
import os
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
    layout_arg = next((v for v in args if v.startswith("--arrival-layout=")), None)
    layout_mode = layout_arg.split("=", 1)[1] if layout_arg else "auto"
    if layout_mode not in ("auto", "split", "wide"):
        raise ValueError("--arrival-layout accepts auto, split or wide")
    tail_arg = next((v for v in args if v.startswith("--arrival-tail-cap=")), None)
    tail_capacity = int(tail_arg.split("=", 1)[1]) if tail_arg else 4096
    if not 1 <= tail_capacity <= (1 << 30) or (tail_arg and layout_mode == "wide"):
        raise ValueError("--arrival-tail-cap requires split storage and a positive capacity up to 2^30")
    args = [v for v in args if v not in (layout_arg, tail_arg)]
    initial = next((v for v in args if v.startswith("--start-history=")), None)
    level = 0
    if initial:
        profiles = [str(size) for size in sizes] + ["2048"]
        value = initial.split("=", 1)[1]
        if value not in profiles:
            raise ValueError("--start-history accepts 64, 128, 256, 512 or 2048")
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

    if (layout_mode == "split" or tail_arg or level == len(sizes)) and "--topo=graph" not in args:
        raise ValueError("127-run arrival layouts require --topo=graph")
    if layout_mode == "split" or tail_arg:
        level = initial_level = len(sizes)

    with tempfile.TemporaryDirectory() as tmp:
        def attempt(ids, level, expanded, fragment_cap=fragment_capacity, tail_cap=tail_capacity, pair_scale=4):
            nonlocal sim, total_flows, auto_oom_retried
            split = level == len(sizes) and layout_mode != "wide"
            if level == len(sizes):
                # A split arena aborts its attempt as soon as it fills. Each
                # selected world therefore owns an independent invocation;
                # healthy output from a previous pass is already final.
                if split and len(ids) > 1:
                    return all(attempt([world], level, expanded, fragment_cap, tail_cap, pair_scale) for world in ids)
                target = binary.with_name("twins2048-options-split-links" if split else "twins2048-options-links")
            else:
                target = binary if level == 0 else binary.with_name(f"twins{sizes[level]}")
                if "--qtrace-options" in args or any(option_world(world) for world in ids):
                    target = target.with_name(target.name + "-options")
            cmd = [str(target), "--single-pass", *args]
            cmd.append(f"--fragment-timers={fragment_cap}")
            if pair_scale != 4:
                cmd.append(f"--fragment-pair-scale={pair_scale}")
            if expanded:
                cmd.append("--size-active")
            if lines is not None:
                worlds = Path(tmp) / "worlds"
                worlds.write_text("".join(lines[i] + "\n" for i in ids))
                cmd.append(f"--worlds={worlds}")
            t = time.monotonic()
            env = {**os.environ, "TWINS_TCP_ARRIVAL_TAIL_CAP": str(tail_cap)} if split else None
            result = subprocess.run(cmd, capture_output=True, text=True, env=env)
            log = result.stdout
            if result.returncode:
                overflow = re.search(r"TCP arrival tails: high-water (\d+) per slot, overflow ([1-9]\d*)", log)
                if split and result.returncode == 3 and overflow:
                    duration = re.search(r"Simulation Duration: ([\d.]+)s", log)
                    if duration:
                        sim += float(duration[1])
                    print(f"TCP arrival attempt cap={tail_cap} observed={overflow[1]} overflow={overflow[2]} "
                          f"loop_s={duration[1] if duration else 'unavailable'} wall_s={time.monotonic()-t:.6f}", flush=True)
                    needed = max(tail_cap + 1, int(overflow[1]))
                    grown = 1 << (needed - 1).bit_length()
                    if tail_arg is None and grown <= 1 << 30:
                        return attempt(ids, level, expanded, fragment_cap, grown, pair_scale)
                if initial is None and not auto_oom_retried and level == initial_level and level > 0 and "out of memory" in result.stderr.lower():
                    auto_oom_retried = True
                    return attempt(ids, 0, expanded, fragment_cap, tail_cap, pair_scale)
                if len(ids) > 1 and "out of memory" in result.stderr.lower():
                    middle = len(ids) // 2
                    return (attempt(ids[:middle], level, expanded, fragment_cap, tail_cap, pair_scale)
                            and attempt(ids[middle:], level, expanded, fragment_cap, tail_cap, pair_scale))
                sys.stderr.write(result.stdout + result.stderr)
                print(f"TCP worker exit: {result.returncode} ({target.name})",file=sys.stderr)
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
            active = affected("active-ovf") | affected("due-ovf")
            fragment = affected("fragment-timer-ovf")
            stored = affected("fragment-ovf") if re.search(r"\bIPv4 fragment-ovf \d+",log) else set()
            ties = affected("undecided-ties")
            layout = re.search(r"\bframe_bytes=(\d+)", log)
            frame_bytes = int(layout[1]) if layout else 0
            print(f"TCP pass bytes={frame_bytes} sized_active={int(expanded)} worlds={len(ids)} "
                  f"active_ovf={count('active-ovf')} due_ovf={count('due-ovf')} "
                  f"fragment_timer_ovf={count('fragment-timer-ovf')} fragment_cap={fragment_cap} ties={count('undecided-ties')} "
                  f"loop_s={duration[1]} wall_s={time.monotonic()-t:.6f} fragment_store_worlds={len(stored)} "
                  f"fragment_store_scale={pair_scale}", flush=True)
            if active or ties or stored:
                for line in log.splitlines():
                    if line.startswith(("TCP arrival ring", "TCP compared runs", "TCP heterogeneous", "TCP memory ")):
                        print(line, flush=True)
            if any(count(f) for f in ("tx-cap", "ooo-ovf", "rtt-ovf", "ring-ovf")):
                sys.stderr.write(log + result.stderr)
                return False
            if not active and not ties and not fragment and not stored:
                total_flows += int(complete[1])
                sys.stdout.write(log.replace(duration[0] + "\n", ""))
                sys.stderr.write(result.stderr)
                return complete[1] == complete[2]
            if ((active and expanded) or (ties and level + 1 >= len(sizes) + bool(extended(ties)))
                    or (fragment and fragment_cap > (1 << 29)) or (stored and pair_scale >= 65536)):
                sys.stderr.write(log + result.stderr)
                return False
            # Worlds with no retry keep their first-pass outputs. A separate
            # stalled world must still make the batch fail.
            for world in set(ids) - active - ties - fragment - stored:
                path = dict(item.split("=", 1) for item in lines[world].split(","))["out"]
                with open(path, newline="") as stream:
                    rows = list(csv.DictReader(stream))
                    if any(int(row["completed_ns"]) < 0 for row in rows):
                        sys.stderr.write(f"world {world} has incomplete flows\n")
                        return False
                    total_flows += len(rows)
            groups = {}
            for world in sorted(active | ties | fragment | stored):
                key = (level + int(world in ties), expanded or world in active,
                       fragment_cap * (2 if world in fragment else 1),
                       min(65536, pair_scale * (2 if world in stored else 1)))
                groups.setdefault(key, []).append(world)
            for (next_level, next_expanded, next_fragments, next_pairs), group in groups.items():
                if not attempt(group, next_level, next_expanded, next_fragments, tail_cap, next_pairs):
                    return False
            return True
        ok = attempt(list(range(len(lines))) if lines is not None else [0], level, False)
    if ok and lines is not None:
        print(f"flows {total_flows} completed {total_flows} across {len(lines)} worlds")
    print(f"Simulation Duration: {sim:.6f}s\nTotal TCP wall: {time.monotonic()-start:.6f}s")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run(Path(sys.argv[1]), sys.argv[2:]))
