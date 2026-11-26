"""Accept short-IRN traces through the common last-flow completion."""
import argparse
import json
from pathlib import Path

p=argparse.ArgumentParser(description=__doc__)
p.add_argument("results",type=Path)
a=p.parse_args()
root=a.results
report={}
for stem,gold,lows in (("irn","irn-stock",(100000,500,800,900)),
                      ("irn-collision","irn-collision-native",(833,834,835))):
    for low in lows:
        n=root/gold/str(low)
        expected=sorted(tuple(map(int,row.split())) for row in (n/"reference.fct").open())
        limit=max(row[5]+row[6] for row in expected)
        def events(path):
            output=[]
            for line in path.open():
                fields=line.split()
                if int(fields[0])<=limit:
                    output.append((tuple(map(int,fields[:3])),tuple(fields[3:])))
            return sorted(output,key=lambda row:row[0])
        reference=events(n/"wire.raw")
        receipts={}
        for side in ("hip","cuda"):
            d=root/f"{stem}-{side}"/str(low)
            flow=sorted(tuple(map(int,row.split()))[1:] for row in (d/"flows.fct").open())
            result=events(d/"wire.raw")
            receipts[side]=dict(matched_fct=flow==sorted(x[5:7] for x in expected),
                                matched_wire=reference==result,events=len(result),rc=int((d/"rc").read_text()))
            assert all((receipts[side]["matched_fct"],receipts[side]["matched_wire"],receipts[side]["rc"]==0)), (stem,low,side)
        row=dict(limit_ns=limit,backends=receipts)
        if stem=="irn-collision":
            snd=[key[0] for key,cols in reference if key[1:]==(0,1) and cols[0]=="D"]
            ack=next(key[0]+1004 for key,cols in reference if key[1:]==(128,1) and cols[:3]==("N","2","0"))
            row.update(ack_ns=ack,last_rto_ns=snd[-1]+low,data_tx_ns=snd)
            assert (snd[-1] < ack) and (low!=834 or snd[-1]+low==ack)
        report[f"{stem}/{low}"]=row
    print(stem,"matched",len(lows),"variants",flush=True)
(root/"irn-acceptance.json").write_text(json.dumps(report,indent=2)+"\n")
