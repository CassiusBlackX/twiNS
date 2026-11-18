import sys,json,subprocess as sp,os,signal,threading,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from common import *
import parallel75 as dispatcher
small=dict(engine='simai',ncores=1,memory=7)
a=dispatcher.allocate(small,[]);b_=dispatcher.allocate(small,[a]);assert a['cores']!=b_['cores']
assert dispatcher.allocate(dict(engine='p16',ncores=16,memory=10),[a]) is None
assert dispatcher.allocate(dict(engine='simai',ncores=1,memory=68),[a]) is None
assert dispatcher.allocate(dict(engine='p0',ncores=1,memory=10),[a])['cores']==[0]
assert dispatcher.allocate(dict(engine='p0',ncores=1,memory=10),[dict(cores=[0],memory=10,gpu=True)]) is None
# Real process interruption / non-interruption, fixture GPU telemetry only.
old=c.snapshot
class Stop:
 def __init__(self):self.n=0
 def is_set(self):return self.n>=3
 def wait(self,n):self.n+=1;time.sleep(.01)
def fixture():return dict(utc_ns=time.time_ns(),gpu='100,1,0,0,0',gpu_processes='',gpu_pmon='0 2147000000 C 99 0 - - - - fixture')
c.snapshot=fixture
other=sp.Popen(['sleep','30'],start_new_session=True);cpu=sp.Popen(['sleep','30'],start_new_session=True);gpu=sp.Popen(['sleep','30'],start_new_session=True)
d=E/'receipts/parallel75-selfcheck';d.mkdir(exist_ok=True)
try:
 guarded_monitor(Stop(),d/'cpu.jsonl',cpu.pid,'simai',None,8);assert cpu.poll() is None
 guarded_monitor(Stop(),d/'gpu.jsonl',gpu.pid,'p0',None,0);assert gpu.wait(timeout=3)==-signal.SIGTERM;assert other.poll() is None
finally:
 c.snapshot=old
 for child in (other,cpu,gpu):
  if child.poll() is None:child.terminate();child.wait()
# Validate MTP footer shell without running an experimental model.
import ast
module=ast.parse(Path(__file__).with_name('ai.py').read_text())
shell=next(n.value for n in ast.walk(module) if isinstance(n,ast.Constant) and isinstance(n.value,str) and n.value.startswith('export AS_SEND_LAT=3; timeout 7200'))
assert sp.run(['bash','-n','-c',shell]).returncode==0
save(E/'receipts/parallel75-selfcheck.json',dict(utc_ns=time.time_ns(),passed=True,disjoint_physical_cores=True,total_memory_admission=True,p16_barrier=True,single_gpu_lane=True,foreign_gpu_does_not_abort_cpu=True,foreign_gpu_aborts_only_owned_gpu=True,unrelated_process_alive=True,mtp_footer_shell_syntax=True,scope='scheduler logic and controlled sleep processes; GPU activity is a telemetry fixture; no simulator timing samples'))
print('parallel75 control checks passed')
