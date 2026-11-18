"""Exercise the old terminal markers and flock with a temporary synthetic queue."""
from pathlib import Path
import fcntl,json,subprocess,sys,tempfile,time
from s50_worker import previous_finished

with tempfile.TemporaryDirectory() as tmp:
    d=Path(tmp)
    prior=d/'old';export=prior/'export';export.mkdir(parents=True)
    cur=d/'current';cur.mkdir()
    (cur/'request.json').write_text(json.dumps(dict(old=str(prior),command=['sh','-c','echo measured >> current/probe'])))
    worker=[sys.executable,str(Path(__file__).with_name('s50_worker.py').resolve())]
    subprocess.run(worker,cwd=d,check=True)
    assert not (cur/'started').exists()
    (export/'all-finished.json').write_text('{"finished":true}')
    for name in ('cubic','scans','ai-latency','mtp','throughput'):(export/(name+'-quality.json')).write_text('{"complete":true}')
    (export/'scans-quality.json').write_text('{"complete":false}')
    subprocess.run(worker,cwd=d,check=True)
    assert not (cur/'started').exists()
    (export/'scans-quality.json').write_text('{"complete":true}')
    with (prior/'exclusive-campaign.lock').open('a') as f:
        fcntl.flock(f,fcntl.LOCK_EX)
        child=subprocess.Popen(worker,cwd=d)
        time.sleep(.2)
        assert child.poll() is None and not (cur/'started').exists()
        fcntl.flock(f,fcntl.LOCK_UN)
        assert child.wait(timeout=10)==0
    subprocess.run(worker,cwd=d,check=True)
    assert (cur/'probe').read_text()=='measured\n' and (cur/'exit-status').read_text()=='0'
    print('PASS missing terminal marker, failed quality, held lock, released lock, idempotent reconnect')

    parallel=export/'parallel75';parallel.mkdir()
    (parallel/'active.json').write_text(json.dumps(dict(resource_epoch='parallel75-20261119',utc_ns=100)))
    marker=dict(policy='parallel75-20261119',utc_ns=99,status='all predeclared attempts returned timing-eligible receipts; output acceptance retained separately')
    (export/'all-finished.json').write_text(json.dumps(marker))
    assert not previous_finished(export)
    marker['utc_ns']=101
    (export/'all-finished.json').write_text(json.dumps(marker))
    (export/'mtp-quality.json').unlink()
    assert previous_finished(export)
    print('PASS active parallel75 policy, stale terminal rejected, final receipt accepted')
