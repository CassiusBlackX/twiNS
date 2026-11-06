#!/usr/bin/env python3
"""Normalize traces for comparison.
  norm.py ref RUNLOG_DIR OUT_PREFIX   ns-3-ub runlog -> OUT.port, OUT.task
  norm.py twins PREFIX OUT_PREFIX     twins-ub(seq) output -> same form
Port lines are grouped by (node, port), keeping each port's own order (PortTrace files are per
port, in event order); task lines are sorted by (task, event)."""
import sys, os, re, glob

def write(out, ports, tasks):
    with open(out + '.port', 'w') as f:
        for key in sorted(ports):
            for t, ev, sz in ports[key]:
                f.write(f"{key[0]} {key[1]} {t} {ev} {sz}\n")
    with open(out + '.task', 'w') as f:
        for k in sorted(tasks):
            f.write(f"{k[0]} {k[1]} {tasks[k]}\n")

def ref(d, out):
    ports, tasks = {}, {}
    for fn in glob.glob(os.path.join(d, 'PortTrace_node_*_port_*.csv')):
        m = re.search(r'node_(\d+)_port_(\d+)', fn)
        key = (int(m.group(1)), int(m.group(2)))
        rows = []
        with open(fn) as f:
            next(f)
            for line in f:
                t, ev, port, sz = line.strip().split(',')
                rows.append((int(t), 'T' if ev == 'Port Tx' else 'R', int(sz)))
        ports[key] = rows
    for fn in glob.glob(os.path.join(d, 'TaskTrace_node_*.csv')):
        node = int(re.search(r'node_(\d+)', fn).group(1))
        with open(fn) as f:
            next(f)
            for line in f:
                v = line.strip().split(',')
                if v[1] == 'WQE Starts':
                    tasks[(int(v[2]), 'S')] = f"{node} {v[0]}"
                elif v[1] == 'WQE Completes':
                    tasks[(int(v[2]), 'C')] = f"{node} {v[0]}"
    write(out, ports, tasks)

def twins(p, out):
    ports, tasks = {}, {}
    with open(p + '.port') as f:
        for line in f:
            t, n, port, ev, sz = line.split()[:5]
            ports.setdefault((int(n), int(port)), []).append((int(t), ev, int(sz)))
    with open(p + '.task') as f:
        for line in f:
            t, n, task, ev = line.split()
            tasks[(int(task), ev)] = f"{n} {t}"
    write(out, ports, tasks)

if __name__ == '__main__':
    {'ref': ref, 'twins': twins}[sys.argv[1]](sys.argv[2], sys.argv[3])
