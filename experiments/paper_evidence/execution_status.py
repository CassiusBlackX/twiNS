#!/usr/bin/env python3
"""Durable cross-host status, from local receipts; executed != passed != published."""
import datetime,json,time,subprocess
from collections import Counter
from pathlib import Path
E=Path('/home/cassius/Work/SpecDES/twiNS/out/paper-evidence-20261116');MAT=Path('/home/cassius/Projects/TwiNS')
def read(p,default=None):
 try:return json.loads(p.read_text())
 except (OSError,ValueError):return default
def results(path):return [v for p in path.glob('*/result.json') if (v:=read(p))]
while True:
 local_done=(E/'local/all-finished.json').exists();remote_done=(E/'s50/export/all-finished.json').exists();hip=read(E/'receipts/hip-boundary-result.json');cuda=read(E/'s50/export/candidate-gates/summary.json');archive=read(E/'receipts/local-archive-verification.json',{})
 profiles=results(E/'local/profile-final');strict=[v for v in profiles if v['variant']=='strict'];growth=results(E/'local/growth');unified=results(E/'local/unified');timer=results(E/'local/timer-regression');a3=[v for v in results(E/'s50/export/attempts') if v['label'].startswith('latency-')];batches=[v for p in (E/'s50/export/batches-v2').glob('*.json') if (v:=read(p)) and 'makespan_ns' in v]
 data=dict(updated=datetime.datetime.now().astimezone().isoformat(),local_stages_executed=local_done,remote_stages_executed=remote_done,hip_boundary_gate=hip,cuda_gate_status=cuda.get('status') if cuda else None,local_export_verification=archive,counts=dict(profile_clean_count=sum(v['variant']=='clean' for v in profiles),profile_count=sum(v['variant']=='profile' for v in profiles),strict_count=len(strict),strict_timeouts=sum(v['timeout'] for v in strict),growth_count=len(growth),unified_count=len(unified),unified_status=dict(Counter(v['final_status'] for v in unified)),timer_count=len(timer),timer_status=dict(Counter(v['final_status'] for v in timer)),a3_attempts=len(a3),a3_accepted=sum(bool(v['accepted']) for v in a3),completed_batches=len(batches),accepted_batches=sum(bool(v['all_accepted']) for v in batches)),published=False,main_merged=False)
 services=subprocess.run(['systemctl','--user','show','twins-evidence-local-followthrough','twins-evidence-hip-candidate','twins-evidence-sync','twins-evidence-publish-local','-p','Id','-p','ActiveState','-p','SubState','-p','Result'],capture_output=True,text=True,timeout=10);data['local_services']=services.stdout
 remote_end=read(E/'s50/export/all-finished.json',{});final_index_current=bool(remote_end) and (archive.get('remote_index_utc_ns') or 0)>=remote_end['utc_ns']
 finished=local_done and remote_done and hip is not None and cuda is not None and archive.get('complete',False) and final_index_current;data['scheduled_execution_finished']=finished
 lines=['# 本轮执行总状态','',data['updated'],'','这是持续更新的执行清单；“阶段执行结束”不等于全部结果通过，release/tag/匿名发布仍未完成。','',f'- 本机各阶段执行结束：{local_done}；s50 各阶段执行结束：{remote_done}。',f'- A2 clean/profile 已完成：{data["counts"]["profile_clean_count"]}/{data["counts"]["profile_count"]}；strict 已完成 {len(strict)}，一小时删失 {sum(v["timeout"] for v in strict)}。',f'- 增长曲线尝试 {len(growth)}；统一逐配置结果 {len(unified)}；timer 广泛回归 {len(timer)}。',f'- A3 正式重复已完成 {len(a3)}，输出/flags 通过 {sum(bool(v["accepted"]) for v in a3)}。',f'- 真实吞吐批次已完成 {len(batches)}/18，通过 {sum(bool(v["all_accepted"]) for v in batches)}。',f'- 后续边界候选 HIP 门禁：{hip.get("status") if hip else "等待本机队列"}；CUDA 门禁：{cuda.get("status") if cuda else "等待延迟交接"}。',f'- 远端已完成 export 的本机哈希检查：{archive.get("verified_files",0)}/{archive.get("indexed_files",0)}，complete={archive.get("complete",False)}。','','逐点时间/校验及解释见 [实验结果](experiment_results_20261116.md)、[A2 表](a2_cost_results_20261116.md)、[本机实时页](experiment_results_local_live_20261116.md)、[s50 实时页](experiment_results_live_20261116.md)。所有重要结果保存在本机，未拉回普通大 trace。','','本机后台服务：twins-evidence-local-followthrough、twins-evidence-hip-candidate、twins-evidence-sync、twins-evidence-publish-local、twins-evidence-status。s50：twins-evidence-parallel-latency → twins-evidence-batches（资源池并行调度，旧串行服务在当前测量结束后退出）；另有 raw-cleanup/归档索引服务。','',f'本轮排程执行全部结束（含候选门禁收到结果及归档校验）：{finished}。此项不代替逐个科学结果验收，也不自动合并 main 或发布。']
 lines+=['','```text',services.stdout,'```']
 text='\n'.join(lines)+'\n';(E/'reports/execution-status.json').write_text(json.dumps(data,indent=2)+'\n');(E/'reports/execution-status.md').write_text(text);(MAT/'experiment_execution_status_20261116.md').write_text(text)
 if finished:break
 time.sleep(30)
