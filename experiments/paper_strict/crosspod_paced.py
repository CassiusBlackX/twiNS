"""Run the fixed pacing control following the documented overloaded-input pilot."""
from pathlib import Path
import subprocess
import sys
import crosspod
out=Path(sys.argv[1]).resolve()
out.mkdir(parents=True)
src=Path(__file__).parent
subprocess.run([sys.executable,src/'crosspod_inputs.py',out/'inputs','paced'],check=True)
build=sorted((crosspod.ROOT/'crosspod').glob('20*'))[-1]/'results/builds'
raise SystemExit(subprocess.run(['flock',crosspod.LOCK,sys.executable,crosspod.OBSERVER,out/'resources',
    sys.executable,src/'crosspod.py',out/'measurements',build,out/'inputs']).returncode)
