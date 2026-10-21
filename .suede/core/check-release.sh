#!/usr/bin/env bash
#
# Did the last push to main publish, and did the publish work?
#
#   git push && bash .suede/core/check-release.sh   # what .suede/core/push.sh does
#   bash .suede/core/check-release.sh               # on its own: main's latest commit on GitHub
#   bash .suede/core/check-release.sh --no-wait     # where things stand, without waiting
#
# Right after a push it knows exactly what was pushed: git logs every push in
# origin/main's reflog, with the commit main was at before. The publish
# workflow (subrepo-push-release) only runs when something under release/
# changed, so if nothing did it says so and stops. Otherwise it finds the run
# for the pushed commit, prints its link as soon as GitHub has created it, and
# waits for it to finish.
#
# On its own - nothing pushed just now - it fetches and reports the publish
# run for main's latest commit; if there is none, the latest publish run, and
# whether anything since then touches release/.
#
# Before waiting, it checks the one mistake a run cannot recover from:
# release/.gitrepo pinning a release commit GitHub does not have, because the
# release branch was never pushed. It names the fix.
#
# GitHub only, through its public API: no gh, no token. A token in GH_TOKEN
# (or GITHUB_TOKEN) lifts the 60-requests-an-hour limit and, when a run fails,
# lets it print the failed job's log; without one it prints the run's link,
# whose summary carries the publish guard's reason. Needs curl and jq.
#
# Exit: 0 published, or nothing to publish; 1 the run failed, or the pin is
# not on GitHub; 2 could not tell (not GitHub, no run appeared, API unreachable).
#
# Env:
#   SUEDE_GITHUB_API      default https://api.github.com
#   SUEDE_CHECK_APPEAR    seconds to wait for GitHub to create the run (default 90)
#   SUEDE_CHECK_TIMEOUT   seconds to wait for it to finish (default 900)
#   SUEDE_CHECK_INTERVAL  seconds between polls (default 5)

set -euo pipefail
LIB_PREFIX="check-release"
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

usage() { grep '^#' "$0" | grep -v '^#!/' | sed 's/^# \?//'; exit 0; }

WAIT=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage ;;
    --no-wait) WAIT=0; shift ;;
    *) printf 'check-release: unknown argument: %s (see --help)\n' "$1" >&2; exit 2 ;;
  esac
done

API="${SUEDE_GITHUB_API:-https://api.github.com}"
APPEAR="${SUEDE_CHECK_APPEAR:-90}"
TIMEOUT="${SUEDE_CHECK_TIMEOUT:-900}"
INTERVAL="${SUEDE_CHECK_INTERVAL:-5}"
WORKFLOW="subrepo-push-release.yml"
MAIN="main"
ORIGIN_MAIN="refs/remotes/origin/$MAIN"
ORIGIN_RELEASE="refs/remotes/origin/release"

say() { printf 'check-release: %s\n' "$*"; }
cannot_tell() { say "$*"; exit 2; }

lib_enter_root
command -v curl >/dev/null 2>&1 || cannot_tell "needs curl"
command -v jq >/dev/null 2>&1 || cannot_tell "needs jq (apt-get install jq, brew install jq)"

# --- where ----------------------------------------------------------------

github_slug() { # <url> -> owner/name, or nothing for a remote not on GitHub
  local rest
  case "$1" in
    https://github.com/*|http://github.com/*) rest="${1#*github.com/}" ;;
    git@github.com:*)                         rest="${1#git@github.com:}" ;;
    ssh://git@github.com/*)                   rest="${1#ssh://git@github.com/}" ;;
    *) return 1 ;;
  esac
  rest="${rest%/}"; rest="${rest%.git}"
  printf '%s' "$rest"
}

# The URL as configured: `git remote get-url` applies insteadOf rewrites (a
# token-carrying one, say), which would hide that origin is on GitHub.
ORIGIN_URL="$(git config --get remote.origin.url 2>/dev/null)" || cannot_tell "no remote named origin"
SLUG="$(github_slug "$ORIGIN_URL")" \
  || cannot_tell "origin is not on GitHub ($ORIGIN_URL): there is no Actions run to follow"
WORKFLOW_PAGE="https://github.com/$SLUG/actions/workflows/$WORKFLOW"

# --- what was pushed --------------------------------------------------------
# Read before fetching: a fetch that moves origin/main logs an entry of its own.

PUSHED=0; BEFORE=""
if [[ "$(git reflog show -n 1 --format=%gs "$ORIGIN_MAIN" 2>/dev/null)" == "update by push"* ]]; then
  PUSHED=1
  BEFORE="$(git rev-parse -q --verify "$ORIGIN_MAIN@{1}" 2>/dev/null || true)"
fi

git fetch -q origin "$MAIN" release 2>/dev/null \
  || git fetch -q origin "$MAIN" 2>/dev/null \
  || cannot_tell "could not fetch $MAIN from origin"
TIP="$(git rev-parse -q --verify "$ORIGIN_MAIN")" || cannot_tell "origin has no $MAIN branch"

if AHEAD="$(git rev-list --count "$ORIGIN_MAIN..$MAIN" 2>/dev/null)" && [[ "$AHEAD" -gt 0 ]]; then
  say "your $MAIN has $AHEAD commit(s) GitHub does not: this checks what GitHub has ($(short "$TIP"))"
fi

touches_release() { # <from or empty> <to>
  [[ -z "$1" ]] && return 0
  [[ -n "$(git diff --name-only "$1" "$2" -- "$RELEASE_DIR" 2>/dev/null)" ]]
}

if [[ "$PUSHED" == 1 ]] && ! touches_release "$BEFORE" "$TIP"; then
  say "the push ($(short "$BEFORE")..$(short "$TIP")) changed nothing under $RELEASE_DIR/, so it does not publish"
  exit 0
fi

# --- the pin -----------------------------------------------------------------

PIN_PROBLEM=0
check_pin() {
  local pin
  pin="$(git config --blob "$TIP:$RELEASE_DIR/.gitrepo" --get subrepo.commit 2>/dev/null || true)"
  [[ -n "$pin" ]] || return 0
  git rev-parse -q --verify "$ORIGIN_RELEASE" >/dev/null || return 0
  if git cat-file -e "$pin^{commit}" 2>/dev/null && git merge-base --is-ancestor "$pin" "$ORIGIN_RELEASE"; then
    return 0
  fi
  PIN_PROBLEM=1
  say "$RELEASE_DIR/.gitrepo pins $(short "$pin"), which is not on GitHub's release branch ($(short "$(git rev-parse "$ORIGIN_RELEASE")"))."
  if git rev-parse -q --verify refs/heads/release >/dev/null && git merge-base --is-ancestor "$pin" refs/heads/release 2>/dev/null; then
    say "your local release branch has it. Push it, then re-run the publish from the run's page:"
    say "    git push origin release"
  else
    say "no local branch has it either: the commit that pins it was made somewhere else."
  fi
}

# --- GitHub -----------------------------------------------------------------

api() { # <path>
  local token="${GH_TOKEN:-${GITHUB_TOKEN:-}}" auth=()
  [[ -n "$token" ]] && auth=(-H "Authorization: Bearer $token")
  curl -fsSL -H "Accept: application/vnd.github+json" ${auth[@]+"${auth[@]}"} "$API/$1"
}
run_for() { # <sha> -> the run as one line of JSON, nothing if there is none
  api "repos/$SLUG/actions/workflows/$WORKFLOW/runs?head_sha=$1&per_page=1" | jq -c '.workflow_runs[0] // empty'
}
latest_run() {
  api "repos/$SLUG/actions/workflows/$WORKFLOW/runs?branch=$MAIN&event=push&per_page=1" | jq -c '.workflow_runs[0] // empty'
}
get() { jq -r ".$1 // empty" <<<"$2"; }

# Standalone, main's tip may simply not publish: then the run worth reporting
# is the latest one, if nothing since it touches release/.
TARGET="$TIP"
if [[ "$PUSHED" == 0 ]]; then
  RUN="$(run_for "$TIP")" || cannot_tell "could not reach GitHub's API ($API)"
  if [[ -z "$RUN" ]]; then
    LATEST="$(latest_run)" || cannot_tell "could not reach GitHub's API ($API)"
    LATEST_SHA="$(get head_sha "$LATEST")"
    if [[ -z "$LATEST" ]]; then
      say "no publish run on record for $SLUG"
    elif git merge-base --is-ancestor "$LATEST_SHA" "$TIP" 2>/dev/null && ! touches_release "$LATEST_SHA" "$TIP"; then
      say "nothing since $(short "$LATEST_SHA") touches $RELEASE_DIR/, so $MAIN's latest commit does not publish; the latest publish run is for $(short "$LATEST_SHA")"
      TARGET="$LATEST_SHA"
    else
      say "$MAIN's latest commit ($(short "$TIP")) changes $RELEASE_DIR/ since the last publish run"
    fi
  fi
fi

check_pin

RUN=""
deadline=$((SECONDS + APPEAR))
while :; do
  RUN="$(run_for "$TARGET")" || cannot_tell "could not reach GitHub's API ($API)"
  [[ -n "$RUN" ]] && break
  [[ "$WAIT" == 1 && "$SECONDS" -lt "$deadline" ]] || break
  sleep "$INTERVAL"
done
if [[ -z "$RUN" ]]; then
  say "GitHub has no publish run for $(short "$TARGET")$( [[ "$WAIT" == 1 ]] && printf ' after %ss' "$APPEAR" ). Its runs: $WORKFLOW_PAGE"
  [[ "$PIN_PROBLEM" == 1 ]] && exit 1
  exit 2
fi

URL="$(get html_url "$RUN")"; RUN_ID="$(get id "$RUN")"
say "publish run for $(short "$TARGET"): $URL"

# --- follow it -------------------------------------------------------------

STATUS=""; last=""
deadline=$((SECONDS + TIMEOUT))
while :; do
  STATUS="$(get status "$RUN")"
  [[ "$STATUS" != "$last" && "$STATUS" != completed ]] && say "$STATUS"
  last="$STATUS"
  [[ "$STATUS" == completed ]] && break
  if [[ "$WAIT" == 0 ]]; then
    say "still $STATUS; run again to see how it ends"
    [[ "$PIN_PROBLEM" == 1 ]] && exit 1
    exit 0
  fi
  if [[ "$SECONDS" -ge "$deadline" ]]; then
    say "still $STATUS after ${TIMEOUT}s; follow it at $URL"
    exit 2
  fi
  sleep "$INTERVAL"
  RUN="$(api "repos/$SLUG/actions/runs/$RUN_ID")" || cannot_tell "could not reach GitHub's API ($API)"
done

CONCLUSION="$(get conclusion "$RUN")"
if [[ "$CONCLUSION" == success ]]; then
  git fetch -q origin release 2>/dev/null || true
  say "published: the release branch is at $(short "$(git rev-parse -q --verify "$ORIGIN_RELEASE" || echo unknown)")"
  say "the run pushed a .gitrepo update back to $MAIN; bring it in with: git pull"
  exit 0
fi

say "the publish run ended: $CONCLUSION"
say "its job summary says why: $URL"
token="${GH_TOKEN:-${GITHUB_TOKEN:-}}"
if [[ -n "$token" ]]; then
  job="$(api "repos/$SLUG/actions/runs/$RUN_ID/jobs" | jq -r '[.jobs[] | select(.conclusion == "failure")][0].id // empty' || true)"
  if [[ -n "$job" ]]; then
    say "the failed job's log ends:"
    api "repos/$SLUG/actions/jobs/$job/logs" 2>/dev/null | tail -n 30 | sed 's/^/    /' || true
  fi
fi
exit 1
