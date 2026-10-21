#!/usr/bin/env bash
#
# Compose and open the pull request for a rebuilt downstream submission.
#
# Split out of the workflow for two reasons: composing a description is not
# control flow, and "open a PR" is the one step that differs between forges.
# `gh` talks to GitHub; the Gitea backend is what lets the offline Actions
# harness reproduce the same flow.
#
# Inputs (env):
#   SUBMISSION_REF      required - the downstream/<...> branch, and the PR head
#   BASE_COMMIT         required - the release commit the consumer branched from
#   SUBMISSION_COMMIT   required - the consumer's commit
#   CONFLICTED          true => open as a draft and label it
#   MAIN_BRANCH         default: main
#   SUEDE_PR_BACKEND    gh (default) | gitea
#   GITEA_URL, GITEA_TOKEN, GITEA_REPO   required by the gitea backend

set -euo pipefail

MAIN_BRANCH="${MAIN_BRANCH:-main}"
CONFLICTED="${CONFLICTED:-false}"
BACKEND="${SUEDE_PR_BACKEND:-gh}"
SUBMISSION_REF="${SUBMISSION_REF:?SUBMISSION_REF is required}"
BASE_COMMIT="${BASE_COMMIT:?BASE_COMMIT is required}"
SUBMISSION_COMMIT="${SUBMISSION_COMMIT:?SUBMISSION_COMMIT is required}"

readonly CONFLICT_LABEL="needs-conflict-resolution"
readonly TITLE="chore(suede): proposed change from ${SUBMISSION_REF}"

submitted_commits() {
  git log --no-merges --format='- %s (%h)' "${BASE_COMMIT}..${SUBMISSION_COMMIT}" 2>/dev/null || true
}

conflict_notice() {
  [[ "$CONFLICTED" == "true" ]] || return 0
  cat <<'NOTICE'
> **Unresolved conflict markers are present in `release/`** - the consumer's
> changes overlap newer release changes. Resolve them before merging.

NOTICE
}

description() {
  cat <<DESCRIPTION
Snapshot proposed by a downstream consumer via \`git subrepo\`.

- **Branch / provenance:** \`${SUBMISSION_REF}\` (consumer repo + commit)
- **Consumer base (release):** \`${BASE_COMMIT}\`
- This branch is now \`${MAIN_BRANCH}\`-shaped: check it out, test, push fixes, merge.

$(conflict_notice)**Submitted commits**

$(submitted_commits)
DESCRIPTION
}

open_with_gh() {
  local body_path="$1"
  local draft=()
  [[ "$CONFLICTED" == "true" ]] && draft=(--draft)
  gh pr create --base "$MAIN_BRANCH" --head "$SUBMISSION_REF" \
    "${draft[@]}" --title "$TITLE" --body-file "$body_path"
  [[ "$CONFLICTED" == "true" ]] && gh pr edit "$SUBMISSION_REF" --add-label "$CONFLICT_LABEL"
  return 0
}

# Gitea's API is close enough to GitHub's for this one call, which is the whole
# reason the offline harness can exercise the real flow rather than a mock.
open_with_gitea() {
  local body_path="$1" payload
  command -v jq >/dev/null 2>&1 || { echo "open-pull-request: jq is required for the gitea backend" >&2; exit 1; }
  payload="$(jq -n --rawfile body "$body_path" --arg title "$TITLE" --arg head "$SUBMISSION_REF" \
    --arg base "$MAIN_BRANCH" '{title: $title, body: $body, head: $head, base: $base, labels: []}')"
  curl -fsSL -X POST \
    -H "Authorization: token ${GITEA_TOKEN:?GITEA_TOKEN is required}" \
    -H "Content-Type: application/json" \
    -d "$payload" \
    "${GITEA_URL:?GITEA_URL is required}/api/v1/repos/${GITEA_REPO:?GITEA_REPO is required}/pulls"
}

WORKSPACE="$(mktemp -d)"
trap 'rm -rf "$WORKSPACE"' EXIT
description > "$WORKSPACE/body.md"

case "$BACKEND" in
  gh)    open_with_gh "$WORKSPACE/body.md" ;;
  gitea) open_with_gitea "$WORKSPACE/body.md" ;;
  print) cat "$WORKSPACE/body.md" ;;   # what the harness asserts against
  *)     printf 'open-pull-request: unknown backend %s\n' "$BACKEND" >&2; exit 2 ;;
esac
