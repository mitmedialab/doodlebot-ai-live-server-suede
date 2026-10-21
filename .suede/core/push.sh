#!/usr/bin/env bash
#
# Push, then follow the publish the push starts.
#
#   bash .suede/core/push.sh                  # git push
#   bash .suede/core/push.sh origin release   # any git push arguments
#
# Exactly `git push "$@" && bash .suede/core/check-release.sh`: if the push
# fails nothing else runs, and the exit code is git's. See check-release.sh for
# what is checked and what each exit code means.

set -euo pipefail

usage() { grep '^#' "$0" | grep -v '^#!/' | sed 's/^# \?//'; exit 0; }
[[ "${1-}" == "-h" || "${1-}" == "--help" ]] && usage

CORE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
git push "$@"
exec bash "$CORE_DIR/check-release.sh"
