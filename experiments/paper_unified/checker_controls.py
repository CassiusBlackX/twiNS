"""Use archived SimAI output to exercise the closed-loop comparator's failure paths."""
import json
from pathlib import Path
import subprocess
import sys

checker, archived, out = (Path(x).resolve() for x in sys.argv[1:])
out.mkdir()
ref = archived / "normalized.fct"
dag = archived / "normalized.dag"
message_lines=ref.read_text().splitlines(keepends=True)
identity=max(int(line.split()[0]) for line in message_lines)+1
summary={}
for mode,rows in {"unchanged":message_lines, "extra":[*message_lines,f"{identity} 0 0\n"],
                  "missing":message_lines[1:], "duplicate":[message_lines[0],*message_lines]}.items():
    candidate=out/f"{mode}.fct"
    candidate.write_text("".join(rows))
    command=[sys.executable,str(checker),str(ref),str(candidate),str(dag),str(dag)]
    run=subprocess.run(command,capture_output=True,text=True,check=False)
    summary[mode]=json.loads(run.stdout)
    assert run.returncode == int(mode!="unchanged"), mode
    if mode!="unchanged":
        key="duplicate_candidate" if mode=="duplicate" else mode
        assert len(summary[mode]["messages"][key])==1, mode
# Find independent native timer callbacks sharing one execution time on the actual setup.
source=[ln.split() for ln in (archived/"events.trace").read_text().splitlines()]
times={int(r[1]): int(float(r[2]))+int(r[6]) for r in source if r[0]=="T"}
timers=[(int(r[2]),times[int(r[2])]) for r in source if r[0]=="C" and r[1]=="t"]
first=next(i for i, (x,y) in enumerate(zip(timers,timers[1:])) if x[1]==y[1] and x[0]!=y[0])
for name, log in (("reference",timers),("transposed",timers[:first]+[timers[first+1],timers[first]]+timers[first+2:])):
    (out/f"{name}.order").write_text("".join(f"C t {i} {ns}\n" for i,ns in log))
run=subprocess.run([sys.executable,str(checker),str(ref),str(ref),str(dag),str(dag),
                   "--reference-execution",str(out/"reference.order"),"--candidate-execution",str(out/"transposed.order")],
                   capture_output=True,text=True,check=False)
summary["transposed"]=json.loads(run.stdout)
assert run.returncode==1 and summary["transposed"]["first_callback_difference"]["line"]==first+1
(out/"summary.json").write_text(json.dumps(summary,indent=2)+"\n")
