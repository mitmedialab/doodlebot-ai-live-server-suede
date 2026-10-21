#!/usr/bin/env bash
# .suede/core/sync.sh — update every piece of suede machinery vendored into
# this repository, in one command, from `main`.
#
#   bash .suede/core/sync.sh [<path> ...]
#
# It finds them rather than being told: every subrepo whose remote is the suede
# library itself. In a fully initialized dependency that is four things.
#
#   .suede/core                 the maintainer's tools (this folder)
#   release/.suede/core         the tools that ship to consumers
#   .github/workflows           main's workflows
#   release/.github/workflows   the release branch's workflows
#
# Never ./release, which tracks this repository's own release branch: that one
# is published by push-release.sh, not pulled.
#
# Two of the four have a `.gitrepo` whose `parent` is meaningless here, for two
# different reasons, and both make `git subrepo pull` refuse:
#
#   * The workflow subrepos were cloned into the *template* this repository was
#     created from. A repository made from a template starts a fresh history,
#     so their parent names a commit that does not exist here at all. (They are
#     cloned into the template rather than at init because an Action is
#     restricted in what it may do to .github/workflows.)
#   * A `.suede/core` vendored onto the `release` branch before the layout
#     changed has a parent that does exist but is a release-branch commit, and
#     so is not an ancestor of `main`.
#
# Either way the fix is the same shape - point `parent` at a commit that IS in
# this history - so this repairs it and retries rather than making you read the
# failure. git-subrepo names the right commit when it can; when it cannot (the
# template case, where it suggests an empty SHA) the last commit that touched
# the subrepo is the honest answer, because that is the state on disk.
#
# Inputs (env):
#   RELEASE_DIR         default: release
#   SUEDE_LIBRARY_URL   default: https://github.com/pmalacho-mit/suede.git

set -euo pipefail

usage() { grep '^#' "$0" | grep -v '^#!/' | sed 's/^# \?//'; exit 0; }
[[ "${1-}" == "-h" || "${1-}" == "--help" ]] && usage

RELEASE_DIR="${RELEASE_DIR:-release}"
LIBRARY_URL="${SUEDE_LIBRARY_URL:-https://github.com/pmalacho-mit/suede.git}"

die() { printf 'sync: %s\n' "$*" >&2; exit 1; }
say() { printf 'sync: %s\n' "$*" >&2; }

command -v git >/dev/null 2>&1 || die "git not found"

# An install can be present without being reachable - a devcontainer feature or
# a login shell profile a non-interactive script never sourced. This is the
# marker it leaves behind.
ensure_git_subrepo() {
  git subrepo --version >/dev/null 2>&1 && return 0
  [[ -n "${GIT_SUBREPO_ROOT-}" && -f "${GIT_SUBREPO_ROOT-}/.rc" ]] || return 1
  set +eu
  # shellcheck disable=SC1091
  source "$GIT_SUBREPO_ROOT/.rc"
  set -eu
  git subrepo --version >/dev/null 2>&1
}

ensure_git_subrepo \
  || die "git-subrepo is not installed (https://github.com/ingydotnet/git-subrepo)"

cd "$(git rev-parse --show-toplevel)" || die "not inside a git repository"
git diff --quiet --no-ext-diff && git diff --cached --quiet --no-ext-diff \
  || die "you have uncommitted changes - git subrepo pull refuses a dirty tree"

# One spelling for one repository, so ssh and https forms of the library - and
# the local paths the tests use - all compare equal.
identity() { # <url>
  local url="${1%/}"
  url="${url%.git}"
  url="${url#*://}"
  url="${url#*@}"
  printf '%s' "${url/:/\/}"
}

readonly LIBRARY_ID="$(identity "$LIBRARY_URL")"

remote_of() { git config -f "$1/.gitrepo" --get subrepo.remote 2>/dev/null || true; }

# The four paths suede owns in a repository of its own, named rather than
# searched for.
#
# Searching is wrong here, and quietly so: every *installed dependency* ships a
# `.suede/core` and a `.github/workflows` of its own, each a subrepo of this
# same library. A scan finds those too, and pulling one edits a vendored
# dependency - which then no longer matches the commit its `.gitrepo` names, so
# `diff.sh` calls the pointer dishonest and `push-release.sh` refuses to
# publish. What suede owns here is a fixed, short list.
library_subrepos() {
  local directory
  for directory in \
    ".suede/core" \
    ".github/workflows" \
    "$RELEASE_DIR/.suede/core" \
    "$RELEASE_DIR/.github/workflows"
  do
    [[ -f "$directory/.gitrepo" ]] || continue
    # Not discovery - a guard. A `.github/workflows` someone vendored from
    # somewhere else is theirs, and this leaves it alone.
    [[ "$(identity "$(remote_of "$directory")")" == "$LIBRARY_ID" ]] || {
      say "$directory: not vendored from $LIBRARY_URL - skipping"
      continue
    }
    printf '%s\n' "$directory"
  done
}

# git-subrepo's recommendation, when its refusal carries one. The template case
# suggests an empty SHA, which is how "it does not know either" reads.
recommended_parent() { # <output>
  printf '%s' "$1" | grep -oE "to '[0-9a-f]{7,40}'" | grep -oE '[0-9a-f]{7,40}' | head -1
}

touched_this_subrepo() { # <path> <commit>
  git log --format=%H -- "$1" | grep -qx "$2"
}

repair_parent() { # <path> <pull output> -> 0 if it repaired something
  local path="$1" parent current
  parent="$(recommended_parent "$2")"
  # git-subrepo's suggestion is worth having only when it names a commit that
  # actually touched this subrepo. It does not always: point the parent at a
  # commit from elsewhere in the history and the merge that follows shares no
  # commits with upstream, which is `refusing to merge unrelated histories`.
  if [[ -z "$parent" ]] || ! touched_this_subrepo "$path" "$parent"; then
    # The *oldest* commit that touched the path - where this subrepo's content
    # entered the history, and so the last point at which it equalled the
    # upstream commit `.gitrepo` records. Anything later is a local change
    # rather than a sync point, and using one as the base is precisely what
    # produces unrelated histories. Older than necessary is safe: git-subrepo
    # replays more, and the local changes since then merge as they should.
    parent="$(git log --reverse --format=%H -- "$path" | head -1)"
  fi
  [[ -n "$parent" ]] || return 1
  current="$(git config -f "$path/.gitrepo" --get subrepo.parent 2>/dev/null || true)"
  # Nothing left to try: repointing where it already points would spin.
  [[ "$parent" != "$current" ]] || return 1
  say "$path: its recorded parent is not in this history - repointing at ${parent:0:7}"
  git config -f "$path/.gitrepo" subrepo.parent "$parent"
  git add "$path/.gitrepo"
  git commit --quiet -m "suede: repoint $path at a parent in this history"
}

# A pull that conflicts only because this repository deleted a file upstream
# has since changed. `initialize.yml` is the case this exists for: every
# initialized repository deletes it - the workflow's last act is to remove
# itself - and it is still in the branch being pulled, so any change to it
# upstream lands here as `deleted by us / modified by them`, forever.
#
# Keeping the deletion is the only answer that respects what the repository
# did on purpose. Anything else conflicting is a real disagreement about
# content and is left alone.
#
# The `--force` on the last line is not bravado. git-subrepo looks for its own
# worktree by the *unencoded* path (`subrepo/.github/workflows`) after having
# created it under the encoded one (`subrepo/%2egithub/workflows`), so for any
# path whose first segment starts with a dot - which is every path suede
# vendors - `git subrepo commit` cannot find it, and the resolution workflow
# git-subrepo itself prints cannot be completed. `--force` skips that lookup.
conflicted() { # <pull output>
  # Not the sentence naming the worktree: git-subrepo wraps it across two
  # lines ("...has been\ncreated at <path>..."), so it matches nothing.
  printf '%s' "$1" | grep -q 'finish the pull by hand'
}

resolve_deletions() { # <path> <pull output>
  local path="$1" worktree unmerged file
  # The numbered instructions git-subrepo prints put the path on a line of its
  # own, which is the only place it appears unwrapped.
  worktree="$(printf '%s' "$2" | sed -n 's#^ *1\. cd \(.*\)$#\1#p' | head -1)"
  [[ -n "$worktree" && -d "$worktree" ]] || return 1
  unmerged="$(git -C "$worktree" diff --name-only --diff-filter=U)"
  [[ -n "$unmerged" ]] || return 1

  # Every conflict, or none: a partial resolution is worse than a refusal.
  while IFS= read -r file; do
    [[ -n "$file" ]] || continue
    [[ "$(git -C "$worktree" status --porcelain -- "$file")" == DU* ]] || return 1
  done <<< "$unmerged"

  while IFS= read -r file; do
    [[ -n "$file" ]] || continue
    git -C "$worktree" rm -q -- "$file"
    say "$path: keeping your deletion of $file, which upstream has changed"
  done <<< "$unmerged"
  git -C "$worktree" commit --quiet -m "suede: keep local deletions while pulling $path"
  git subrepo commit "$path" --force >/dev/null
}

PULLED=()
FAILED=()

refuse() { # <path> <output> <why>
  FAILED+=("$1")
  say "$1: $3"
  printf '%s\n' "$2" >&2
}

# The two failures are independent and can arrive one after the other: a
# repaired parent lets the pull get far enough to reach a merge, which is where
# the deleted-file conflict lives. So this retries rather than handling one
# failure and giving up.
pull_one() { # <path>
  local path="$1" output status attempt
  for attempt in 1 2 3; do
    status=0
    output="$(git subrepo pull "$path" 2>&1)" || status=$?
    if [[ "$status" == 0 ]]; then
      PULLED+=("$path")
      if printf '%s' "$output" | grep -q 'is up to date'; then
        say "$path: already up to date"
      else
        say "$path: updated"
      fi
      return 0
    fi
    if conflicted "$output"; then
      if resolve_deletions "$path" "$output"; then
        PULLED+=("$path"); say "$path: updated"; return 0
      fi
      refuse "$path" "$output" \
        "conflicts beyond your own deletions - resolve them in the worktree above, then: git subrepo commit $path --force"
      return 0
    fi
    repair_parent "$path" "$output" || { refuse "$path" "$output" "FAILED"; return 0; }
  done
  refuse "$path" "$output" "FAILED"
}

# Pulling a subrepo nested inside ./release leaves a `subrepo/<path>` branch and
# a .git/tmp/subrepo/<path> scratch directory behind. Refs are directories, so
# the branch makes `subrepo/release` uncreatable; the scratch directory sits
# where the push wants its worktree. Either stops the next `git subrepo push
# release`, which is the very next thing publishing does.
clear_nested_leftovers() {
  local path nested=0
  for path in ${PULLED[@]+"${PULLED[@]}"}; do
    [[ "$path" == "$RELEASE_DIR/"* ]] || continue
    git subrepo clean "$path" >/dev/null 2>&1 || true
    nested=1
  done
  # The scratch directory goes whatever was pulled: it is git-subrepo's, it is
  # never read across runs, and a leftover under `.git/tmp/subrepo/release` is
  # what stops the next publish. Only the branch removal above is conditional,
  # because `git subrepo clean` on a path is meaningless without one.
  rm -rf .git/tmp/subrepo
  git worktree prune >/dev/null 2>&1 || true
  [[ "$nested" == 1 ]] || return 0
  say "cleared the git-subrepo leftovers of the subrepos under $RELEASE_DIR/"
}

TARGETS=()
if [[ $# -gt 0 ]]; then
  TARGETS=("$@")
else
  while IFS= read -r found; do TARGETS+=("$found"); done < <(library_subrepos)
fi

[[ ${#TARGETS[@]} -gt 0 ]] || die "found no subrepos of $LIBRARY_URL in this repository"

for target in "${TARGETS[@]}"; do
  [[ -f "$target/.gitrepo" ]] || die "$target is not a subrepo (no .gitrepo)"
  pull_one "$target"
done

clear_nested_leftovers

[[ ${#FAILED[@]} -eq 0 ]] || die "could not update: ${FAILED[*]}"
say "commit is already made by git-subrepo; push main to publish anything under $RELEASE_DIR/"
