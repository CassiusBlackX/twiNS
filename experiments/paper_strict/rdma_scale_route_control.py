"""Compare stock and compact HPCC including packet order on a small contested topology."""

from pathlib import Path
import sys

from crosspod import ROOT
import rdma_scale_compact_reference as compact
from rdma_scale_reference import config,run_reference
from pfc_sustained import save


def signature(path,wire=False):
    rows=path.read_text().splitlines()
    if not wire:
        return sorted(rows)
    return sorted((r.split() for r in rows),key=lambda l:tuple(map(int,l[:3])))


def main():
    out=Path(sys.argv[1]).resolve()
    out.mkdir()
    compact.BUILD=next((p/'results/build' for p in sorted((ROOT/'rdma_scale_compact_reference').glob('20*'),reverse=True)
                        if (p/'results/build/build-provenance.json').is_file()))
    case=out/'input'
    config(case,8)
    rows=[]
    for kind in ('incast','distributed'):
        base=out/'stock'/kind
        alt=out/'compact'/kind
        a=run_reference(case,kind,base,core=8,memory='3g')
        b=compact.run(out/'compact',case,kind,core=8,memory='3g')
        assert a['rc']==b['rc']==0
        exact=signature(base/'output.fct')==signature(alt/'output.fct')
        x=signature(base/'selected-wire.log',True)
        y=signature(alt/'selected-wire.log',True)
        assert exact and x==y and x
        rows.append({'traffic':kind,'reference_exact':exact,'port_wire_equal':True,'wire_count':len(x),'stock':a,'compact':b})
        save(out/'summary.json',rows)


if __name__=='__main__':
    main()
