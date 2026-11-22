"""Repeat the frozen SimAI latency and eight-job protocols on the current candidate."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

VIDEOS=Path.home()/'Videos'
BASE=VIDEOS/'twins-paper-evidence-20261116'
CANDIDATE=VIDEOS/'twins-paper-followup-20261123/candidate'
LOCK=VIDEOS/'twins-isolated-20261118/out/exclusive-campaign.lock'

def measure(output):
    sys.path.insert(0,str(BASE/'experiments/paper_evidence'))
    import campaign as c
    c.ROOT=BASE
    c.OUT=output
    c.BIN=CANDIDATE/'build'
    c.EXPORT=output/'receipts'
    c.RAW=output.parent/'raw'
    c.CACHE=output.parent/'validation-cache'
    for path in (c.EXPORT,c.RAW,c.CACHE):
        path.mkdir(parents=True,exist_ok=True)
    import batches as b
    policy_path=BASE/'out/paper-evidence-20261116/export/batches-v2/memory-policy.json'
    policy=json.loads(policy_path.read_text())
    names=('gpt3_13b_full_hpn128','llama_65b_full_hpn128','gpt3_175b_full_hpn128',
           'gpt3_175b_probe_hpn1024')
    host_condition=dict(gpu_isolation='project lock, clean foreign GPU admission and ongoing process attribution',
                           cpu_parallelism='P0 core 0, P16 0-16; no intentional concurrent measured CPU job',
                           revision=(CANDIDATE/'revision').read_text().strip())
    cfg=BASE/'validation/simai/SimAI.conf'
    candidate_cfg=CANDIDATE/'validation/simai/SimAI.conf'
    if cfg.read_bytes()!=candidate_cfg.read_bytes():
        raise ValueError('configuration differs across candidate and reference measurement')
    c.save(output/'protocol.json',dict(host_condition=host_condition,config_sha256=c.sha(cfg),
                                      candidate_binary_sha256=c.sha(c.BIN/'twins-ai'),cases=names,
                                      repeats=3,batch_policy=policy,source_matrix=c.M))
    receipts=[]
    for repeat in range(1,4):
        workloads = names[repeat-1:]+names[:repeat-1]
        for n in workloads:
            for engine in ('p0','p16') if repeat%2 else ('p16','p0'):
                label=f'candidate-r{repeat}-{n}-{engine}'
                row=c.attempt(label,c.BY[n],engine,resource_context=host_condition)
                if row.get('accepted'):
                    path=c.RAW/label
                    # The receipt preserves both comparison hashes on every run.
                    # Keep one full matching product per workload and P setting.
                    for output_name, suffix in (('messages','.fct'),('actions.dag','.dag')) if repeat==1 else ():
                        source=path/output_name
                        if source.is_file():
                            target=c.EXPORT/'attempts'/label/('verified'+suffix)
                            shutil.copy2(source,target)
                    if path.is_dir():
                        # Only this run's raw, regenerable stub files remain.
                        shutil.rmtree(path)
                receipts.append(dict(label=label,accepted=row.get('accepted'),wall_ns=row.get('wall_ns')))
    batch=[]
    for repeat in range(1,4):
        for engine in ('p0','p16') if repeat%2 else ('p16','p0'):
            label=f'candidate-eight-r{repeat}-{engine}'
            b.batch(label,engine,1,policy,resource_context=host_condition)
            result=json.loads((b.BROOT/(label+'.json')).read_text())
            batch.append(result)
    c.save(output/'summary.json',dict(latency=receipts,batches=batch))

def main():
    output=Path(sys.argv[1]).resolve()
    output.mkdir(parents=True,exist_ok=False)
    if len(sys.argv)>2:
        measure(output)
        return 0
    # The cross-rack comparison has priority on the same GPU.
    other=VIDEOS/'twins-paper-strict-20261123/crosspod'
    deadline=time.monotonic()+5400
    while time.monotonic()<deadline:
        epochs=sorted(other.glob('20*'))
        if epochs and (epochs[-1]/'results/measurements/summary.json').exists():
            break
        time.sleep(20)
    observe=VIDEOS/'twins-paper-followthrough-20261122/project/experiments/paper_review/observe_cuda.py'
    return subprocess.run(['flock',str(LOCK),sys.executable,str(observe),str(output/'resources'),
                           sys.executable,__file__,str(output/'measurements'),'measure']).returncode

if __name__=='__main__':
    raise SystemExit(main())
