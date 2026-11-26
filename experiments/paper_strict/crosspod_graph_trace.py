"""Measure GeDES graph envelopes with only graph-level tracing."""

from pathlib import Path
import subprocess
import sys

import crosspod
from crosspod_attribution import ALIGNED,CASES,prepare
from crosspod_large import binaries
from crosspod_timeline import capture,export


def measure(out):
    frozen,_=binaries(ALIGNED/'build')
    for tag in CASES:
        capture(out/'profiles'/tag/'gedes',tag,'gedes',frozen['gedes'],out,'graph')


def main():
    out=Path(sys.argv[1]).resolve()
    if len(sys.argv)>2:
        return measure(out)
    out.mkdir()
    prepare(out)
    subprocess.run(['flock',crosspod.LOCK,sys.executable,crosspod.OBSERVER,out/'resources',
                    sys.executable,__file__,out,'capture'],check=True)
    for tag in CASES:
        export(out,tag,'gedes')


if __name__=='__main__':
    main()
