"""Local, restartable successor. Only read prior status until its complete/lock gate opens."""
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tarfile
import tempfile
import time

root=Path(__file__).resolve().parents[2]
campaign=root/'out/campaign'
evidence=campaign/'s50'
evidence.mkdir(exist_ok=True)
prior='/home/cassius/Videos/twins-isolated-20261118/out'
work='/home/cassius/Videos/twins-paper-scale-20261119'
base='/home/cassius/Videos/twins-extensions-20261119'
ssh=['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10','-o','ServerAliveInterval=20','s50']
def status(message):
    with (evidence/'queue.jsonl').open('a') as f:
        f.write(json.dumps(dict(utc_ns=time.time_ns(),status=message))+'\n')
def connection(cmd,**opts):
    return subprocess.run([*ssh,cmd],check=True,**opts)
def remote(cmd,**opts):
    return connection('cd '+work+' && '+cmd,**opts)
def py(script):
    return 'python3 -c '+shlex.quote(script)
def ready():
    # Read existing terminal receipts without copying anything onto s50.
    script=(Path(__file__).with_name('s50_worker.py')).read_text().split('def main():')[0]
    script+=f"\nprint(int(previous_finished(Path({prior!r})/'export')))"
    return connection(py(script),capture_output=True,text=True).stdout.strip()=='1'
def sync(source,dst,*args):
    subprocess.run(['rsync','-aL','--exclude=__pycache__',*args,str(source),'s50:'+work+'/'+dst],check=True)
def prepare():
    # The old runner writes its final marker just before releasing its flock.
    # rsync creates the leaf destination, but requires its parent to exist.
    connection('flock '+prior+'/exclusive-campaign.lock mkdir -p '+work+'/validation')
    for p,dst in [(str(campaign/'dcqcn/inputs')+'/','inputs/'),(str(root/'src')+'/','src/'),(root/'CMakeLists.txt','CMakeLists.txt'),
                  (str(root/'experiments/paper_scale')+'/','scripts/'),(str(root/'validation/extensions')+'/','validation/extensions/'),
                  (str(campaign/'a87-control/live')+'/','live/'),(str(campaign/'diagnostic-runtime')+'/','diagnostic-runtime/'),
                  (campaign/'reference-hashes.json','reference-hashes.json')]:sync(p,dst)
    sync(str(campaign/'ai-cap')+'/','ai-inputs/','--include=*/','--include=simai.topo','--include=fabric.txt','--include=config','--include=workload.txt','--include=ref.flows','--exclude=*')
    remote(py("import hashlib,json,pathlib;d=pathlib.Path('inputs');assert all(hashlib.file_digest((d/p).open('rb'),'sha256').hexdigest()==h for p,h in json.loads((d/'manifest.json').read_text())['sha256'].items())"))
def collect(job):
    dst=evidence/job
    dst.mkdir(exist_ok=True)
    rc=int(remote('cat current/exit-status',capture_output=True,text=True).stdout)
    remote('tar czf case.tgz -C current .')
    digest=remote('sha256sum case.tgz',capture_output=True,text=True).stdout.split()[0]
    with tempfile.NamedTemporaryFile(dir=dst) as tmp:
        remote('cat case.tgz',stdout=tmp);tmp.flush();tmp.seek(0)
        if hashlib.file_digest(tmp,'sha256').hexdigest()!=digest:raise ValueError('return digest mismatch')
        tmp.seek(0)
        with tarfile.open(fileobj=tmp,mode='r:gz') as arc:arc.extractall(dst,filter='data')
        if (dst/'exit-status').read_text().strip()!=str(rc):raise ValueError('missing local terminal status')
    receipt=dst/'received.tmp';receipt.write_text(json.dumps(dict(sha256=digest,rc=rc,utc_ns=time.time_ns())))
    receipt.replace(dst/'received.json')
    cleanup(job)
    return rc
def cleanup(job):
    remote(py("import json,pathlib,shutil;d=pathlib.Path('current');assert json.loads((d/'request.json').read_text())['job']=="+repr(job)+";shutil.rmtree(d);pathlib.Path('case.tgz').unlink(missing_ok=True)"))
def current_job():
    return remote("if test -e current/request.json; then cat current/request.json; fi",capture_output=True,text=True).stdout.strip()
def drain(job):
    # Locking in the detached worker makes reconnect/relaunch idempotent.
    while True:
        done=remote("if test -f current/exit-status; then echo done; else nohup python3 scripts/s50_worker.py </dev/null >/dev/null 2>&1 & fi",capture_output=True,text=True).stdout.strip()
        if done=='done':return collect(job)
        time.sleep(30)
def execute(job,cmd):
    for attempt in range(1,4):
        name=job if attempt==1 else f'{job}-attempt{attempt}'
        saved=evidence/name/'received.json'
        if saved.exists():
            rc=json.loads(saved.read_text())['rc']
        else:
            payload=json.dumps(dict(job=name,command=list(map(str,cmd)),old=prior))
            remote(py("import pathlib;p=pathlib.Path('current');p.mkdir(exist_ok=True);s=p/'request.tmp';s.write_text("+repr(payload)+");s.replace(p/'request.json')"))
            status('running '+name)
            rc=drain(name)
        if rc not in (125,126):return rc
    raise RuntimeError('three interrupted attempts '+job)
def batch():
    prepare()
    prev=current_job()
    if prev:
        name=json.loads(prev)['job']
        if (evidence/name/'received.json').exists():cleanup(name)
        else:drain(name)
    if execute('cuda-build',['python3','scripts/s50_build.py']):raise RuntimeError('CUDA build failed; see local receipt')
    for mode in ('heterogeneous','uniform','spread','incast'):
        for h in (128,256,512,1024):
            args=['env','PYTHONPATH='+work+'/validation/extensions','python3','scripts/scale.py','inputs','current/trials','release',base+'/tcp-oracle/reference',base+'/irn-oracle','--irn-reference',work+'/live/run','--hosts',str(h),'--mode',mode,'--core','0']
            if execute(f'{mode}-{h}',args):
                status(f'limited formal point {mode}-{h}')
    execute('tcp-diagnostics',['python3','scripts/tcp_diag.py','inputs','current/tcp','release','profile','reference-hashes.json'])
    for m,h in [('incast',256),('spread',1024),('incast',1024)]:
        for side,exe in [('gpu',work+'/profile/twins-rdma'),('cpu',work+'/diagnostic-runtime/run')]:
            args=['env','LD_LIBRARY_PATH='+base+'/irn-oracle/build','taskset','-c','0','python3','scripts/irn_diag.py','.',exe,side,'--out','current/trace','--hosts',str(h),'--mode',m]
            execute(f'wire-{m}-{h}-{side}',args)
    execute('ecn-original',['python3','scripts/ecn_gpu.py','inputs','current/ecn','release/twins','reference-hashes.json'])
    cmd='cp -r ai-inputs current/ai && python3 scripts/ai_stages.py current/ai gpu --exe '+base+'/build/twins-ai'
    execute('live-ai',['bash','-ec',cmd])
    from s50_finalize import finish
    finish(campaign)
    status('finished returning formal trials and diagnostics')
    # collect() already verifies each transfer against the remote archive's SHA-256.
    # Keep that local evidence as the canonical copy and release the CUDA successor.
    terminal = evidence/'finished.tmp'
    terminal.write_text(json.dumps(dict(
        utc_ns=time.time_ns(),
        source_revision=subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip(),
        comparisons='comparisons.json',
        receipts={p.parent.name:json.loads(p.read_text())['rc'] for p in sorted(evidence.glob('*/received.json'))},
    ),indent=2)+'\n')
    terminal.replace(evidence/'finished.json')
if __name__=='__main__':
    while not (evidence/'finished.json').exists():
        try:
            if ready():batch();break
            status('waiting for previous full campaign')
        except (OSError,subprocess.SubprocessError,ValueError,RuntimeError) as exc:status(repr(exc))
        time.sleep(300)
