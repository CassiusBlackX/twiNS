#!/bin/bash
# build.sh [cpu|gpu|all]: the UB engines into build/
cd "$(dirname "$0")/../.."
mkdir -p build
W="${1:-all}"
F="-O3 -march=native -std=c++17 -Isrc -Wall -Wno-unused-function -Wno-unused-variable"
set -e
if [ "$W" != gpu ]; then
    g++ $F src/ub/seq.cc -o build/twins-ubseq
    g++ $F src/ub/par.cc -o build/twins-ubpar
    g++ $F -pthread src/ub/mt.cc -o build/twins-ubmt
fi
if [ "$W" != cpu ] && [ -x /opt/rocm/bin/hipcc ]; then
    /opt/rocm/bin/hipcc -O3 -std=c++17 --offload-arch=${UB_ARCH:-gfx1200} -ffp-contract=off -Wno-unused-value \
        -Wno-unused-result -Isrc src/ub/gpu.cu -o build/twins-ub
fi
