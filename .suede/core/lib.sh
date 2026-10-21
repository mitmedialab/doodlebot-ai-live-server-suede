#!/usr/bin/env bash
# .suede/core/lib.sh — what the maintainer's scripts have in common. Sourced,
# never run.
#
# The one rule everything here applies: a RELEASE DEPENDENCY is a symlink at
# the root named <name><sep><repo> - "what it is, then who needs it" - that
# resolves to a folder holding a .gitrepo outside release/. <repo> is this
# repository's name, or that name without its `suede.`/`suede__` prefix (the
# installer drops it when the dependency is prefixed too); <sep> is `.` or
# `__`. Nothing else declares one: it is simply this repository's own edge.
#
# Inputs (env):
#   RELEASE_DIR          default: release
#   SUEDE_RELEASE_CORE   where the consumer-facing scripts (diff, deps.sh) are;
#                        default: $RELEASE_DIR/.suede/core, which is where a
#                        dependency vendors them

RELEASE_DIR="${RELEASE_DIR:-release}"
SEPARATORS=("." "__")

lib_die() { printf '%s: %s\n' "${LIB_PREFIX:-suede}" "$*" >&2; exit 1; }
lib_say() { printf '%s: %s\n' "${LIB_PREFIX:-suede}" "$*" >&2; }

# Everything runs from the repository root, with root-relative paths.
lib_enter_root() {
  ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || lib_die "not inside a git repository"
  ROOT="$(cd "$ROOT" && pwd -P)"
  cd "$ROOT"
}

# The repository's name: what origin calls it, else the folder.
repo_name() {
  local url
  if url="$(git remote get-url origin 2>/dev/null)"; then
    url="${url%/}"; url="${url%.git}"; printf '%s\n' "${url##*[:/]}"
  else
    basename "$ROOT"
  fi
}

field() { git config -f "$1" --get "subrepo.$2" 2>/dev/null || true; }
short() { printf '%s' "${1:0:7}"; }

# The published spelling of a remote. A shipped record is resolved by people
# and runners holding no key of ours, so it names HTTPS; the live .gitrepo
# keeps whatever it has (SSH, from the installer) for pushing.
https_spelling() { # <url>
  local url="$1" rest host path
  case "$url" in
    ssh://*) rest="${url#ssh://}"; rest="${rest#*@}"; host="${rest%%/*}"; path="${rest#*/}" ;;
    *@*:*)   rest="${url#*@}"; host="${rest%%:*}"; path="${rest#*:}" ;;
    *)       printf '%s\n' "$url"; return ;;
  esac
  path="${path%/}"; path="${path%.git}"
  printf 'https://%s/%s\n' "$host" "$path"
}

# Root-relative real path of an entry, following a symlink; empty if it does
# not resolve to a directory.
real_path_of() { # <entry>
  local target
  target="$(cd "$1" 2>/dev/null && pwd -P)" || return 0
  printf '%s\n' "${target#"$ROOT"/}"
}

# The names this repository goes by at the end of a declaring symlink: its own,
# and, for a `suede.`/`suede__`-prefixed repository, the name without it.
repo_tails() {
  local repo
  repo="$(repo_name)"
  printf '%s\n' "$repo"
  case "$repo" in
    suede.?*)  printf '%s\n' "${repo#suede.}" ;;
    suede__?*) printf '%s\n' "${repo#suede__}" ;;
  esac
}

# Every release dependency, as "<entry>\t<real path>" lines, sorted. A symlink
# that carries the name but does not resolve to an install is reported on
# stderr and left out: an unfinished install or a leftover, not a declaration.
# Real folders never declare, whatever they are called.
release_dependencies() {
  local entry tail sep real matched
  local tails=()
  while IFS= read -r tail; do tails+=("$tail"); done < <(repo_tails)
  for entry in *; do
    [[ -L "$entry" ]] || continue
    matched=0
    for tail in "${tails[@]}"; do
      for sep in "${SEPARATORS[@]}"; do
        [[ "$entry" == ?*"$sep$tail" ]] && matched=1
      done
    done
    [[ "$matched" == 1 ]] || continue
    real="$(real_path_of "$entry")"
    if [[ -z "$real" ]]; then
      lib_say "$entry: dangling - it declares a release dependency but points at nothing"
    elif [[ ! -f "$real/.gitrepo" ]]; then
      lib_say "$entry: $real has no .gitrepo - not an installed dependency"
    elif [[ "$real" == "$RELEASE_DIR" || "$real" == "$RELEASE_DIR"/* ]]; then
      lib_say "$entry: points inside $RELEASE_DIR/ - a vendored dependency needs no declaration"
    else
      printf '%s\t%s\n' "$entry" "$real"
    fi
  done | sort
}

# Everything this repository ships a pointer to, directly or not, as
# "<kind>\t<entry>\t<real path>" lines: kind `release` for each declaration,
# and `transitive` for every install reached through their edges, all the way
# down, with the edge that reached it. Inside release/ is vendored and left
# out; a missing edge is deps.sh's to report, so it is skipped here.
shipped_dependencies() {
  local seen="" queue="" entry real record sibling target
  while IFS=$'\t' read -r entry real; do
    [[ -n "$entry" ]] || continue
    grep -qxF -- "$real" <<<"$seen" && continue
    seen+="$real"$'\n'; queue+="$real"$'\n'
    printf 'release\t%s\t%s\n' "$entry" "$real"
  done < <(release_dependencies)
  while [[ -n "$queue" ]]; do
    real="${queue%%$'\n'*}"; queue="${queue#*$'\n'}"
    for record in "$real"/.suede/.dependencies/*.gitrepo; do
      [[ -e "$record" ]] || continue
      entry="$(basename "$record" .gitrepo)"
      sibling="$(dirname "$real")/$entry"; sibling="${sibling#./}"
      target="$(real_path_of "$sibling")"
      [[ -n "$target" && -f "$target/.gitrepo" ]] || continue
      case "$target" in "$RELEASE_DIR"|"$RELEASE_DIR"/*) continue ;; esac
      grep -qxF -- "$target" <<<"$seen" && continue
      seen+="$target"$'\n'; queue+="$target"$'\n'
      printf 'transitive\t%s\t%s\n' "$sibling" "$target"
    done
  done
}

# Every installed dependency, as "<kind>\t<entry>\t<real path>" lines sorted by
# path: what shipped_dependencies finds (release, transitive), every other
# subrepo outside release/ as `development`, and those inside it as `vendored`.
# A subrepo nested inside another one is that dependency's business and is left
# out, and so is suede's own machinery (.suede/core, .github/workflows).
installed_dependencies() {
  local shipped kept="" file dir parent nested
  shipped="$(shipped_dependencies)"
  {
    [[ -n "$shipped" ]] && printf '%s\n' "$shipped"
    while IFS= read -r file; do
      dir="${file%/.gitrepo}"; dir="${dir#./}"
      case "$dir" in
        "$RELEASE_DIR") continue ;;
        .suede/core|*/.suede/core|.github/workflows|*/.github/workflows) continue ;;
      esac
      nested=0
      while IFS= read -r parent; do
        [[ -n "$parent" && "$dir" == "$parent"/* ]] && { nested=1; break; }
      done <<<"$kept"
      [[ "$nested" == 1 ]] && continue
      kept+="$dir"$'\n'
      awk -F'\t' -v d="$dir" '$3 == d { found = 1 } END { exit !found }' <<<"$shipped" && continue
      if [[ "$dir" == "$RELEASE_DIR"/* ]]; then
        printf 'vendored\t-\t%s\n' "$dir"
      else
        printf 'development\t-\t%s\n' "$dir"
      fi
    done < <(find . -name .gitrepo -not -path '*/.git/*' -not -path '*/node_modules/*' \
               -not -path '*/.worktrees/*' | LC_ALL=C sort)
  } | LC_ALL=C sort -t$'\t' -k3,3
}

# The ways to reach one repository, the recorded one first, then the other
# spelling (SSH <-> HTTPS); only HTTPS when HTTPS_ONLY=1. The same rule as the
# scripts in release/.suede/core.
spellings() { # <url>
  local url="$1" rest host path
  case "$url" in
    https://*|http://*) rest="${url#*://}"; rest="${rest#*@}"; host="${rest%%/*}"; path="${rest#*/}" ;;
    ssh://*)            rest="${url#ssh://}"; rest="${rest#*@}"; host="${rest%%/*}"; path="${rest#*/}" ;;
    *@*:*)              rest="${url#*@}"; host="${rest%%:*}"; path="${rest#*:}" ;;
    *)                  printf '%s\n' "$url"; return 0 ;;
  esac
  if [[ -z "$host" || -z "$path" || "$host" == *:* ]]; then printf '%s\n' "$url"; return 0; fi
  path="${path%/}"; path="${path%.git}"
  case "$url" in
    https://*|http://*)
      printf '%s\n' "$url"
      [[ "${HTTPS_ONLY:-0}" == 1 ]] || printf 'git@%s:%s.git\n' "$host" "$path" ;;
    *)
      [[ "${HTTPS_ONLY:-0}" == 1 ]] || printf '%s\n' "$url"
      printf 'https://%s/%s.git\n' "$host" "$path" ;;
  esac
}

# The commit at the tip of an install's branch on its remote, or nothing if no
# spelling answers. One ls-remote, no fetch.
remote_tip() { # <install path>
  local remote branch url tip
  remote="$(field "$1/.gitrepo" remote)"; branch="$(field "$1/.gitrepo" branch)"
  [[ -n "$remote" ]] || return 1
  while IFS= read -r url; do
    tip="$(GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-ssh -o BatchMode=yes -o ConnectTimeout=5}" GIT_TERMINAL_PROMPT=0 \
      git ls-remote --exit-code "$url" "refs/heads/${branch:-release}" 2>/dev/null | cut -f1)" \
      && [[ -n "$tip" ]] && { printf '%s\n' "$tip"; return 0; }
  done < <(spellings "$remote")
  return 1
}

# The consumer-facing script of that name, from the copy this repository
# vendors into release/. It has to be current: deps.sh is newer than some
# published cores.
release_tool() { # <name>
  local core="${SUEDE_RELEASE_CORE:-$RELEASE_DIR/.suede/core}"
  [[ -f "$core/$1" ]] \
    || lib_die "$core/$1 not found - update the vendored core with: bash .suede/core/sync.sh"
  printf '%s\n' "$core/$1"
}
