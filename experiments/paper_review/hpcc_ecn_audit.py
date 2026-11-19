"""Build a read-only-instrumented HPCC oracle and an explicit global-stream prototype."""
import argparse
from pathlib import Path
import re
import subprocess


def replace_once(text, old, new):
    if text.count(old) != 1:
        raise ValueError(f"unexpected reference source: {old}")
    return text.replace(old, new)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("tree", type=Path)
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    tree, out = args.tree.resolve(), args.out.resolve()
    out.mkdir(parents=True)
    built = tree/"build"
    for lib in built.glob("libns3*.so"):
        (out/lib.name).symlink_to(lib)
    edits = {
        "core": ("rng-seed-manager", "    g_nextStreamIndex++;",
                 "    g_nextStreamIndex++;\n    twinsAuditLastStream = next;", ""),
        "point-to-point": ("switch-mmu",
            "if ((ecn_rng[ifindex] ? ecn_rng[ifindex]->RandU01() : UniformVariable(0, 1).GetValue()) < p)",
            r'''uint64_t stream = UINT64_MAX;
            double draw;
            if (ecn_rng[ifindex])
                draw = ecn_rng[ifindex]->RandU01();
            else if (getenv("TWINS_ECN_EXPLICIT_GLOBAL")) {
                stream = RngSeedManager::GetNextStreamIndex();
                RngStream rng(RngSeedManager::GetSeed(), stream, RngSeedManager::GetRun());
                draw = rng.RandU01();
            } else {
                draw = UniformVariable(0, 1).GetValue();
                stream = twinsAuditLastStream;
            }
            fprintf(stderr, "ecn-draw %llu %llu %u %u %u %u %a %a %d\n",
                (unsigned long long)Simulator::Now().GetTimeStep(),
                (unsigned long long)stream, twinsAuditSwitch[this],
                ifindex, qIndex, egress_bytes[ifindex][qIndex], draw, p, draw < p);
            if (draw < p)''',
            '#include <map>\n#include <cstdlib>\n#include <cstdio>\n'),
    }
    for mod, (stem, anchor, replacement, includes) in edits.items():
        directory = tree/"src"/mod/"model"
        body = (directory/(stem+".cc")).read_text()
        if mod == "core":
            body = replace_once(body, "uint64_t RngSeedManager::GetNextStreamIndex (void)",
                                'extern "C" { uint64_t twinsAuditLastStream = 0; }\n\nuint64_t RngSeedManager::GetNextStreamIndex (void)')
            # Preserve the reference's original indentation.
            anchor, replacement = anchor[2:], replacement.replace("    ", "  ")
        else:
            body = replace_once(body, "namespace ns3 {", '''namespace ns3 {
        extern "C" uint64_t twinsAuditLastStream;
        static std::map<const SwitchMmu*,uint32_t> twinsAuditSwitch;''')
            body = replace_once(body,
                "ecn_rng[port] = new RngStream(RngSeedManager::GetSeed(), (1ull << 63) + stream, RngSeedManager::GetRun());",
                """twinsAuditSwitch[this] = stream / pCnt;
                if (getenv("TWINS_ECN_ORIGINAL")) return;
                ecn_rng[port] = new RngStream(RngSeedManager::GetSeed(), (1ull << 63) + stream, RngSeedManager::GetRun());""")
        modified = out/(stem+".cc")
        modified.write_text(includes+replace_once(body,anchor,replacement))
        obj = out/(stem+".o")
        subprocess.run(["g++","-O3","-std=gnu++11","-fPIC","-w",f"-I{built}",f"-I{tree}",
                        f"-I{directory}","-c",modified,"-o",obj], check=True)
        name=f"libns3.18-{mod}-optimized.so"
        soname_deps=re.findall(r"\(NEEDED\).*?\[(.*?)\]",subprocess.check_output(
            ["readelf","-d",built/name],text=True))
        entries=[obj if p.name==stem+".cc.1.o" else p for p in sorted((built/"src"/mod).rglob("*.1.o"))]
        assert obj in entries
        target=out/name
        target.unlink()
        subprocess.run(["g++","-shared",f"-Wl,-soname,{name}","-o",target,*entries,
                        f"-L{out}","-Wl,--no-as-needed",*["-l:"+d for d in soname_deps],"-pthread"],check=True)


if __name__=="__main__":
    main()
