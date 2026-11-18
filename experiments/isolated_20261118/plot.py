#!/usr/bin/env python3
"""Final scientific PNG/SVG figures, separate latency/batch timing, no LaTeX edits."""
import argparse,collections,hashlib,json,statistics
from pathlib import Path
def main():
 ap=argparse.ArgumentParser();ap.add_argument('--evidence',type=Path,required=True);a=ap.parse_args();E=a.evidence;O=E/'reports';v=json.loads((O/'live-data.json').read_text());lat=[x for x in v['latency'] if 'latency' in x['formal_id'] and x['accepted']];batches=[x for x in v['batches'] if x['all_accepted']]
 assert len(lat)==72 and len(batches)==18,'Final figures require all planned accepted eligible observations'
 import matplotlib;matplotlib.use('Agg')
 import matplotlib.pyplot as plt
 import numpy as np
 plt.rcParams.update({'font.size':10,'svg.fonttype':'none'})
 names=[j['case'] for j in json.loads((Path(__file__).parent/'reuse/matrix.json').read_text())['jobs']];labels=['Allgather 8MiB /128','Allgather 512MiB /1024','GPT-3 13B full /128','LLaMA 65B full /128','GPT-3 175B full /128','GPT-3 13B mid /512','LLaMA 65B mid /1024','GPT-3 175B probe /1024'];cols=['#555555','#0072b2','#d55e00']
 def summary(xs):
  vals=[x/1e9 for x in xs];assert len(vals)==3;return statistics.median(vals),min(vals),max(vals)
 fig,ax=plt.subplots(figsize=(11.2,5.6))
 for i,eng in enumerate(('simai','p0','p16')):
  stats=[summary([x['wall_ns'] for x in lat if x['case']==n and x['engine']==eng]) for n in names];m=np.array([x[0] for x in stats]);err=np.array([[x[0]-x[1] for x in stats],[x[2]-x[0] for x in stats]]);ax.errorbar(m,np.arange(8)+(i-1)*.22,xerr=err,fmt='o',markersize=4,capsize=3,color=cols[i],label={'simai':'Sequential SimAI','p0':'TwiNS P=0','p16':'TwiNS P=16'}[eng])
 ax.set(yticks=np.arange(8),yticklabels=labels,xscale='log',xlabel='Process wall time (seconds, log scale)',title='Candidate 6bc5a7c, isolated settings: median and range, n=3');ax.invert_yaxis();ax.grid(axis='x',alpha=.2);ax.legend(loc='upper right');fig.tight_layout()
 for ext in ('png','svg'):fig.savefig(O/('latency-isolated.'+ext),dpi=180)
 plt.close(fig)
 settings=[('simai',i) for i in (1,2,4,8)]+[('p0',1),('p16',1)];st=[summary([x['makespan_ns'] for x in batches if (x['engine'],x['target_concurrency'])==s]) for s in settings];peaks=[sorted({x['peak_actual_concurrency'] for x in batches if (x['engine'],x['target_concurrency'])==s}) for s in settings];fig,ax=plt.subplots(figsize=(9.5,4.6));med=np.array([x[0]/3600 for x in st]);err=np.array([[(x[0]-x[1])/3600 for x in st],[(x[2]-x[0])/3600 for x in st]]);ax.bar(np.arange(6),med,color=['#777777']*4+cols[1:]);ax.errorbar(np.arange(6),med,yerr=err,fmt='none',ecolor='black',capsize=4)
 labels=[f'CPU target{s[1]}\n(actual peak {peaks[i]})' for i,s in enumerate(settings[:4])]+['TwiNS P=0','TwiNS P=16']
 for i,t in enumerate(st):ax.text(i,med[i]+max(med)*.025,f'{t[0]/60:.2f} min',ha='center',fontsize=9)
 ax.set(xticks=np.arange(6),xticklabels=labels,ylabel='Eight-job batch makespan (hours)',ylim=(0,max(med)*1.17),title='Isolated batches, same eight jobs, 70GiB: median and range, n=3');ax.grid(axis='y',alpha=.2);ax.set_axisbelow(True);fig.tight_layout()
 for ext in ('png','svg'):fig.savefig(O/('throughput-isolated.'+ext),dpi=180)
 plt.close(fig)
 h=lambda p:hashlib.sha256(p.read_bytes()).hexdigest();best=min(x[0] for x in st[:4]);data=dict(generator_sha256=h(Path(__file__)),source_json_sha256=h(O/'live-data.json'),artifacts={p.name:h(p) for p in O.glob('*-isolated.*')},best_cpu_median_s=best,p0_speedup_vs_best_cpu=best/st[4][0],p16_speedup_vs_best_cpu=best/st[5][0],scope='PNG/SVG artifacts from all72 accepted eligible latency and18 batch observations; range is observed min/max, no confidence interval; inspect PNG layout before paper use')
 (O/'figure-provenance.json').write_text(json.dumps(data,indent=2)+'\n')
if __name__=='__main__':main()
