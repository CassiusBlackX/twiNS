"""Compress on a87, validate, and publish an immutable archive on the local computer."""
import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

def publish(name,stage,local,publication):
    # stage is the dedicated bundle directory under the authorized a87 workspace.
    remote='/home/cassius/netsim/twins-paper-scale-20261119'
    if name not in {'twins-paper-scale-rx-a87-20261119','twins-paper-scale-s50-20261119'}:raise ValueError(name)
    if stage not in {'local-bundle','s50-bundle'}:raise ValueError(stage)
    command=f'cd {remote} && python3 archive.py --seal {stage} {name}'
    subprocess.run(['ssh','a87',command],check=True)
    expected=subprocess.check_output(['ssh','a87','cat '+remote+'/'+name+'.tar.zst.sha256'],text=True).split()[0]
    local.mkdir(parents=True,exist_ok=True)
    path=local/(name+'.tar.zst')
    if not path.exists():
        partial=path.with_suffix('.partial')
        subprocess.run(['rsync','-a','a87:'+remote+'/'+path.name,partial],check=True)
        with partial.open('rb') as f:
            if hashlib.file_digest(f,'sha256').hexdigest()!=expected:raise ValueError('archive digest mismatch')
        partial.replace(path)
    with path.open('rb') as f:
        if hashlib.file_digest(f,'sha256').hexdigest()!=expected:raise ValueError('local archive collision')
    manifest=path.with_suffix('.zst.sha256');manifest.write_text(expected+'  '+path.name+'\n')
    for f in (path,manifest):
        dst=publication/f.name
        if dst.exists():
            with dst.open('rb') as stream, f.open('rb') as src:
                if hashlib.file_digest(stream,'sha256').digest()!=hashlib.file_digest(src,'sha256').digest():raise FileExistsError(dst)
        else:
            with tempfile.NamedTemporaryFile(dir=publication,delete=False) as pending:
                with f.open('rb') as src:shutil.copyfileobj(src,pending)
                pending.flush();os.fsync(pending.fileno())
            Path(pending.name).replace(dst)
    return path

def seal(stage,name):
    target=Path(name+'.tar.zst')
    if target.exists():return
    source=Path(stage)
    inventory=source/'SHA256SUMS'
    with inventory.open('w') as text:
        for p in sorted(source.rglob('*')):
            if p!=inventory and p.is_file():
                with p.open('rb') as f:text.write(hashlib.file_digest(f,'sha256').hexdigest()+'  '+str(p.relative_to(source))+'\n')
    temp=target.with_suffix('.temp')
    subprocess.run(['tar','-I','zstd -T4 -3','-cf',temp,'-C',source,'.'],check=True)
    subprocess.run(['zstd','-tq',temp],check=True)
    with tempfile.TemporaryDirectory(dir=source.parent) as checkdir:
        subprocess.run(['tar','-I','zstd','-xf',temp,'-C',checkdir],check=True)
        subprocess.run(['sha256sum','--status','-c','SHA256SUMS'],cwd=checkdir,check=True)
    with temp.open('rb') as f:h=hashlib.file_digest(f,'sha256').hexdigest()
    target.with_suffix('.zst.sha256').write_text(h+'  '+target.name+'\n')
    temp.replace(target)
if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--seal',action='store_true')
    p.add_argument('stage');p.add_argument('name')
    a=p.parse_args()
    if a.seal:seal(a.stage,a.name)
    else:
        root=Path(__file__).resolve().parents[2]
        publish(a.name,a.stage,root/'out/campaign/archives',root.parent/'materials/paper_data')
