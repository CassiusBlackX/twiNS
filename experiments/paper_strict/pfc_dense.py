"""Confirm the completed high-occupancy 6 MiB pool / 8 MiB burst at the same scale."""
from pathlib import Path
import subprocess
import sys
import time
import pfc_sustained as study

study.DESIGN=[('sustained-candidate',6,8)]

def main():
    out=Path(sys.argv[1]).resolve()
    if len(sys.argv)>2:
        return study.measure(out,Path(sys.argv[2]))
    out.mkdir()
    cases=out/'measurements'
    study.freeze(cases)
    study.reference(cases,core=8)
    deadline=time.monotonic()+3600
    source=study.EVIDENCE/'pfc_sustained'
    while time.monotonic()<deadline:
        epochs=sorted(source.glob('20*'))
        if epochs:
            binary=epochs[-1]/'results/build-receipts/compiled/twins-ai'
            if binary.is_file() and (binary.parent.parent/'compile_commands.json').is_file():
                break
        time.sleep(15)
    else:
        raise RuntimeError('the corrected CUDA counter build did not complete')
    return subprocess.run(['flock',study.LOCK,sys.executable,study.OBSERVER,out/'resources',
        sys.executable,__file__,cases,binary]).returncode

if __name__=='__main__':
    raise SystemExit(main())
