#!/usr/bin/env bash
#
# How every installed dependency stands against its remote.
#
#   bash .suede/core/diff.sh                  # everything, for you
#   bash .suede/core/diff.sh --shipped-only   # what the publish guard checks
#   bash .suede/core/diff.sh --https          # skip SSH: no key here
#
# By default it checks release, transitive and development dependencies alike,
# for two things:
#
#   local changes   your files differ from the commit the .gitrepo pins - what
#                   `upstream` would propose, or what you would lose on a
#                   reinstall
#   behind          the remote's release branch has moved past that commit -
#                   what `sync` would bring you
#
# --shipped-only narrows it to what this repository ships a pointer to (release
# dependencies, and every install their edges reach) and to local changes only.
# That is the publish guard: a pointer must name the code you built against,
# while a development dependency ships nothing and being behind is never a
# reason to refuse a release.
#
# Vendored dependencies are never checked: one exists precisely *because* it
# diverges, and it ships as source. Each comparison uses the `diff` that ships
# in release/.suede/core, so this and a consumer's view cannot disagree.
#
# --https reaches every remote over HTTPS only, skipping the SSH attempt.
#
# Exit 0 when nothing checked has local changes, 1 when something does, 2 when
# a comparison could not run. Being behind is reported, never an exit code.

set -euo pipefail
LIB_PREFIX="diff"
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

usage() { grep '^#' "$0" | grep -v '^#!/' | sed 's/^# \?//'; exit 0; }

SHIPPED_ONLY=0
HTTPS_ONLY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)      usage ;;
    --shipped-only) SHIPPED_ONLY=1; shift ;;
    --https)        HTTPS_ONLY=1; shift ;;
    *)              lib_die "unknown argument: $1 (see --help)" ;;
  esac
done
export HTTPS_ONLY

lib_enter_root
DIFF="$(release_tool diff)"
DIFF_ARGS=()
[[ "$HTTPS_ONLY" == 1 ]] && DIFF_ARGS=(--https)

label() { # <kind> <entry> <real>
  if [[ "$2" == "-" ]]; then printf '%s (%s)' "$3" "$1"; else printf '%s (%s, %s)' "$2" "$3" "$1"; fi
}

CHANGED=0; FAILED=0; BEHIND=0; COUNT=0; SHIPPED_CHANGED=0
while IFS=$'\t' read -r kind entry real; do
  [[ -n "$real" ]] || continue
  case "$kind" in
    vendored) continue ;;
    development) [[ "$SHIPPED_ONLY" == 1 ]] && continue ;;
  esac
  COUNT=$((COUNT + 1))
  name="$(label "$kind" "$entry" "$real")"
  pin="$(field "$real/.gitrepo" commit)"

  status=0
  bash "$DIFF" --in "$real" --quiet ${DIFF_ARGS[@]+"${DIFF_ARGS[@]}"} >/dev/null 2>&1 || status=$?
  case "$status" in
    0) ;;
    1) CHANGED=$((CHANGED + 1))
       [[ "$kind" != development ]] && SHIPPED_CHANGED=$((SHIPPED_CHANGED + 1))
       echo "$name has local changes against $(short "$pin")"
       bash "$DIFF" --in "$real" --stat ${DIFF_ARGS[@]+"${DIFF_ARGS[@]}"} 2>/dev/null | sed 's/^/    /' || true ;;
    *) FAILED=$((FAILED + 1))
       echo "$name: could not compare"
       bash "$DIFF" --in "$real" --quiet ${DIFF_ARGS[@]+"${DIFF_ARGS[@]}"} 2>&1 >/dev/null | sed 's/^/    /' || true ;;
  esac

  [[ "$SHIPPED_ONLY" == 1 ]] && continue
  if tip="$(remote_tip "$real")"; then
    if [[ "$tip" != "$pin" ]]; then
      BEHIND=$((BEHIND + 1))
      echo "$name is behind: pinned $(short "$pin"), its branch is at $(short "$tip")"
      echo "    bash $DIFF --in $real --sync    # what a sync would bring"
    fi
  else
    echo "$name: could not reach its remote to see whether it is behind"
  fi
done < <(installed_dependencies)

SCOPE="installed"; [[ "$SHIPPED_ONLY" == 1 ]] && SCOPE="shipped"
if [[ "$CHANGED" == 0 && "$FAILED" == 0 && "$BEHIND" == 0 ]]; then
  if [[ "$SHIPPED_ONLY" == 1 ]]; then
    lib_say "every $SCOPE dependency matches its pinned commit ($COUNT checked)"
  else
    lib_say "every $SCOPE dependency matches its pinned commit and is at its branch tip ($COUNT checked)"
  fi
  exit 0
fi

lib_say "$COUNT $SCOPE dependencies checked: $CHANGED with local changes, $FAILED not compared$( [[ "$SHIPPED_ONLY" == 0 ]] && printf ', %s behind' "$BEHIND" )"
if [[ "$SHIPPED_CHANGED" -gt 0 ]]; then
  cat <<'WHY'

A shipped dependency is published as a pointer, so the pointer has to be
honest. Either revert these changes, propose them upstream
(bash <dependency>/.suede/core/upstream), or vendor the dependency so the
source itself ships: `git mv <folder> release/<name>` and remove its
declaring symlink. The publish guard refuses until one of those is done.
WHY
fi
if [[ "$CHANGED" -gt "$SHIPPED_CHANGED" ]]; then
  echo
  echo "A development dependency's changes do not block a publish; upstream them when ready."
fi
[[ "$CHANGED" -gt 0 ]] && exit 1
[[ "$FAILED" -gt 0 ]] && exit 2
exit 0
