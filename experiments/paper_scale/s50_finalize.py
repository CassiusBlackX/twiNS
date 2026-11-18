"""Compute expensive packet interval/report analysis on a87 after the paired s50 trials."""
import json
from pathlib import Path
import subprocess
from ai_compare import compare

def finish(campaign):
    evidence=campaign/'s50'
    remote='/home/cassius/netsim/twins-paper-scale-20261119/s50-analysis'
    subprocess.run(['ssh','a87','mkdir -p '+remote],check=True)
    script=Path(__file__).with_name('irn_report.py')
    subprocess.run(['rsync','-a',script,'a87:'+remote+'/'],check=True)
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
        subprocess.run(['ssh','a87','mkdir -p '+remote+'/'+name],check=True)
        subprocess.run(['rsync','-a',*raw,'a87:'+remote+'/'+name+'/'],check=True)
        subprocess.run(['ssh','a87',f'cd {remote} && python3 irn_report.py {name} {name} {name}/report.json'],check=True)
        subprocess.run(['rsync','-a','a87:'+remote+'/'+name+'/report.json',str(dirs[0])+'/'],check=True)
        report[name]=json.loads((dirs[0]/'report.json').read_text())
    for case in ('large-buffer','small-buffer'):
        gpu=evidence/'live-ai/ai'/case
        if (gpu/'gpu.rc').is_file() and (gpu/'gpu.fct').is_file():
            report['ai-'+case]=compare(gpu,campaign/'a87/ai-v5'/case)
    (evidence/'comparisons.json').write_text(json.dumps(report,indent=2))
