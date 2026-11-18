"""Relink pinned author libraries, replacing only the modified translation units."""
import os,re,subprocess,sys
from pathlib import Path
tree,objs,original,dst=map(lambda s:Path(s).resolve(),sys.argv[1:])
dst.mkdir(exist_ok=True,parents=True)
for lib in original.glob('libns3*.so'):
    p=dst/lib.name
    if not p.exists():p.symlink_to(lib)
for mod in ('network','internet','point-to-point'):
    name=f'libns3.19-{mod}-optimized.so'
    lib=original/name
    deps=re.findall(r'\(NEEDED\).*?\[(.*?)\]',subprocess.check_output(['readelf','-d',lib],text=True))
    objects=[]
    for p in sorted((tree/'build/src'/mod).rglob('*.1.o')):
        replacement=objs/(p.name.split('.cc.')[0]+'.o')
        objects.append(replacement if replacement.exists() else p)
    assert objects
    target=dst/name
    if target.is_symlink():target.unlink()
    new=dst/(name+'.new')
    subprocess.run(['g++','-shared','-Wl,-soname,'+name,'-o',new,*objects,'-L'+str(dst),'-Wl,--no-as-needed',*['-l:'+d for d in deps],'-pthread'],check=True)
    os.replace(new,target)
