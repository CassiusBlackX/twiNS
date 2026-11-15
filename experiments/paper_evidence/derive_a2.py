#!/usr/bin/env python3
"""Receipt -> A2 tables/figures. No LaTeX edits, no pooling different source revisions."""
import hashlib,json,math,re,statistics
from receipt_status import terminal_profile
from pathlib import Path
E=Path('/home/cassius/Work/SpecDES/twiNS/out/paper-evidence-20261116');R=E/'reports';F=R/'figures';F.mkdir(exist_ok=True)
files=sorted(list((E/'local/profile-final').glob('*/result.json'))+list((E/'local/growth').glob('*/result.json')))
fingerprint=hashlib.sha256(b''.join(p.read_bytes() for p in files)+Path(__file__).read_bytes()).hexdigest()
cache=R/'a2-derived.json'
if cache.exists() and json.loads(cache.read_text()).get('receipt_fingerprint')==fingerprint:raise SystemExit(0)
def parsed(x):
 if 'counters' in x:return x['counters']
 return dict(l.split(': ',1) for l in x.get('metrics',[]) if ': ' in l)
def bounds(hist,q):
 h=list(map(int,hist.split()));target=sum(h)*q;cum=0
 for i,n in enumerate(h):
  cum+=n
  if cum>=target:return [0 if i==0 else 2**i,1 if i==0 else 2**(i+1)-1]
 return None
rows=[];groups={}
for p in files:
 x=terminal_profile(json.loads(p.read_text()),p);x['receipt']=str(p.relative_to(E))
 if 'case' in x:groups.setdefault(x['case'],{}).setdefault(x['variant'],[]).append(x)
 if x.get('variant')!='profile':continue
 c=parsed(x);n=list(map(int,c['evidence_counts'].split()));mem={k:int(v) for k,v in re.findall(r'(\w+)=(\d+)',c['evidence_storage'])};instrument=int(re.search(r'instrumentation_bytes=(\d+)',c['evidence_schema'])[1]);extra=0
 if x.get('kind')=='tcp':
  k=16 if x['case'].startswith('tcp-ordinary') else 8;runs=int(re.search(r'runs(\d+)',x['case'])[1]);extra=(5*k*k//4)*k*(2*runs+2)*2
 inline=mem['inline_allocated']+extra;allocated=mem['tracked_device_allocated']-instrument
 row=dict(case=x.get('case','growth-x'+str(x.get('scale'))),round=x.get('round'),receipt=x['receipt'],accepted=x.get('accepted',x.get('equal') and not x.get('flagged') and x['rc']==0),pushes=n[0],max_observed_length=n[7],mean_recorded_length=n[1]/max(1,n[0]),mean_inline_runs=n[2]/max(1,n[0]),retained_tail_pointer_fraction=n[5]/max(1,n[0]),truncated_parent_fraction=n[6]/max(1,n[0]),comparison_calls=n[9],mean_walk_steps=n[8]/max(1,n[9]),length_p50_bounds=bounds(c['evidence_length_log2'],.5),length_p99_bounds=bounds(c['evidence_length_log2'],.99),depth_p99_bounds=bounds(c['evidence_comparison_steps_log2'],.99),full_rle_known_samples=n[10],full_rle_unknown_samples=n[13],full_rle_max=n[14],sampled_full_length_per_run=n[12]/n[11] if n[11] else None,inline_history_bytes=inline,inline_fp_field_bytes=0 if x.get('kind')=='tcp' else inline//5,tcp_switch_history_correction_bytes=extra,tracked_production_allocated_highwater_bytes=allocated,tail_bytes=mem['tail_bytes'],memo_bytes=mem['memo_bytes'],other_tracked_model_bytes=allocated-inline-mem['tail_bytes']-mem['memo_bytes'],instrumentation_bytes=instrument,tail_allocations=mem['tail_allocations'],tail_overwrite_count=mem['tail_overwrite_count'],tail_slots=mem['tail_slots'],histograms={k:c[k] for k in ('evidence_length_log2','evidence_inline_runs_log2','evidence_comparison_steps_log2')})
 rows.append(row)
timings=[]
for name,variants in sorted(groups.items()):
 if not {'clean','profile'}<=variants.keys():continue
 a,b=variants['clean'],variants['profile'];ca=[v['wall_ns']/1e9 for v in a];cb=[v['wall_ns']/1e9 for v in b]
 timings.append(dict(case=name,clean_s=ca,profile_s=cb,ratio=statistics.median(cb)/statistics.median(ca),accepted_clean=sum(v['accepted'] for v in a),accepted_profile=sum(v['accepted'] for v in b),strict=[dict(wall_s=v['wall_ns']/1e9,timeout=v['timeout'],accepted=v['accepted'],internal_retries=v.get('internal_retries',0),receipt=v['receipt'],peak_device_bytes=v.get('memory_observations',{}).get('peak_total_device_bytes',{})) for v in variants.get('strict',[])]))
data=dict(receipt_fingerprint=fingerprint,evidence_source='48e0a3a0ffce5d21ac52a175449dccd71cfbf2a7',rows=rows,timings=timings,limitations=['Push/comparison weighted, not distinct-live-chain distributions. TCP lengths are recorded-prefix construction lengths, not full logical lineage.','RDMA full RLE sampled every4096 pushes; unknown tail-depth samples excluded from ratio and reported.','Retained tail pointer on push is not a count of shared live ancestors. Tail allocated/overwritten != live tail occupancy.','Allocation ledger has no device Free calls: tracked explicit allocation highwater. Excludes GPU driver/runtime. Whole-device observed VRAM includes desktop.','TCP switch last_chain storage is corrected analytically, k=16 or8 single fattree, from actual source allocation. RDMA auxiliary64MiB is removed from production totals.','TWINS_NO_FP still carries/updates fingerprints. No separate causal end-to-end carrying cost is inferred.','Microbench fixed contiguous layout does not preserve production cache locality.'])
cache.write_text(json.dumps(data,indent=2)+'\n')
lines=['# A2 可供论文使用的受控结果','','源码固定 `48e0a3a`，48 次 clean/profile 测量（8 个设置×2×3）；短容量两项 flags 保留。后续 strict 超时为一小时删失值，见实时页。','','| 设置 | clean 中位数 s | 插桩中位数 s | 插桩/clean | clean 通过/次数 | profile 通过/次数 |','|---|---:|---:|---:|---:|---:|']
for x in timings:lines.append(f'| {x["case"]} | {statistics.median(x["clean_s"]):.9f} | {statistics.median(x["profile_s"]):.9f} | {x["ratio"]:.3f} | {x["accepted_clean"]}/{len(x["clean_s"])} | {x["accepted_profile"]}/{len(x["profile_s"])} |')
lines+=['','下面计数/存储表取第 1 轮 profile；JSON 保留全部轮次及直方图。RDMA 的长度/RLE 是逻辑 leads；TCP 是有限前缀，不能直接互比压缩率。','','| 设置 | 最大观测长度 | 平均 walk steps | 完整 RLE length/runs | inline MiB | tail MiB | memo MiB | 其他显式状态 MiB |','|---|---:|---:|---:|---:|---:|---:|---:|']
for x in rows:
 if x['round']!=1:continue
 ratio=f'{x["sampled_full_length_per_run"]:.3f}' if x['sampled_full_length_per_run'] is not None else '前缀，未给完整比率'
 lines.append(f'| {x["case"]} | {x["max_observed_length"]} | {x["mean_walk_steps"]:.3f} | {ratio} | {x["inline_history_bytes"]/2**20:.3f} | {x["tail_bytes"]/2**20:.3f} | {x["memo_bytes"]/2**20:.3f} | {x["other_tracked_model_bytes"]/2**20:.3f} |')
lines+=['','集合通信不应被写成“主要靠 RLE 获得巨大压缩”：本组完整 RLE 的 reduction 很小，固定帧内前缀、共享的外部尾节点及 FP 比较捷径分别承担不同作用。tail 累计分配可以远超 ring 容量，不能把累计节点数当同时驻留量。','','## 解释边界','']+['- '+x for x in data['limitations']]
lines+=['','重跑语义：最终 pass 的 flags 与初始诊断分开，wall/峰值内存覆盖全部重跑。strict 可触发 tail 扩容，因此端到端差值不等于纯 FP 指令成本。strict 当前每例一次，不能编造三次范围。','','微基准补充审计：micro-final 的 fingerprint_shortcut 阶段仍含 corpus 捕获钩子，其旧时间排除。其他阶段无该钩子；修正版将直接调用 ccmp_impl，另存 micro-isolated-result.json（core6bc5a7c），不覆盖旧记录。端到端48次对照不受影响。','','原始文件映射：`reports/a2-derived.json` 每行含 receipt；静态图 `reports/figures/a2-timing.pdf`、`a2-storage.pdf`、`a2-length-cdf.pdf`、`growth.pdf` 由 `experiments/paper_evidence/derive_a2.py` 生成。不直接修改论文图或 LaTeX。']
(R/'a2-paper-tables.md').write_text('\n'.join(lines)+'\n')
try:import matplotlib;matplotlib.use('Agg');import matplotlib.pyplot as plt
except ImportError:raise SystemExit('Tables saved; plotting requires matplotlib.')
plt.rcParams.update({'font.size':8,'pdf.fonttype':42,'ps.fonttype':42})
names=[x['case'].replace('c_allgather_512m_hpn1024','allgather1024').replace('gpt3_13b_probe_hpn128','GPT13B probe') for x in timings];ys=list(range(len(timings)))
fig,ax=plt.subplots(figsize=(7,3.4))
for v,offset,color in [('clean_s',-.16,'#336699'),('profile_s',.16,'#be6b35')]:
 for i,x in enumerate(timings):
  t=x[v];m=statistics.median(t);ax.errorbar(m,i+offset,xerr=[[m-min(t)],[max(t)-m]],fmt='o',color=color,capsize=2,label=v[:-2] if i==0 else None)
ax.set(yticks=ys,yticklabels=names,xscale='log',xlabel='End-to-end wall seconds (median and min–max, n=3)');ax.invert_yaxis();ax.legend();fig.tight_layout();fig.savefig(F/'a2-timing.pdf');fig.savefig(F/'a2-timing.png',dpi=180);plt.close(fig)
mode_rows=[x for x in timings if x['strict']];fig,ax=plt.subplots(figsize=(7,2.8))
for i,x in enumerate(mode_rows):
 t=x['clean_s'];m=statistics.median(t);ax.errorbar(m,i-.12,xerr=[[m-min(t)],[max(t)-m]],fmt='o',color='#336699',capsize=2,label='FP shortcut enabled (median/range, n=3)' if i==0 else None)
 v=x['strict'][0];ax.plot(v['wall_s'],i+.12,'x',color='#be6b35',label='FP shortcut disabled (single, all retries)' if i==0 else None)
 if v['timeout']:ax.annotate('>=1h censored',(v['wall_s'],i+.12),fontsize=6)
 elif not v['accepted']:ax.annotate('output/flag check failed',(v['wall_s'],i+.12),fontsize=6)
ax.set(yticks=range(len(mode_rows)),yticklabels=[x['case'].replace('c_allgather_512m_hpn1024','allgather1024').replace('gpt3_13b_probe_hpn128','GPT13B probe') for x in mode_rows],xscale='log',xlabel='End-to-end seconds; disabling the shortcut can grow tail storage');ax.invert_yaxis()
if mode_rows:ax.legend(fontsize=6)
fig.tight_layout();fig.savefig(F/'a2-fp-vs-walk.pdf');fig.savefig(F/'a2-fp-vs-walk.png',dpi=180);plt.close(fig)
valid=[x for x in rows if x['round']==1 and x['accepted']];fig,ax=plt.subplots(figsize=(7,3.3));left=[0]*len(valid)
for k,label in [('inline_history_bytes','Inline history (FP for RDMA/AI)'),('tail_bytes','Tail ring'),('memo_bytes','Exact memo'),('other_tracked_model_bytes','Other model state')]:
 vals=[x[k]/2**20 for x in valid];ax.barh(range(len(valid)),vals,left=left,label=label);left=[a+b for a,b in zip(left,vals)]
ax.set(yticks=range(len(valid)),yticklabels=[x['case'].replace('c_allgather_512m_hpn1024','allgather1024').replace('gpt3_13b_probe_hpn128','GPT13B probe') for x in valid],xlabel='Explicit tracked GPU allocations (MiB), excluding profiling auxiliary');ax.invert_yaxis();ax.legend(fontsize=7);fig.tight_layout();fig.savefig(F/'a2-storage.pdf');plt.close(fig)
fig,ax=plt.subplots(figsize=(6,3.2))
for x in valid:
 h=list(map(int,x['histograms']['evidence_length_log2'].split()));n=sum(h);cum=0;cdf=[]
 for v in h:cum+=v;cdf.append(cum/n)
 ax.step([1 if i==0 else 2**(i+1)-1 for i in range(32)],cdf,where='post',label=x['case'])
ax.set(xscale='log',xlim=(1,2**21),ylim=(0,1.02),xlabel='Length bucket upper bound (TCP: recorded prefix)',ylabel='Push-weighted cumulative fraction');ax.legend(fontsize=6);fig.tight_layout();fig.savefig(F/'a2-length-cdf.pdf');plt.close(fig)
growth=[json.loads(p.read_text()) for p in files if '/growth/' in str(p)];fig,axs=plt.subplots(1,2,figsize=(7,2.8))
for variant in ('clean','profile','strict'):
 a=sorted([x for x in growth if x['variant']==variant],key=lambda x:x['scale'])
 if a:
  axs[0].plot([x['scale'] for x in a],[x['wall_ns']/1e9 for x in a],'-o',label=variant)
  for x in a:
   if x['timeout']:axs[0].annotate('≥3600s',(x['scale'],x['wall_ns']/1e9),fontsize=6)
g=[x for x in rows if x['round'] is None];axs[1].plot([int(x['case'].split('x')[-1]) for x in g],[x['tail_allocations'] for x in g],'-o',label='Cumulative tail allocations');axs[1].axhline(2**24,ls='--',color='gray',label='Fixed ring slots')
axs[0].set(yscale='log',xlabel='Flow-size multiplier',ylabel='Wall seconds');axs[1].set(xlabel='Flow-size multiplier',ylabel='Tail nodes (count, not bytes)');axs[0].legend(fontsize=6);axs[1].legend(fontsize=6);fig.tight_layout();fig.savefig(F/'growth.pdf');plt.close(fig)
(R/'figure-provenance.json').write_text(json.dumps(dict(generator='experiments/paper_evidence/derive_a2.py',generator_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),receipt_fingerprint=fingerprint,matplotlib_version=matplotlib.__version__,sources=[str(p.relative_to(E)) for p in files],artifacts={str(p.relative_to(E)):hashlib.sha256(p.read_bytes()).hexdigest() for p in F.glob('*')},source='48e0a3a'),indent=2)+'\n')
