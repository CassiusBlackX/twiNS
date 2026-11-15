#!/bin/bash
set -eu
cd "$(dirname "$0")/../.."
R=/home/cassius/Work/SpecDES/twiNS/out/paper-evidence-20261116
while [ ! -f "$R/local/all-finished.json" ]; do sleep 15; done
# Independent candidate gate; never overwrite the frozen48e0 measurement binaries.
cmake -S . -B build-boundary-candidate -DTWINS_GPU_BACKEND=HIP -DCMAKE_HIP_COMPILER=/opt/rocm/llvm/bin/clang++ -DCMAKE_PREFIX_PATH=/opt/rocm -DTWINS_BUILD_EVIDENCE_TOOLS=ON >"$R/receipts/hip-boundary-configure.log" 2>&1
cmake --build build-boundary-candidate --target twins-rdma twins-ai twins-astra-stub twins-island twins-lineage-bench -j2 >"$R/receipts/hip-boundary-build.log" 2>&1
build-boundary-candidate/twins-lineage-bench --boundary-only >"$R/receipts/hip-boundary-fixture.jsonl" 2>&1
python3 experiments/paper_evidence/isolated_micro.py
python3 - <<'PY'
import hashlib,json,time
from pathlib import Path
r=Path('/home/cassius/Work/SpecDES/twiNS/out/paper-evidence-20261116')
v=dict(utc_ns=time.time_ns(),status='boundary_fixture_passed',core_manifest='manifests/candidate-boundary.json',binary_sha256=hashlib.sha256(Path('build-boundary-candidate/twins-lineage-bench').read_bytes()).hexdigest(),scope='clean HIP compile and saturated count/prefix checks; does not relabel frozen48e0 performance evidence')
(r/'receipts/hip-boundary-result.json').write_text(json.dumps(v,indent=2))
PY
