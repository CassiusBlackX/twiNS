"""Monitor live CUDA process ancestry while the caller holds the benchmark lock."""
import json
from pathlib import Path
import subprocess
import sys
import time


def gpu_pids():
    info = subprocess.check_output(
        ['nvidia-smi','--query-compute-apps=pid','--format=csv,noheader,nounits'],
        text=True, timeout=5,
    )
    return {int(row) for row in info.splitlines() if row.strip()}


def parent_of(pid):
    status=Path(f'/proc/{pid}/stat').read_text()
    return int(status.rsplit(')',1)[1].split()[1])


def ownership(pid, ancestor):
    try:
        while pid>1:
            if pid==ancestor:
                return 'own'
            pid=parent_of(pid)
        return 'foreign'
    except (OSError, ValueError, IndexError):
        return 'exited'


def main():
    out=Path(sys.argv[1])
    out.mkdir(parents=True,exist_ok=True)
    deadline=time.monotonic()+900
    while gpu_pids():
        if time.monotonic()>=deadline:
            raise SystemExit('GPU still occupied after 15 minutes')
        time.sleep(5)
    foreign, vanished, errors = [], set(), []
    with subprocess.Popen(sys.argv[2:]) as service:
        code=None
        while code is None:
            try:
                code=service.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    for pid in gpu_pids():
                        label=ownership(pid,service.pid)
                        if label=='foreign':
                            foreign.append({'pid':pid,'utc_ns':time.time_ns()})
                        elif label=='exited':
                            vanished.add(pid)
                except (subprocess.SubprocessError,ValueError) as error:
                    errors.append(str(error))
    (out/'gpu-ownership.json').write_text(json.dumps({
        'eligible': not foreign and not errors, 'foreign':foreign,
        'exited_before_attribution':sorted(vanished),'errors':errors,'period_s':3,
    },indent=2)+'\n')
    raise SystemExit(code)


if __name__=='__main__':
    main()
