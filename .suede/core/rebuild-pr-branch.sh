#!/usr/bin/env bash
# rebuild-pr-branch.sh — the core git logic of suede-downstream-to-main.
#
# SINGLE SOURCE OF TRUTH: the action runs this file, and the test harness runs
# this same file. There is no copied logic and no runner emulation.
#
# Given a release-shaped submission branch, it: recovers the base release commit
# the consumer actually had, replays their change onto the latest release
# (conflicts -> markers), and transplants the result under release/ on top of
# main. It leaves the rebuilt content on the local branch $PR_HEAD_BRANCH for the
# caller to force-push.
#
# Inputs (env):
#   SUBMISSION_REF   required — the downstream/<...> branch name (== github.ref_name)
#   REMOTE           default: origin
#   MAIN_BRANCH      default: main
#   RELEASE_BRANCH   default: release
#   RELEASE_DIR      default: release
#   PR_HEAD_BRANCH   default: pull-request-head
#
# Outputs:
#   - appends base_commit / submission_commit / conflicted / has_changes to
#     $GITHUB_OUTPUT when that variable is set (i.e. inside Actions)
#   - always prints: RESULT base=<sha> conflicted=<bool> has_changes=<bool>
set -euo pipefail

die() { echo "::error::rebuild-pr-branch: $*" >&2; exit 1; }

REMOTE="${REMOTE:-origin}"
MAIN_BRANCH="${MAIN_BRANCH:-main}"
RELEASE_BRANCH="${RELEASE_BRANCH:-release}"
RELEASE_DIR="${RELEASE_DIR:-release}"
PR_HEAD_BRANCH="${PR_HEAD_BRANCH:-pull-request-head}"
SUBMISSION_REF="${SUBMISSION_REF:?SUBMISSION_REF (the downstream/... branch) is required}"

# Bring the three endpoints in as remote-tracking refs.
git fetch --quiet "$REMOTE" \
  "${MAIN_BRANCH}:refs/remotes/${REMOTE}/${MAIN_BRANCH}" \
  "${RELEASE_BRANCH}:refs/remotes/${REMOTE}/${RELEASE_BRANCH}" \
  "${SUBMISSION_REF}:refs/remotes/${REMOTE}/${SUBMISSION_REF}"

release_commit="$(git rev-parse "refs/remotes/${REMOTE}/${RELEASE_BRANCH}")"
submission_commit="$(git rev-parse "refs/remotes/${REMOTE}/${SUBMISSION_REF}")"

# The base the consumer actually had. Usually release_commit and
# submission_commit share it outright and merge-base recovers it for free.
#
# Usually, not always: `git subrepo push` does not guarantee the split it
# publishes stays attached to the branch it was split from. When it re-roots
# the split, the submission is a parentless history with no ancestor in common
# with release, merge-base answers nothing, and every three-way below would
# have nothing to be three-way about. The base is still recorded, though: a
# re-rooted split carries the dependency's own .gitrepo at its root, and that
# file's `commit =` field IS the release commit the consumer had. Recover it
# from there and graft the split onto it, which puts the history back in the
# shape the rest of this script already knows how to handle.
base_commit="$(git merge-base "$release_commit" "$submission_commit" || true)"
graft_root=""
if [ -z "$base_commit" ]; then
  echo "note: ${SUBMISSION_REF} shares no history with ${RELEASE_BRANCH};" \
       "recovering the base from the submission's .gitrepo"
  git cat-file -e "${submission_commit}:.gitrepo" 2>/dev/null \
    || die "the submission shares no history with ${RELEASE_BRANCH} and carries no .gitrepo to recover the base from"
  recorded="$(git config --blob "${submission_commit}:.gitrepo" subrepo.commit || true)"
  [ -n "$recorded" ] \
    || die "the submission's .gitrepo has no 'commit =' field, so the base it was taken from cannot be recovered"
  base_commit="$(git rev-parse --quiet --verify "${recorded}^{commit}")" \
    || die "the submission's .gitrepo names base ${recorded}, which is not a commit in this repository"
  git merge-base --is-ancestor "$base_commit" "$release_commit" \
    || die "the submission's .gitrepo names base ${recorded}, which is not on ${RELEASE_BRANCH}"
  # Only the first-parent root matters: the split is the linear chain the
  # consumer's commits became, and that chain is what needs a base underneath.
  graft_root="$(git rev-list --first-parent --max-parents=0 "$submission_commit" | tail -n 1)"
  git replace --graft "$graft_root" "$base_commit" >/dev/null
fi

# Replay the submission onto the latest release in release-shaped space. Both
# share the recovered base, so git uses it automatically; conflicts -> markers.
git checkout --quiet -B release-replay "$release_commit"
conflicted=false
if ! git merge --no-edit --no-ff "$submission_commit"; then
  conflicted=true
  git add -A
  git commit --no-edit --quiet \
    -m "MERGE CONFLICTS: consumer changes vs. latest release — resolve before merge"
fi

# The graft has done its job once the replay is a real commit; drop it so no
# later command in this job sees a rewritten history.
if [ -n "$graft_root" ]; then git replace -d "$graft_root" >/dev/null; fi

# Transplant the merged tree under release/ on top of main; keep main's .gitrepo.
git checkout --quiet -B "$PR_HEAD_BRANCH" "refs/remotes/${REMOTE}/${MAIN_BRANCH}"
[ -d "$RELEASE_DIR" ] || { echo "::error::main has no ./$RELEASE_DIR folder"; exit 1; }
git rm -r --quiet "$RELEASE_DIR"
git read-tree --prefix="${RELEASE_DIR}/" -u release-replay
git checkout "refs/remotes/${REMOTE}/${MAIN_BRANCH}" -- "${RELEASE_DIR}/.gitrepo"
git add -A

has_changes=true
if git diff --quiet --cached --no-ext-diff; then
  has_changes=false
else
  git commit --quiet -m "chore(suede): proposed change from ${SUBMISSION_REF}"
fi

if [ -n "${GITHUB_OUTPUT:-}" ]; then
  {
    echo "base_commit=$base_commit"
    echo "submission_commit=$submission_commit"
    echo "conflicted=$conflicted"
    echo "has_changes=$has_changes"
  } >> "$GITHUB_OUTPUT"
fi
echo "RESULT base=$base_commit conflicted=$conflicted has_changes=$has_changes"
