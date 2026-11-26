"""Rebuild a TCP reference observer using a frozen ns-3 compile/link receipt."""
import json
from pathlib import Path
import subprocess
import sys

receipt, src, out=(Path(x).resolve() for x in sys.argv[1:])
info=json.loads(receipt.read_text())
compile=list(info["compile"])
link=list(info["link"])
obj=out.with_suffix(".o")
old_obj=compile[compile.index("-o")+1]
old_exe=link[link.index("-o")+1]
compile=[str(obj) if a==old_obj else str(src) if a==compile[-1] else a for a in compile]
link=[str(obj) if a==old_obj else str(out) if a==old_exe else a for a in link]
for cmd in (compile,link):
    subprocess.run(cmd,check=True,timeout=150)
