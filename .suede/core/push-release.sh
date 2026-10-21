#!/usr/bin/env bash
#
# The whole main-side release flow, so the workflow that calls it can stay a
# caller. Run on `main` after a change under release/ has landed.
#
#   extract -> guard -> commit the manifest -> sync out -> propagate
#
# The guard is the reason this is worth having in one place: a release
# dependency ships as a *pointer*, so before that pointer goes out we check it
# is honest (diff.sh: nothing diverged from its pinned commit) and that every
# declared dependency is actually in place (deps.sh --check, run against
# release/ so it walks the whole tree of siblings). A failure here stops the
# push and writes the reason into the job summary, rather than publishing a
# lie.
#
# Flags:
#   --https              reach every remote over HTTPS only (CI has no SSH key)
#
# Inputs (env):
#   RELEASE_DIR          default: release
#   DRY_RUN              set to 1 to stop before touching the remote
#   SUEDE_RELEASE_CORE   where diff and deps.sh are (default release/.suede/core)

set -euo pipefail
LIB_PREFIX="push-release"
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

DRY_RUN="${DRY_RUN:-0}"
CORE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

HTTPS_FLAG=()
for argument in "$@"; do
  case "$argument" in
    --https) HTTPS_FLAG=(--https) ;;
    *) lib_die "unknown argument: $argument" ;;
  esac
done

lib_enter_root
[[ -d "$RELEASE_DIR" ]] || lib_die "no ./$RELEASE_DIR folder - nothing to publish"

# Anything written here also lands in the GitHub job summary, so a maintainer
# reads the reason on the run page rather than in the log.
report() {
  printf '%s\n' "$*" >&2
  [[ -n "${GITHUB_STEP_SUMMARY:-}" ]] && printf '%s\n' "$*" >> "$GITHUB_STEP_SUMMARY"
  return 0
}

# The manifest is generated, never hand-edited, so regenerate it every run and
# commit only when it actually moved.
refresh_manifest() {
  bash "$CORE_DIR/extract.sh"
  git add -A "$RELEASE_DIR/.suede/.dependencies" 2>/dev/null || true
  # --no-ext-diff: a diff.external tool (difftastic, say) would print here.
  if git diff --cached --quiet --no-ext-diff -- "$RELEASE_DIR"; then
    lib_say "manifest unchanged"
    return 0
  fi
  git commit --quiet -m "chore(suede): update dependency records"
  lib_say "committed refreshed dependency records"
}

# Two different failures, two different fixes, so they are reported separately.
guard() {
  local failed=0 deps
  # Shipped only: a development dependency ships nothing, and being behind is
  # never a reason to refuse a release.
  if ! bash "$CORE_DIR/diff.sh" --shipped-only ${HTTPS_FLAG[@]+"${HTTPS_FLAG[@]}"} > "$WORKSPACE/diff.txt" 2>&1; then
    report "### suede: a release dependency has diverged from its pin"
    report ''
    report '```'
    report "$(cat "$WORKSPACE/diff.txt")"
    report '```'
    failed=1
  fi
  deps="$(release_tool deps.sh)"
  if ! bash "$deps" --check --in "$RELEASE_DIR" > "$WORKSPACE/deps.txt" 2>&1; then
    report "### suede: a declared dependency is not in place"
    report ''
    report '```'
    report "$(cat "$WORKSPACE/deps.txt")"
    report '```'
    failed=1
  fi
  return "$failed"
}

# Pulling a subrepo nested inside release/ - `.suede/core` is one in every
# dependency - leaves two things behind that stop the push below, in two
# different ways:
#
#   * a branch `subrepo/release/%2esuede/core`. Git refs are directories, so
#     while it exists `subrepo/release` cannot be created: "cannot lock ref".
#   * a scratch directory `.git/tmp/subrepo/release/%2esuede`. That leaves
#     `.git/tmp/subrepo/release` sitting there as an ordinary directory, and
#     the push wants exactly that path for its worktree: "this operation must
#     be run in a work tree".
#
# `git subrepo clean` clears the first and not the second. The pull that causes
# both happens on main, days earlier and by hand, which makes this the only
# place that can be relied on to tidy up after it.
release_nested_subrepos() {
  find "$RELEASE_DIR" -name .gitrepo -not -path "$RELEASE_DIR/.gitrepo" -not -path '*/.git/*' \
    | sed 's#/\.gitrepo$##' | sort
}

clear_nested_subrepo_refs() {
  local nested cleared=0
  while IFS= read -r nested; do
    [[ -n "$nested" ]] || continue
    git subrepo clean "$nested" >/dev/null 2>&1 || true
    cleared=1
  done < <(release_nested_subrepos)
  [[ "$cleared" == 1 ]] || return 0
  lib_say "cleared the git-subrepo leftovers of the subrepos nested in $RELEASE_DIR"
  rm -rf .git/tmp/subrepo
  git worktree prune >/dev/null 2>&1 || true
}

# Pull first so the push lands on top of the current release tip (a release
# that advanced by some other route); "nothing to pull" is not a failure.
sync_release_branch() {
  git subrepo pull "$RELEASE_DIR" || true
  clear_nested_subrepo_refs
  git subrepo push "$RELEASE_DIR"
  git push   # propagate the .gitrepo pointer bump back to main
}

WORKSPACE="$(mktemp -d)"
trap 'rm -rf "$WORKSPACE"' EXIT

refresh_manifest

if ! guard; then
  lib_say "refusing to publish - the release branch is unchanged"
  exit 1
fi

[[ "$DRY_RUN" == "1" ]] && { lib_say "dry run: stopping before the push"; exit 0; }
sync_release_branch
lib_say "published $RELEASE_DIR to the release branch"
