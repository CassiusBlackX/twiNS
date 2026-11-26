"""Stage only source, frozen inputs and acceptance hashes on s50, and return its results."""
import argparse
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile


SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=20", "s50"]
RSYNC = ["rsync", "-az", "-e", "ssh -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=20"]


def stage(project, data, base, output):
    with tempfile.TemporaryFile() as archive:
        subprocess.run(["git", "archive", "HEAD", "CMakeLists.txt", "src", "validation/ns3/twins-ref.cc",
                        "experiments/paper_tcp_options"], cwd=project, stdout=archive, check=True)
        archive.seek(0)
        with tarfile.open(fileobj=archive) as tar:
            tar.extractall(output, filter="data")
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=project, text=True)
    (output / "revision").write_text(commit)
    shutil.copytree(base / "inputs", output / "inputs")
    for name in ("references.json", "release-inventory.json"):
        shutil.copy2(base / name, output / name)
    entries = json.loads((base / "release-inventory.json").read_text())
    files = {arg.split("=", 1)[1] for case in entries for arg in case["args"]
             if arg.startswith(("--flows=", "--topofile=", "--ecmp_from="))}
    from native import digest
    checks = []
    for name in sorted(files):
        path = Path(name)
        if path.is_absolute() or not path.parts or path.parts[0] != "data" or ".." in path.parts:
            raise ValueError(f"unexpected resume input outside the data tree: {path}")
        target = output / path
        target.parent.mkdir(exist_ok=True, parents=True)
        shutil.copy2(data / path, target)
        checks.append(f"{digest(target)}  {path}\n")
    (output / "release-inputs.sha256").write_text("".join(checks))


def run(stage_dir, output):
    commit = (stage_dir / "revision").read_text().strip()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("invalid staged revision")
    remote = "/home/cassius/Videos/twins-paper-tcp-options-20261126/" + commit[:12]
    path = shlex.quote(remote)
    output.mkdir(parents=True)
    subprocess.run([*SSH, f"mkdir -p {path} && test ! -e {path}/results"], check=True)
    subprocess.run([*RSYNC, str(stage_dir) + "/", f"s50:{remote}/"], check=True)
    with (output / "worker.log").open("wb") as log:
        rc = subprocess.run([*SSH, f"cd {path} && bash experiments/paper_tcp_options/run_cuda.sh"],
                            stdout=log, stderr=subprocess.STDOUT).returncode
    (output / "ssh-returncode").write_text(f"{rc}\n")
    subprocess.run([*RSYNC, "--checksum", f"s50:{remote}/results/", str(output) + "/"], check=True)
    manifest = output / "return.sha256"
    with manifest.open("wb") as stream:
        subprocess.run([*SSH, f"set -o pipefail; cd {path}/results && find . -type f -print0 | sort -z | xargs -0 -r sha256sum"],
                       stdout=stream, check=True)
    subprocess.run(["sha256sum", "--quiet", "-c", manifest], cwd=output, check=True)
    if (output / "exit-status").is_file():
        subprocess.run([*SSH, f"cd {path} && test \"$(pwd -P)\" = {path} && test ! -L results && "
                       "test -f results/exit-status && rm -rf -- results"], check=True)
    return rc


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    modes = cli.add_subparsers(dest="mode", required=True)
    prepare = modes.add_parser("stage")
    for name in ("project", "data", "base", "output"):
        prepare.add_argument(name, type=lambda value: Path(value).resolve())
    execute = modes.add_parser("run")
    for name in ("stage", "output"):
        execute.add_argument(name, type=lambda value: Path(value).resolve())
    args = cli.parse_args()
    if args.mode == "stage":
        args.output.mkdir(parents=True)
        stage(args.project, args.data, args.base, args.output)
    else:
        raise SystemExit(run(args.stage, args.output))
