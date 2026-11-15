#!/usr/bin/env python3
"""Actual CPU input paths, captured after the campaign starts; never backdate provenance."""
import os,shutil,subprocess,time
from pathlib import Path
import campaign as c
out=c.EXPORT/'provenance-supplement';out.mkdir(exist_ok=True)
paths={}
for j in c.JOBS:
 n=j['case'];g=j['gpus']
 for engine,ps in [('simai',[c.SD/'astra-sim-alibabacloud/inputs/config/SimAI.conf',c.AIROOT/f'wl/paper/{n.replace("_hpn","_")}.txt',c.AIROOT/f'topo/AlibabaHPN_{g}g_8gps_DualToR_SinglePlane_200Gbps_H100']),('twins',[c.ROOT/'validation/simai/SimAI.conf',c.ROOT/f'data/simai/hpn{g}.topo',c.ROOT/f'out/simai/wl/paper/{n.replace("_hpn","_")}.txt'])]:
  for p in ps:
   key=str(p)
   if key not in paths:
    st=p.stat();dest=out/'inputs'/(c.sha(p)+'-'+p.name);dest.parent.mkdir(exist_ok=True);shutil.copy2(p,dest)
    paths[key]=dict(sha256=c.sha(p),size=st.st_size,mtime_ns=st.st_mtime_ns,ctime_ns=st.st_ctime_ns,local_export_copy=str(dest.relative_to(c.EXPORT)))
binaries=[c.SD/c.SIMBIN.removeprefix('/simai/'),c.BIN/'twins-ai']
libroot=binaries[0].parent.parent/'lib'
if libroot.exists():binaries+=sorted(p for p in libroot.glob('*.so*') if p.is_file())
binary_records={str(p):dict(sha256=c.sha(p),size=p.stat().st_size,mtime_ns=p.stat().st_mtime_ns) for p in binaries}
c.save(out/'manifest.json',dict(captured_utc_ns=time.time_ns(),inputs=paths,binaries=binary_records,image=c.cmdout(['docker','image','inspect','localhost/simai-build','--format','{{.Id}} {{.Created}}']),scope='Retrospective actual CPU paths from recorded commands. Earlier started.json files hashed TwiNS config even for CPU; this supplement corrects that path error. mtimes and current hashes do not prove absence of historical mutation. New attempts hash actual inputs before timing.'))
