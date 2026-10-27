#!/bin/bash
# Every validated set against the stored ns-3 outputs; prints mismatches and a total.
# usage: validation/check_all.sh [--no-sweeps]
cd "$(dirname "$0")/.."
T="dctcp newreno cubic"
K8="k8_paper k8_fullsync k8_fullexp k8_a2a30 k8_incast16 k8_a2a50s"
{
    validation/check_exact.sh "$K8 ls_paper ls_a2a30 ls_incast16 ls_a2a50s k16_paper k16_a2a30 k16_incast16 k16_a2a50s" "$T"
    for v in jit jit1 jit2 jit3 jit4; do validation/check_exact.sh "$K8" "$T" $v; done
    # flow sizes that are not MSS multiples
    validation/check_exact.sh "k8_a2a30b k8_a2a50b k8_incastb ls_a2a30b k16_a2a30b" "$T"
} | awk '{split($4, a, "/"); if (a[1] != a[2]) print "MISMATCH", $0; else n++} END {print n " runs identical"}'
[ "$1" = --no-sweeps ] || validation/check_sweeps.sh
