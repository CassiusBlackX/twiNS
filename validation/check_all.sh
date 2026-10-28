#!/bin/bash
# Every validated set against the stored ns-3 outputs; prints mismatches and a total.
# usage: validation/check_all.sh [--no-sweeps]
cd "$(dirname "$0")/.."
T="dctcp newreno cubic"
K8="k8_paper k8_fullsync k8_fullexp k8_a2a30 k8_incast16 k8_a2a50s"
{
    validation/check_exact.sh "$K8 ls_paper ls_a2a30 ls_incast16 ls_a2a50s k16_paper k16_a2a30 k16_incast16 k16_a2a50s" "$T"
    for v in jit jit1 jit2 jit3 jit4; do validation/check_exact.sh "$K8" "$T" $v; done
    # the general kernel on runs the lean one would take (both must agree with ns-3)
    validation/check_exact.sh "k8_paper k8_a2a30 k8_a2a50s ls_a2a30 k16_a2a30" "$T" base --general | sed 's/: /, general kernel: /'
    # flow sizes that are not MSS multiples
    validation/check_exact.sh "k8_a2a30b k8_a2a50b k8_incastb ls_a2a30b k16_a2a30b" "$T"
    # persistent connections carrying several flows; with and without FIN
    R="k8_r30 k8_r50 k8_r40p1 k8_rpc ls_r30 k16_r30"
    validation/check_exact.sh "$R" "$T"
    validation/check_exact.sh "$R k8_a2a30 k8_incast16 k8_a2a50s k8_a2a30b" "$T" fin
    # per-packet ECMP (spraying)
    validation/check_exact.sh "k8_paper k8_a2a30 k8_incast16 k8_a2a50s k8_a2a30b ls_a2a30 k16_a2a30" "$T" spray
    validation/check_exact.sh "k8_r30" "$T" sprayfin
    # topology files (Jellyfish, torus, Dragonfly), and link failures; per-flow ECMP and spraying
    G="jf32_a2a30 jf32_a2a50 jf32_incast torus6_a2a30 torus6_a2a50 df4x9_a2a30 df4x9_a2a50 jf256_a2a30"
    G="$G ft8f_a2a30 ft8f_a2a50s jf32f_a2a50 torus6f_a2a30"
    validation/check_exact.sh "$G" "$T"
    validation/check_exact.sh "$G" "$T" spray
} | awk '{for (i = 1; i < NF; i++) if ($(i + 1) == "flows") split($i, a, "/"); if (a[1] != a[2]) print "MISMATCH", $0; else n++} END {print n " runs identical"}'
[ "$1" = --no-sweeps ] || validation/check_sweeps.sh
