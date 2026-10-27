#!/bin/bash
# Run a command in the hpcc-build container (Containerfile: Ubuntu 16.04, gcc 5.4, python 2.7),
# with the HPCC repository at /hpcc and the work directory at /work.
#   HPCC_DIR  the HPCC repository (default ~/netsim/hpcc)
#   WORK      the work directory: configs, topologies, flows, outputs (default ~/netsim/rdma)
#   PODMAN    podman with its storage options (default: storage under ~/netsim/podman)
HPCC_DIR=${HPCC_DIR:-$HOME/netsim/hpcc}
WORK=${WORK:-$HOME/netsim/rdma}
PODMAN=${PODMAN:-podman --root $HOME/netsim/podman/root --runroot $HOME/netsim/podman/run}
exec $PODMAN run --rm -v "$HPCC_DIR":/hpcc:Z -v "$WORK":/work:Z -w /hpcc/simulation hpcc-build bash -c "$*"
