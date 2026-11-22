"""Separate emitted pass summaries from buffered warnings and process completion."""
import re

RDMA=re.compile(r'^flows done (\d+) of (\d+);',re.M)
FLAGS=re.compile(r'undecided ties (\d+), on lost tails (\d+).*?overflows: tx (\d+) queue (\d+) ackq (\d+) qps (\d+) due (\d+)')
TCP=re.compile(r'^flows (\d+) completed (\d+).*',re.M)

def status(log):
    rdma=list(RDMA.finditer(log))
    passes=[]
    if rdma:
        for i,match in enumerate(rdma):
            end=rdma[i+1].start() if i+1<len(rdma) else len(log)
            block=log[match.start():end]
            flags=FLAGS.search(block)
            values=list(map(int,flags.groups())) if flags else None
            passes.append(dict(index=i+1,completed=int(match[1]),total=int(match[2]),
                               counters=dict(zip(('ties','lost_tails','tx','queue','ackq','qps','due'),values)) if values is not None else None,
                               flagged=any(values) if values is not None else True))
    else:
        for i,match in enumerate(TCP.finditer(log)):
            fields={}
            for key in ('active-ovf','ooo-ovf','due-ovf','rtt-ovf','ring-ovf','undecided-ties','tx-cap'):
                found=re.search(r'\b'+re.escape(key)+r' (\d+)',match[0])
                if found:
                    fields[key]=int(found[1])
            passes.append(dict(index=i+1,completed=int(match[2]),total=int(match[1]),counters=fields,
                               flagged=any(fields.values())))
    warnings=[line for line in log.splitlines() if re.search(r'warning:|error:|running again with',line)]
    final=passes[-1] if passes else None
    return dict(passes=passes,final=final,warnings=warnings,
                flagged=final['flagged'] if final else bool(re.search(r'warning:|error:',log)),
                complete=final is not None and final['completed']==final['total'])
