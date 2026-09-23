#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
case ${1:---plan} in
  --help|-h) echo 'Usage: scripts/install-host.sh [--plan|--apply]'; exit 0 ;;
  --plan) apply=0 ;;
  --apply) apply=1 ;;
  *) echo 'Use --plan or --apply' >&2; exit 2 ;;
esac
(( $# <= 1 )) || exit 2
cmd=(sudo python3 "$root/hyperalloc-6.18/scripts/kernel-deploy.py" install --role host --system --update-grub)
printf '%q ' "${cmd[@]}"; printf '\n'
if (( apply )); then "${cmd[@]}"; fi
printf '%s\n' 'Select 6.18.0-chameleon-host in GRUB at your next reboot.' \
  'Use intel_iommu=on iommu=pt in the Host kernel command line. This script does not reboot.'
