#!/bin/bash
# run a command in the simai-build container: /simai = SIMAI_DIR, /etc/astra-sim = ETC_DIR, /work = WORK
SIMAI_DIR=${SIMAI_DIR:-/home/cassius/netsim/simai}
ETC_DIR=${ETC_DIR:-/home/cassius/netsim/simai-etc}
WORK=${WORK:-/home/cassius/netsim/aiwork}
mkdir -p $ETC_DIR $WORK
exec podman --root /home/cassius/netsim/podman/root --runroot /home/cassius/netsim/podman/run run --rm -v $SIMAI_DIR:/simai:Z -v $ETC_DIR:/etc/astra-sim:Z -v $WORK:/work:Z -w /simai simai-build bash -c "$*"
