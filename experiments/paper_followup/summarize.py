"""Join returned evidence without changing frozen or per-run measurement records."""
from collections import defaultdict
import json
from pathlib import Path
from statistics import median
import sys

def summarize(rows):
    walls=[row['wall_ns']/1e9 for row in rows if row.get('accepted')]
    return dict(trials=len(rows),accepted=sum(row.get('accepted',False) for row in rows),
                median_s=median(walls) if walls else None,range_s=[min(walls),max(walls)] if walls else None)

def main():
    root=Path(sys.argv[1])
    groups=defaultdict(list)
    for role in ('k32_cpu','gpu_suite'):
        for epoch in (root/role).glob('20*'):
            if role=='k32_cpu':
                paths=epoch.glob('k32_*/trial-*/result.json')
            else:
                paths=(epoch/'measurements').glob('**/result.json')
            for p in paths:
                row=json.loads(p.read_text())
                if role=='k32_cpu':
                    key='rdma-cpu/'+row['case']
                else:
                    relative=p.relative_to(epoch/'measurements')
                    if relative.parts[0]=='rdma-k32':
                        key='rdma-gpu/'+row['case']
                    elif relative.parts[0]=='callback-horizon':
                        key='callback-horizon/'+row['arm']
                    else:
                        key='/'.join([relative.parts[0],row['case'],row.get('tcp',''),row['arm']])
                row['returned_receipt']=str(p)
                row['epoch']=epoch.name
                groups[key].append(row)
    tables={key:summarize(rows) for key,rows in groups.items()}
    speedups={}
    for key,cpu in tables.items():
        if key.startswith('rdma-cpu/'):
            case=key.removeprefix('rdma-cpu/')
            gpu=tables.get('rdma-gpu/'+case)
            if gpu and cpu['accepted']>=3 and gpu['accepted']>=3:
                speedups[case]=dict(cpu_median_s=cpu['median_s'],gpu_median_s=gpu['median_s'],
                                    speedup=cpu['median_s']/gpu['median_s'])
    result=dict(tables=tables,rdma_speedups=speedups,
                timing_scope='Process launch to exit, excluding output parsing, validation, and sorting. Resource epochs retained in per-run receipts.')
    out=root/'returned-summary.json'
    tmp=out.with_suffix('.tmp')
    tmp.write_text(json.dumps(result,indent=2)+'\n')
    tmp.replace(out)
    print(json.dumps(result,indent=2))

if __name__=='__main__':
    main()
