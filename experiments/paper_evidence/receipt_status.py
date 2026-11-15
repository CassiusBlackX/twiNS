"""Derived terminal-pass classification; raw receipts and complete logs stay unchanged."""
import re
def terminal_profile(x,path):
 logpath=path.parent/'process.log'
 if not logpath.exists():return x
 parts=re.split(r'^.*running again with[^\n]*\n',logpath.read_text(errors='replace'),flags=re.M)
 if len(parts)==1:return x
 x=dict(x);terminal=parts[-1];x['original_whole_log_classification']={k:x.get(k) for k in ('accepted','flagged','overflow')};x['internal_retries']=len(parts)-1
 x['flagged']=bool(re.search(r'were undecided|warning:.*(?:ties the chains|not exact|overflow)|error:',terminal));x['overflow']=bool(re.search(r'warning:.*(?:capacity overflow|a capacity overflowed)',terminal))
 x['accepted']=bool(x['rc']==0 and x.get('validation',{}).get('equal',x.get('equal',False)) and x.get('dag_equal',True) and not x['flagged'] and not x['overflow'])
 x['classification_scope']='terminal pass; wall and memory cover all retries; raw receipt retains original classifier'
 return x
