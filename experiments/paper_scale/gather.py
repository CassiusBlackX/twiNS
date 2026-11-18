"""Pull only closed a87 cases and verify local content before issuing receipts."""
from pathlib import Path
import hashlib,json,subprocess,sys,time,tempfile
root=Path(__file__).resolve().parents[2];d=root/'out/campaign';dst=d/'a87';dst.mkdir(exist_ok=True)
remote='/home/cassius/netsim/twins-paper-scale-20261119'
index="""from pathlib import Path
import hashlib,json
cases=[]
for pat in ('receipts/*/receipt.json','cpu-scale/*/*/receipt.json','cpu-scale/*/*/failure.json','trace/*/receipt.json','ai*/**/reference.rc'):
 for marker in Path('.').glob(pat):
  paths=[p for p in marker.parent.iterdir() if p.is_file() and not p.name.startswith('.')]
  cases.append({str(p):hashlib.file_digest(p.open('rb'),'sha256').hexdigest() for p in paths})
print(json.dumps(cases))"""
sys.path[:0]=[str(root/'validation/extensions'),str(Path(__file__).resolve().parent)]
from scale import verify
def collect():
    cmd=['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10','a87','cd '+remote+' && python3 -']
    cases=json.loads(subprocess.check_output(cmd,input=index,text=True,timeout=120))
    hashes={k:v for case in cases for k,v in case.items()}
    seen=dst/'received-hashes.json'
    old=json.loads(seen.read_text()) if seen.exists() else {}
    need=[p for p,h in hashes.items() if old.get(p)!=h or not (dst/p).exists()]
    if need:
        with tempfile.NamedTemporaryFile(mode='w') as lst:
            lst.write('\n'.join(need)+'\n');lst.flush()
            subprocess.run(['rsync','-a','--files-from='+lst.name,'a87:'+remote+'/',str(dst)+'/'],check=True)
        for p in need:
            with (dst/p).open('rb') as f:assert hashlib.file_digest(f,'sha256').hexdigest()==hashes[p]
        tmp=seen.with_suffix('.tmp');tmp.write_text(json.dumps({**old,**hashes}));tmp.replace(seen)
    summary={}
    for rec in (dst/'cpu-scale').glob('*/*/receipt.json'):
        cpu=rec.parent;h=cpu.name;m=cpu.parent.name;key=m+'/'+h
        tcp=m in ('heterogeneous','uniform')
        gpu=(d/'local'/m/h/'trial-0') if tcp else d/'gpu-only'/m/h
        candidate=d/'wide-fixed'/m/h
        if (candidate/'gpu.json').exists():gpu=candidate
        if not (gpu/'gpu.fct').exists():continue
        case=dst/'checks'/m/h;case.mkdir(parents=True,exist_ok=True)
        for n,p in [('gpu.fct',gpu/'gpu.fct'),('gpu.log',gpu/'gpu.log'),('reference.fct',cpu/'reference.fct')]:
            if (case/n).is_symlink() and (case/n).resolve()!=p.resolve():(case/n).unlink()
            if not (case/n).exists():(case/n).symlink_to(p)
        try:
            rc=json.loads((gpu/'gpu.json').read_text())['rc'];assert rc==0,f'GPU exit {rc}'
            count=verify(case,d/'inputs'/h/('tcp' if tcp else m)/'flows.csv',tcp)
            summary[key]=dict(accepted=True,flows=count)
        except (ValueError,IndexError,AssertionError,FileNotFoundError) as e:summary[key]=dict(accepted=False,reason=str(e))
    total=len(list((dst/'receipts').glob('*/receipt.json')))
    (dst/'progress.json').write_text(json.dumps(dict(ecn=total,scale=summary),indent=2))
    if total==480:
        subprocess.run(['rsync','-a','a87:'+remote+'/receipts/result.json',str(dst/'receipts')+'/'],check=True)
    return (total==480 and len(list((dst/'cpu-scale').glob('*/*/receipt.json')))>=16
            and len(list((dst/'trace').glob('irn-*/receipt.json')))==3
            and all((dst/'ai-v5'/case/'reference.rc').exists() for case in ('clean','loss','large-buffer','small-buffer')))
if __name__=='__main__':
    while True:
        try:
            if collect():break
        except Exception as exc:print(time.ctime(),repr(exc),flush=True)
        time.sleep(90)
