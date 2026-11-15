#!/usr/bin/env python3
from pathlib import Path
p=Path(__file__).with_name('replay_micro.py');s=p.read_text().replace('micro-v2','micro-final').replace("fp_scope='post-root suffix;", "warmup='one untimed kernel per phase and round; memo reset before timed kernel',fp_scope='post-root suffix;")
s=s.replace(" if not d.is_dir() or not (d/'output.corpus.gz').exists():continue"," if not d.is_dir() or not (d/'output.corpus.gz').exists() or (d/'micro-final-result.json').exists():continue")
exec(compile(s,str(p),'exec'),{'__file__':str(p),'__name__':'__main__'})
