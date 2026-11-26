"""Release this campaign's old whole-run lock after its next committed sample."""
import json
import os
from pathlib import Path
import signal
import sys
import time

pid=int(sys.argv[1])
root=Path.cwd()
proc=Path("/proc")/str(pid)
if "figure_tcp.py\x00cpu\x00" not in (proc/"cmdline").read_text() or (proc/"cwd").resolve()!=root:
    raise SystemExit("refusing to interrupt a different process")
summary=root/"figure-cpu/summary.json"

def count():
    try:
        return len(json.loads(summary.read_text()))
    except json.JSONDecodeError:
        return -1

before=count()
while proc.exists() and count()<=before:
    time.sleep(.15)
if not proc.exists():
    raise SystemExit("campaign already exited")
os.kill(pid,signal.SIGSTOP)
try:
    children=list(map(int,(proc/"task"/str(pid)/"children").read_text().split()))
    for child in children:
        q=Path("/proc")/str(child)
        try:
            cmd=(q/"cmdline").read_bytes()
            if b"twins-full-review-20261126/tcp-native/reference\0" in cmd:
                os.kill(child,signal.SIGTERM)
        except FileNotFoundError:
            pass
    assert count()>before
    print(f"paused after {count()} committed samples; retained partial files",flush=True)
    os.kill(pid,signal.SIGTERM)
finally:
    os.kill(pid,signal.SIGCONT)
