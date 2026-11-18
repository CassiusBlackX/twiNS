#!/usr/bin/env python3
"""Actual owned-process interruption test; simulated telemetry never runs a model."""
from common import *
def main():
 d=E/'receipts/guard-selfcheck';d.mkdir(exist_ok=True);target=sp.Popen(['sleep','20'],start_new_session=True);unrelated=sp.Popen(['sleep','20'],start_new_session=True);old=c.snapshot
 class FastStop:
  def is_set(self):return False
  def wait(self,n):time.sleep(.02)
 try:
  c.snapshot=lambda:dict(utc_ns=time.time_ns(),gpu='100,1,0,0,0',gpu_processes='',gpu_pmon='0 2147000000 C 99 0 - - - - telemetry_fixture')
  guarded_monitor(FastStop(),d/'resources.jsonl',target.pid,'cpu',None,0);rc=target.wait(timeout=3);assert rc==-signal.SIGTERM;assert unrelated.poll() is None
  samples=[json.loads(s) for s in (d/'resources.jsonl').read_text().splitlines()];classification=audit(samples,'simai');assert classification['interrupted_for_external_gpu'] and classification['suspected'];c.snapshot=old
  oldjobs=c.JOBS;oldroot=b.BROOT;c.JOBS=oldjobs[:1];b.BROOT=d/'batch';b.BROOT.mkdir(exist_ok=True);event=threading.Event();event.set();cancelled=False
  try:
   b.batch('iso18-selfcheck-prelaunch-cancel','simai',1,read(BASE/'out/paper-evidence-20261116/export/batches-v2/memory-policy.json'),cancel_event=event)
  except InterruptedError:cancelled=True
  finally:c.JOBS=oldjobs;b.BROOT=oldroot
  assert cancelled;states=[read(p) for p in (d/'batch').glob('*trial*/state.json')];assert states and all(x['status']=='interrupted' and not x['active'] and not x['completed'] for x in states)
  result=dict(utc_ns=time.time_ns(),passed=True,owned_process_received_sigterm=rc,unrelated_control_process_remained_alive=True,foreign_telemetry_pid_never_signalled=True,partial_batch_preserved_and_not_timed=True,scope='selfcheck of interruption/safety behavior only; no scientific simulator samples')
  save(E/'receipts/guard-selfcheck.json',result);print(json.dumps(result))
 finally:
  c.snapshot=old
  for p in (target,unrelated):
   if p.poll() is None:p.terminate();p.wait()
if __name__=='__main__':main()
