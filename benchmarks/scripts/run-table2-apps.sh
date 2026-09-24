#!/usr/bin/env bash
set -euo pipefail
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
# Compatibility entry for the now-default Table 2 workload and VM profile.
exec python3 "$here/run-sized-chameleon-apps.py" \
    --profile table2 "$@"
