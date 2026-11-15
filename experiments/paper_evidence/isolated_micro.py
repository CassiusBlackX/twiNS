#!/usr/bin/env python3
"""Independent corrected FP-stage timing, preserving every older receipt."""
from pathlib import Path
p=Path(__file__).with_name('replay_micro.py');s=p.read_text().replace('micro-v2','micro-isolated').replace("SRC/'build-micro/twins-lineage-bench'","SRC/'build-boundary-candidate/twins-lineage-bench'")
s=s.replace(" if not d.is_dir() or not (d/'output.corpus.gz').exists():continue"," if not d.is_dir() or not (d/'output.corpus.gz').exists() or (d/'micro-isolated-result.json').exists():continue")
s=s.replace("fp_scope='post-root suffix;","source_core='6bc5a7c',sampling_hook='bypassed: compare_inputs calls ccmp_impl directly; older micro-final fingerprint_shortcut times include corpus capture and are excluded',warmup='untimed per phase and round',fp_scope='post-root suffix;")
exec(compile(s,str(p),'exec'),{'__file__':str(p),'__name__':'__main__'})
