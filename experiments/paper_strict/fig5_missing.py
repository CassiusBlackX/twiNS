"""Fill DCTCP/NewReno repeat series while retaining existing CUBIC receipts."""
import csv
import hashlib
import json
from pathlib import Path
import re
from statistics import median
import subprocess
import sys
import time

VIDEOS=Path.home()/'Videos'
ISO=VIDEOS/'twins-isolated-20261118'
LOCK=ISO/'out/exclusive-campaign.lock'

def run(out):
    frozen=ISO/'out'
    binary=ISO/'tcp-reference/ns3-unison-ref'
    manifest=json.loads((frozen/'manifest.json').read_text())
    groups={}
    for job in manifest['cubic']:
        name=job['case']
        for protocol,ns3 in [('dctcp','TcpDctcp'),('newreno','TcpNewReno')]:
            key=f'{name}-{protocol}'
            settings=[]
            sequential={}
            for threads in (0,1,2,4,8,16,32):
                runs=[]
                for trial in range(3):
                    dest=out/key/f't{threads}'/f'trial-{trial}'
                    dest.mkdir(parents=True)
                    final=dest/'output.csv'
                    command=['taskset','-c','0' if threads<=1 else f'0-{threads-1}',
                        str(binary),f'--k={job["k"]}',f'--flows={frozen}/inputs/{job["flows"]}',
                        f'--tcp=ns3::{ns3}',f'--stop={job["stop_s"]}','--hostq=red','--flowEcmp=1',
                        f'--queue={job["queue"]}',f'--ecnK={job["ecnK"]}',f'--threads={threads}',f'--out={final}']
                    start=time.perf_counter_ns()
                    with (dest/'process.log').open('wb') as log:
                        try:
                            rc=subprocess.run(command,cwd=dest,stdout=log,stderr=subprocess.STDOUT,timeout=900).returncode
                        except subprocess.TimeoutExpired:
                            rc=124
                    elapsed=time.perf_counter_ns()-start
                    loop=re.findall(r'event_loop_ns (\d+)',(dest/'process.log').read_text(errors='replace'))
                    current=[]
                    if final.exists():
                        with final.open() as f:
                            current=[tuple(map(int,x[:6])) for x in list(csv.reader(f))[1:]]
                    if threads==0:
                        sequential[trial]=current
                    row=dict(rc=rc,threads=threads,trial=trial,process_ns=elapsed,
                        event_loop_ns=int(loop[-1]) if loop else None,command=command,
                        exact=current==sequential[trial],matched_final_fct=sum(a==b for a,b in zip(current,sequential[trial])),
                        complete_flows=sum(x[5]>=0 for x in current),total_rows=len(current),
                        sha256=hashlib.sha256(final.read_bytes()).hexdigest() if final.exists() else None)
                    (dest/'receipt.json').write_text(json.dumps(row,indent=2)+'\n')
                    runs.append(row)
                    print(key,threads,trial,rc,row['event_loop_ns'],flush=True)
                valid=[r for r in runs if r['rc']==0 and r['event_loop_ns'] is not None and r['complete_flows']==r['total_rows']>0]
                loops=[r['event_loop_ns'] for r in valid]
                all_wall=[r['process_ns'] for r in valid]
                settings.append(dict(threads=threads,valid=len(valid),runs=runs,
                    event_median_ns=median(loops) if loops else None,event_range_ns=[min(loops),max(loops)] if loops else None,
                    wall_median_ns=median(all_wall) if all_wall else None,wall_range_ns=[min(all_wall),max(all_wall)] if all_wall else None))
                (out/key/'summary.json').write_text(json.dumps(settings,indent=2)+'\n')
            valid_threaded=[x for x in settings if x['threads']>0 and x['valid']==3]
            groups[key]=dict(arms=settings,fastest=min(valid_threaded,key=lambda x:x['event_median_ns'])['threads'] if valid_threaded else None)
            (out/'summary.json').write_text(json.dumps(groups,indent=2)+'\n')

def main():
    dst=Path(sys.argv[1]).resolve()
    dst.mkdir(parents=True)
    if len(sys.argv)>2:
        run(dst)
        return 0
    # Cross-pod GPU and candidate AI take the measurement lock before the all-core CPU trials.
    root=VIDEOS/'twins-paper-strict-20261123'
    deadline=time.monotonic()+16000
    while time.monotonic()<deadline:
        epochs=sorted((root/'ai_candidate').glob('20*'))
        if epochs and (epochs[-1]/'results/measurements/summary.json').is_file():
            break
        time.sleep(30)
    return subprocess.run(['flock',str(LOCK),sys.executable,__file__,str(dst/'measurements'),'run']).returncode

if __name__=='__main__':
    raise SystemExit(main())
