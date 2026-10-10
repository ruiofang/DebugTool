#!/bin/sh
# All interfaces and sysfs mounts exist only inside the new namespaces.
set -eu
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_dir"
exec unshare --user --map-root-user --net --mount sh -eu -c '
    mount -t sysfs -o ro,nosuid,nodev,noexec sysfs /sys
    ip link add dev vcan_test type vcan
    ip link set dev vcan_test up
    export QT_QPA_PLATFORM=offscreen DEBUGTOOL_VCAN_INTERFACE=vcan_test
    exec "$1" -m unittest qa.test_socketcan_vcan -v
' sh "$project_dir/.venv/bin/python"
