#!/usr/bin/env bash
#
# Write release/.suede/.dependencies/ - the list of release dependencies that
# ships with this dependency - from what the tree declares.
#
#   bash .suede/core/extract.sh
#
# One record per root symlink named <name><sep><repo> that resolves to an
# installed dependency outside release/: `<entry>.gitrepo`, holding the HTTPS
# remote, the branch and the commit. A consumer's deps.sh reads these and
# recreates each entry beside the installed copy of this dependency.
#
# Anything else in that folder is removed: a record for an entry that no
# longer exists, and the package.json / requirements.txt / separator files
# older versions generated. Third-party packages are the author's business
# now - give release/ its own package.json and treat it as a workspace.
#
# The publish flow runs this; run it yourself to see what would ship.

set -euo pipefail
LIB_PREFIX="extract"
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

usage() { grep '^#' "$0" | grep -v '^#!/' | sed 's/^# \?//'; exit 0; }
[[ "${1-}" == "-h" || "${1-}" == "--help" ]] && usage

lib_enter_root
[[ -d "$RELEASE_DIR" ]] || lib_die "no ./$RELEASE_DIR folder - nothing to publish"

DEST="$RELEASE_DIR/.suede/.dependencies"
mkdir -p "$DEST"

WANTED=""
while IFS=$'\t' read -r entry real; do
  [[ -n "$entry" ]] || continue
  record="$DEST/$entry.gitrepo"
  WANTED+="$entry.gitrepo"$'\n'
  {
    printf '; A suede dependency record, written by extract. Do not edit by hand.\n'
    printf '; deps.sh recreates ../%s beside this dependency wherever it is installed.\n' "$entry"
  } > "$record"
  git config -f "$record" subrepo.remote "$(https_spelling "$(field "$real/.gitrepo" remote)")"
  git config -f "$record" subrepo.branch "$(field "$real/.gitrepo" branch)"
  git config -f "$record" subrepo.commit "$(field "$real/.gitrepo" commit)"
  lib_say "$record  ($real @ $(short "$(field "$real/.gitrepo" commit)"))"
done < <(release_dependencies)

for existing in "$DEST"/* "$DEST"/.[!.]*; do
  [[ -e "$existing" ]] || continue
  name="$(basename "$existing")"
  grep -qxF -- "$name" <<<"$WANTED" && continue
  rm -rf "$existing"
  lib_say "removed $existing (no longer generated)"
done
rmdir "$DEST" 2>/dev/null && lib_say "no release dependencies; $DEST removed" || true
