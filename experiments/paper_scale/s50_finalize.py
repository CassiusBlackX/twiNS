"""Compare the returned s50 traces on the same machine that archives them."""
import json
from pathlib import Path
import subprocess
import sys
from ai_compare import compare

def finish(campaign):
    evidence=campaign/'s50'
    script=Path(__file__).with_name('irn_report.py')
    report={}
    for mode,h in [('incast',256),('spread',1024),('incast',1024)]:
        name=f'irn-{mode}-{h}'
        dirs=[evidence/f'wire-{mode}-{h}-{side}'/'trace'/name for side in ('gpu','cpu')]
        if not all(p.is_dir() for p in dirs):
            report[name]={'accepted':False,'reason':'missing diagnostic outputs'};continue
        leaves=['gpu.tx','device-intervals.txt','ports','reference.tx']
        raw=[dirs[0]/x for x in leaves[:3]]+[dirs[1]/leaves[3]]
        if not all(p.is_file() for p in raw):
            report[name]={'accepted':False,'reason':'incomplete trace'};continue
        output=dirs[0]/'report.json'
        subprocess.run([sys.executable,str(script),*map(str,dirs),str(output)],check=True)
        report[name]=json.loads(output.read_text())
    for case in ('large-buffer','small-buffer'):
        gpu=evidence/'live-ai/ai'/case
        if (gpu/'gpu.rc').is_file() and (gpu/'gpu.fct').is_file():
            report['ai-'+case]=compare(gpu,campaign/'a87/ai-v5'/case)
    (evidence/'comparisons.json').write_text(json.dumps(report,indent=2))
