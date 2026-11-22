"""Verify an actual lossless SACK/timestamps control, distinct from the incast pilot."""
import json
from pathlib import Path
import re
import sys
from tcp_reference_controls import execute

base=Path.home()/'Videos/twins-paper-strict-20261123/tcp_reference_controls'
binary=sorted(base.glob('20*'))[-1]/'results/gcc-no-contract/reference'
out=Path(sys.argv[1])
out.mkdir(parents=True)
flow=out/'input.csv'
flow.write_text('src,dst,flow_size,start_ns\n0,15,46720,1000\n')
rows=[]
for label,options in [('default',{}),('sack',{'TWINS_REF_SACK':'1'}),('timestamps',{'TWINS_REF_TIMESTAMPS':'1'})]:
    row,result=execute(out/label,binary,['--k=4',f'--flows={flow}','--tcp=ns3::TcpNewReno','--stop=0.1',
                                       '--hostq=red','--flowEcmp=1','--queue=200'],{**options,'TRACE_FLOW':'0'})
    summary=row['summary'][-1] if row['summary'] else ''
    if row['rc'] or row['completed']!=1 or not re.search(r'drops 0 marks 0 retx 0 rto 0',summary):
        raise ValueError('fixture is not a lossless completed control')
    rows.append(dict(arm=label,result=row,fct=result))
(out/'summary.json').write_text(json.dumps(rows,indent=2)+'\n')
