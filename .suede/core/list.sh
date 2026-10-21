#!/usr/bin/env bash
#
# Every suede dependency in this repository, and what kind it is.
#
#   bash .suede/core/list.sh
#
#   KIND         ENTRY                      PATH                       PIN
#   release      widget.my-app              widget                     86abeeb
#   transitive   mixin.widget               mixin                      30142f6
#   development  -                          fixtures/harness           4f10c2a
#   vendored     -                          release/mixin              9bb0e41
#
# The kind is read off the tree, the same way extract reads it: an install
# inside release/ is vendored; one that a root symlink named <name><sep><repo>
# resolves to is a release dependency; one reached only through a release
# dependency's edges is transitive (ENTRY names the edge) - it ships through
# that dependency's record; anything else is development. suede's own vendored
# machinery (.suede/core, .github/workflows) is left out.

set -euo pipefail
LIB_PREFIX="list"
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

usage() { grep '^#' "$0" | grep -v '^#!/' | sed 's/^# \?//'; exit 0; }
[[ "${1-}" == "-h" || "${1-}" == "--help" ]] && usage

lib_enter_root

rows=""
while IFS=$'\t' read -r kind entry dir; do
  [[ -n "$dir" ]] || continue
  rows+="$(printf '%-12s %-26s %-26s %s' "$kind" "$entry" "$dir" "$(short "$(field "$dir/.gitrepo" commit)")")"$'\n'
done < <(installed_dependencies 2>/dev/null)

if [[ -z "$rows" ]]; then
  echo "no suede dependencies found"
  exit 0
fi
printf '%-12s %-26s %-26s %s\n' KIND ENTRY PATH PIN
printf '%s' "$rows"
