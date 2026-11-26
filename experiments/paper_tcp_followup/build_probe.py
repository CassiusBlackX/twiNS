"""Link a small native probe using the frozen oracle's compilation and link flags."""
import argparse
import json
from pathlib import Path
import shlex
import subprocess

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("tree", type=lambda value: Path(value).resolve())
parser.add_argument("source", type=lambda value: Path(value).resolve())
parser.add_argument("output", type=lambda value: Path(value).resolve())
args = parser.parse_args()
args.output.mkdir(parents=True, exist_ok=False)
cache = args.tree / "cmake-cache"
entry = next(row for row in json.loads((cache / "compile_commands.json").read_text())
             if Path(row["file"]) == args.tree / "scratch/twins-ref.cc")
cc = shlex.split(entry["command"])
obj = args.output / "probe.o"
old = cc[cc.index("-o") + 1]
cc[cc.index("-o") + 1] = str(obj)
cc[-1] = str(args.source)
commands = subprocess.check_output(["ninja", "-C", cache, "-t", "commands",
    args.tree / "build/scratch/ns3.46.1-twins-ref-optimized"], text=True)
link = [str(obj) if token == old else token for token in shlex.split(commands.splitlines()[-1]) if token not in (":", "&&")]
link[link.index("-o") + 1] = str(args.output / "probe")
(args.output / "build.json").write_text(json.dumps(dict(compile=cc, link=link), indent=2) + "\n")
with (args.output / "build.log").open("wb") as log:
    subprocess.run(cc, cwd=entry["directory"], stdout=log, stderr=subprocess.STDOUT, check=True)
    subprocess.run(link, cwd=cache, stdout=log, stderr=subprocess.STDOUT, check=True)
