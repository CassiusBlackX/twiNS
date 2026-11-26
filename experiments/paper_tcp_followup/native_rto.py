"""Build an isolated, audited native TCP library without changing the oracle tree."""
import argparse
import hashlib
import json
from pathlib import Path
import shlex
import subprocess


def build(tree, output):
    output.mkdir(parents=True, exist_ok=False)
    cache = tree / "cmake-cache"
    original = tree / "src/internet/model/tcp-socket-base.cc"
    text = original.read_text()
    anchor = "    uint32_t inFlightBeforeRto = BytesInFlight();"
    assert text.count(anchor) == 1
    audit = '''    if (std::getenv("TWINS_TRACE_TCP_RTO") && m_endPoint)
    {
        std::cout << "NATIVE_TCP_RTO " << Simulator::Now().GetPicoSeconds()
                  << ' ' << m_node->GetId()
                  << ' ' << m_endPoint->GetLocalAddress().Get()
                  << ' ' << m_endPoint->GetPeerAddress().Get()
                  << ' ' << m_endPoint->GetLocalPort()
                  << ' ' << int(m_tcb->m_congState.Get())
                  << ' ' << m_txBuffer->HeadSequence().GetValue()
                  << ' ' << m_tcb->m_highTxMark.Get().GetValue() << std::endl;
    }

'''
    source = output / original.name
    source.write_text('#include <cstdlib>\n#include <iostream>\n' + text.replace(anchor, audit + anchor))
    entries = json.loads((cache / "compile_commands.json").read_text())
    entry = next(row for row in entries if Path(row["file"]) == original)
    cc = shlex.split(entry["command"])
    old_obj = cc[cc.index("-o") + 1]
    obj = output / "tcp-socket-base.cc.o"
    cc[cc.index("-o") + 1] = str(obj)
    cc[-1] = str(source)
    cc[1:1] = ["-I" + str(original.parent)]
    target = tree / "build/lib/libns3.46.1-internet-optimized.so"
    commands = subprocess.check_output(["ninja", "-C", cache, "-t", "commands", target], text=True)
    link = [str(obj) if token == old_obj else token
            for token in shlex.split(commands.splitlines()[-1]) if token not in (":", "&&")]
    library = output / target.name
    link[link.index("-o") + 1] = str(library)
    assert str(obj) in link
    receipt = dict(original_sha256=hashlib.sha256(original.read_bytes()).hexdigest(),
                   instrumented_sha256=hashlib.sha256(source.read_bytes()).hexdigest(), compile=cc, link=link)
    (output / "build.json").write_text(json.dumps(receipt, indent=2) + "\n")
    with (output / "build.log").open("wb") as log:
        subprocess.run(cc, cwd=entry["directory"], stdout=log, stderr=subprocess.STDOUT, check=True)
        subprocess.run(link, cwd=cache, stdout=log, stderr=subprocess.STDOUT, check=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tree", type=lambda value: Path(value).resolve())
    parser.add_argument("output", type=lambda value: Path(value).resolve())
    args = parser.parse_args()
    build(args.tree, args.output)
