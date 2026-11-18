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
  # A changing PID set must not turn the benchmark's own CPU work into contention.
  def fixtures(host_cores):
   return [dict(utc_ns=i*10**9,gpu='0,0,0,0,0',gpu_processes='',gpu_pmon='',cpu_stat=f'cpu0 {100*i} 0 0 0 0 0 0 0\n',own_processes=[dict(pid=100+i,uticks=0,sticks=0)],host_cpu_ticks=[100*host_cores*i,0,0,0,0,0,0,0],owned_cpu_usage_usec=i*10**6,cpu_clock_ticks_per_second=100) for i in range(4)]
  clean=audit(fixtures(1),'simai');assert not clean['suspected'] and clean['legacy_pid_audit']['suspected']
  busy=audit(fixtures(3),'simai');assert busy['suspected'] and busy['foreign_cpu_core_samples']==[2.,2.,2.]
  retired=dict(gpu_pmon='0 2147000000 C 99 0 - - - - retired_owned',known_owned_pids=[2147000000]);assert not foreign_gpu(retired)
  oldjobs=c.JOBS;oldroot=b.BROOT;c.JOBS=oldjobs[:1];b.BROOT=d/'batch';b.BROOT.mkdir(exist_ok=True);event=threading.Event();event.set();cancelled=False;disk_blocked=False
  try:
   b.batch('iso18-selfcheck-prelaunch-cancel','simai',1,read(BASE/'out/paper-evidence-20261116/export/batches-v2/memory-policy.json'),cancel_event=event)
  except InterruptedError:cancelled=True
  except RuntimeError as e:
   if 'less than 10GiB free' not in str(e):raise
   cancelled=True;disk_blocked=True
  finally:c.JOBS=oldjobs;b.BROOT=oldroot
  assert cancelled;states=[read(p) for p in (d/'batch').glob('*trial*/state.json')];assert states and all(x['status']=='interrupted' and not x['active'] and not x['completed'] for x in states)
  result=dict(utc_ns=time.time_ns(),passed=True,owned_process_received_sigterm=rc,unrelated_control_process_remained_alive=True,foreign_telemetry_pid_never_signalled=True,partial_batch_preserved_and_not_timed=not disk_blocked,batch_cancel_case_replaced_by_real_disk_admission_refusal=disk_blocked,cumulative_cpu_not_confused_by_pid_turnover=True,real_foreign_cpu_positive_control=True,retired_owned_gpu_pid_not_foreign=True,scope='selfcheck of interruption/safety/accounting behavior only; no scientific simulator samples')
  save(E/'receipts/guard-selfcheck.json',result);print(json.dumps(result))
 finally:
  c.snapshot=old
  for p in (target,unrelated):
   if p.poll() is None:p.terminate();p.wait()
if __name__=='__main__':main()
