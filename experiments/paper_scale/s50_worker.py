"""A detached job holds the old campaign's flock until its simulator has exited."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import threading
import time

def previous_finished(export):
    stages=('cubic','scans','ai-latency','mtp','throughput')
    try:
        terminal=json.loads((export/'all-finished.json').read_text())
        if terminal.get('policy')=='parallel75-20261119':
            last=json.loads((export/'parallel75/active.json').read_text())
            return (last.get('resource_epoch')==terminal['policy']
                    and terminal.get('utc_ns',0)>=last['utc_ns']
                    and terminal.get('status','').startswith('all predeclared attempts returned timing-eligible receipts'))
        return bool(terminal) and all(json.loads((export/(s+'-quality.json')).read_text()).get('complete') is True for s in stages)
    except (OSError,ValueError):
        return False

def gpu_pids():
    text=subprocess.check_output(['nvidia-smi','--query-compute-apps=pid','--format=csv,noheader,nounits'],text=True)
    return {int(p) for p in text.splitlines() if p.strip().isdigit()}

def descendant(pid,ancestor):
    while pid>1:
        if pid==ancestor:return True
        try:pid=int(Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()[1])
        except (OSError,IndexError,ValueError):return None
    return False

def watch(proc,stop,seen,exited):
    while not stop.wait(3):
        try:
            for pid in gpu_pids():
                owner = descendant(pid, proc.pid)
                if owner is False:
                    seen.add(pid)
                elif owner is None:
                    # A short owned CUDA process can leave NVML's sampled PID
                    # list after it has exited. Keep the observation separate
                    # from a live competing process with resolved ancestry.
                    exited.add(pid)
        except (OSError,subprocess.SubprocessError):seen.add('sampler_error')

def main():
    current=Path('current')
    with (current/'worker.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return
        if (current/'exit-status').exists():return
        job=json.loads((current/'request.json').read_text())
        out=Path(job['old'])
        with (out/'exclusive-campaign.lock').open('a') as shared:
            fcntl.flock(shared,fcntl.LOCK_EX)
            if not previous_finished(out/'export'):return
            (current/'started').write_text(str(time.time_ns()))
            # Preserve interrupted attempts. A restarted host cannot turn partial
            # trials into an accepted result.
            with (current/'controller.log').open('a') as log:
                try:
                    if (current/'invoked').exists():rc=125
                    else:
                        (current/'invoked').touch()
                        timed='scripts/scale.py' in job['command']
                        if timed:
                            while gpu_pids():time.sleep(15)
                        with subprocess.Popen(job['command'],stdout=log,stderr=subprocess.STDOUT,pass_fds=(shared.fileno(),lock.fileno())) as proc:
                            seen=set()
                            exited=set()
                            stop=threading.Event()
                            monitor=threading.Thread(target=watch,args=(proc,stop,seen,exited)) if timed else None
                            if monitor:monitor.start()
                            try:rc=proc.wait()
                            finally:
                                stop.set()
                                if monitor:monitor.join()
                            if timed:
                                (current/'isolation.json').write_text(json.dumps(dict(
                                    poll_seconds=3, foreign_gpu=list(seen),
                                    exited_before_attribution=sorted(exited), eligible=not seen,
                                )))
                                if seen:rc=125
                except OSError as exc:
                    print(exc,file=log);rc=126
            tmp=current/'exit.tmp';tmp.write_text(str(rc));tmp.replace(current/'exit-status')
if __name__=='__main__':main()
