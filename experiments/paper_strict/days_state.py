"""Read the original Days terminal state through Scalar and verify it against CUDA."""
from collections import Counter
import csv
import json
from pathlib import Path
import re
import subprocess
import sys

HOME=Path.home()/'Videos'
BASE=HOME/'twins-paper-strict-20261123'
CODE=r'''
use days::scenario::compile_config;
use days_executor::{run_scalar_with_observations, FlowGeneratorKind, ObservationMode};
use std::env;
fn main() {
    let input = env::args().nth(1).expect("frozen input");
    let image=compile_config(input).expect("compile");
    let result=run_scalar_with_observations(&image,None,ObservationMode::Summary).expect("execute");
    let mut digest=14695981039346656037u64;
    for byte in format!("{result:#?}").bytes() {
        digest=(digest ^ byte as u64).wrapping_mul(1099511628211);
    }
    eprintln!("result_fnv1a64={digest:016x} pending={} resident={} summary={:?}",
        result.pending_events.len(),result.resident_packets.len(),result.summary);
    for event in &result.pending_events {
        eprintln!("pending_event {:?}",event);
    }
    println!("flow,demand_bytes,acked_bytes,emitted_bytes,recovery_high_sequence,has_timer");
    for host in &result.host_states {
        for state in &host.generators {
            let FlowGeneratorKind::Tcp(tcp)=state.kind else { panic!("unexpected generator"); };
            println!("{},{},{},{},{},{}",state.flow.0,tcp.total_bytes,tcp.highest_ack,
                state.bytes_emitted,tcp.recovery_high_sequence,tcp.active_timer.is_some());
        }
    }
}
'''

def main():
    out=Path(sys.argv[1])
    out.mkdir(parents=True)
    code=out/'observer.rs'
    code.write_text(CODE)
    exe=out/'observer'
    deps=HOME/'days/target/release/deps'
    rust=HOME/'twins-days-probe-20261122/toolchain/bin/rustc'
    command=[rust,'--edition=2021','-O',code,'-o',exe,'-L',f'dependency={deps}',
             '--extern',f'days={deps}/libdays-27a621a024f738d1.rlib',
             '--extern',f'days_executor={deps}/libdays_executor-f1fbcc826555389a.rlib']
    with (out/'build.log').open('wb') as stream:
        subprocess.run(command,stdout=stream,stderr=subprocess.STDOUT,check=True)
    data={}
    for label in ('crosspod','crosspod_paced','paced_probe'):
        root=sorted((BASE/label).glob('20*'))[-1]/'results'/('preflight' if label=='paced_probe' else 'measurements')
        for case in sorted(root.glob('k*')):
            logs=list(case.glob('trial-probe/days/process.log' if label=='paced_probe' else 'trial-?/days/process.log'))
            if len(logs)!=(1 if label=='paced_probe' else 3):
                continue
            hashes=[re.findall(r'result_fnv1a64=(\w+)',p.read_text()) for p in logs]
            d=out/f'{label}-{case.name}'
            d.mkdir()
            with (d/'observer.log').open('wb') as log,(d/'flow-state.csv').open('wb') as csv_file:
                config=case/'days.toml'
                if label=='paced_probe':
                    config=sorted((BASE/'crosspod_paced').glob('20*'))[-1]/'results/inputs'/case.name/'days.toml'
                rc=subprocess.run(['taskset','-c','3',exe,config],
                                  stdout=csv_file,stderr=log,timeout=600).returncode
            native=(d/'observer.log').read_text()
            result=re.findall(r'result_fnv1a64=(\w+)',native)
            rows=list(csv.DictReader((d/'flow-state.csv').open()))
            counters=Counter(line.split('kind: ',1)[-1].split(',')[0] for line in native.splitlines()
                             if 'pending_event' in line and 'kind: ' in line)
            complete=sum(row['demand_bytes']==row['acked_bytes'] for row in rows)
            data[d.name]=dict(rc=rc,scalar_hash=result,cuda_hashes=hashes,
                state_digest_equal=bool(result) and all(x==result for x in hashes),
                complete=complete,total=len(rows),pending_kind=dict(counters),
                first_incomplete=[row for row in rows if row['demand_bytes']!=row['acked_bytes']][:4])
            print(d.name,data[d.name]['state_digest_equal'],complete,len(rows),flush=True)
    (out/'summary.json').write_text(json.dumps(data,indent=2)+'\n')

if __name__=='__main__':
    main()
