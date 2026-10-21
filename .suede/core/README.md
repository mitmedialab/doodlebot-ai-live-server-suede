# [Suede](https://github.com/pmalacho-mit/suede) Core (`main`)

Vendored at `.suede/core` on your dependency's **`main`** branch. These are the
maintainer's tools and the scripts CI runs — none of them ship to consumers.
(The consumer-facing half is vendored from `dependency/release/core` onto the
`release` branch, at the same path, and on `main` you have a copy of it at
`release/.suede/core`.)

All of it is bash. Every script runs from the repository root whatever
directory you call it from, and applies one rule:

> A **release dependency** is a symlink at the root named `<name><sep><repo>`
> — what it is, then who needs it — that resolves to a folder holding a
> `.gitrepo` outside `release/`. `<repo>` is this repository's name, or that
> name without its `suede.`/`suede__` prefix (the installer drops it when the
> dependency is prefixed too); `<sep>` is `.` or `__`. Nothing else declares
> one: it is simply this repository's own edge, and a real folder never counts.

This folder is a [git-subrepo](https://github.com/ingydotnet/git-subrepo) of the
suede library, so you get fixes by pulling rather than by editing:

```bash
bash .suede/core/sync.sh
```

## What CI runs

You do not invoke these by hand; the workflows in `.github/workflows` call them,
and they live here rather than in YAML so they can be tested without a runner.

### [push-release.sh](./push-release.sh)

The whole publish flow, run by `subrepo-push-release` when a change under
`release/` lands on `main`: regenerate the dependency records, **guard**, then
sync `release/` out to the `release` branch.

The guard is the part worth knowing about. A release dependency ships as a
*pointer*, so before that pointer goes out it checks two things:

- [`diff.sh --shipped-only`](#diffsh) — nothing that ships has drifted from
  its pinned commit, so every pointer is honest. Development dependencies and
  being behind are left out: neither is a reason to refuse a release;
- `deps.sh --check --in release` — every dependency this repository declares
  is actually in place, and so is everything *they* declare, all the way down,
  each at exactly the commit its record names. A dependency's edge pointed at
  another commit fails here even though every pointer is honest: consumers
  would install the recorded commit, not the one you built against.

If either fires, the reason goes into the job summary and **the `release`
branch is not touched**, so consumers stay on the last honest version.

```bash
DRY_RUN=1 bash .suede/core/push-release.sh            # stop after the guard
DRY_RUN=1 bash .suede/core/push-release.sh --https    # without the SSH attempt
```

The workflow passes `--https`: a runner has no SSH key, so trying SSH first
would only cost a timeout per dependency.

`RELEASE_DIR` (default `release`) overrides which folder is published, and
`SUEDE_RELEASE_CORE` where the consumer-facing `diff` and `deps.sh` are read
from (default `release/.suede/core`; if that copy predates `deps.sh`, the guard
tells you to run `sync.sh`).

### [rebuild-pr-branch.sh](./rebuild-pr-branch.sh)

Turns a consumer's `upstream` into something you can review. Given the
`downstream/**` branch their push created, it recovers the release commit they
branched from, replays their change onto the current release (conflicts become
markers), and transplants the result under `release/` on top of `main`.

Run by `suede-downstream-to-main`, which lives on the `release` branch but
checks out `main` — which is why this script is here and not in `release/core`.

### [open-pull-request.sh](./open-pull-request.sh)

Composes the PR description for that rebuilt branch and opens it. "Open a PR" is
the one step that differs between forges, so it sits behind a backend switch:
`gh` on GitHub, the Gitea API (via `jq`) in the offline test harness, and
`print` to see the description without opening anything.

```bash
SUEDE_PR_BACKEND=print bash .suede/core/open-pull-request.sh   # dry run
```

## What you run

### [push.sh](./push.sh) and [check-release.sh](./check-release.sh)

Push, and see whether the push publishes and whether the publish worked:

```bash
bash .suede/core/push.sh                    # git push, then check-release.sh
git push && bash .suede/core/check-release.sh   # the same, spelled out
bash .suede/core/check-release.sh           # on its own: main's latest commit on GitHub
```

`push.sh` is exactly `git push "$@" && bash .suede/core/check-release.sh`.
`check-release.sh` knows what was just pushed without being told: git logs every
push in `origin/main`'s reflog, with the commit `main` was at before. Then:

1. If nothing under `release/` changed, the publish workflow does not run; it
   says so and stops, asking GitHub nothing.
2. If `release/.gitrepo` pins a `release` commit GitHub does not have, because
   the `release` branch was never pushed, it says so and names the fix
   (`git push origin release`). The run cannot succeed without it.
3. It finds the `subrepo-push-release` run for the pushed commit, prints its
   link as soon as GitHub has created it, and waits for it to finish.
4. On success it reminds you to `git pull` the `.gitrepo` update the run pushes
   back. On failure it points at the run's job summary, where the guard writes
   its reason, and with a token in `GH_TOKEN` it prints the failed job's log too.

On its own it reports the run for `main`'s latest commit, or, when that commit
does not publish, the latest publish run. `--no-wait` reports where things
stand without waiting. Exit `0` published or nothing to publish, `1` the run
failed or the pin is missing, `2` could not tell. GitHub only, through its
public API (no `gh`); needs `curl` and `jq`.


### [extract.sh](./extract.sh)

Writes `release/.suede/.dependencies/` from what the tree declares: one
`<entry>.gitrepo` per release dependency, holding the HTTPS remote, the branch
and the commit. A consumer's `deps.sh` reads these and recreates each entry
beside the installed copy of your dependency — so the record's *name* is the
sibling path your `release/` code imports (`../<entry>/...`).

```bash
bash .suede/core/extract.sh
```

Anything else in that folder is removed: a record whose entry is gone, and the
`package.json`, `requirements.txt` and `separator` files older versions
generated. The publish flow runs this; run it yourself to see what would ship.

### [list.sh](./list.sh)

Every dependency in the repository and what kind it is — the rule above, read
off the tree. `transitive` is an install nothing here declares but a release
dependency's edge reaches (ENTRY names that edge): it ships through that
dependency's record, unlike `development`, which ships nothing.

```
KIND         ENTRY                      PATH                       PIN
release      widget.my-app              widget                     86abeeb
transitive   mixin.widget               mixin                      30142f6
development  -                          fixtures/harness           4f10c2a
vendored     -                          release/mixin              9bb0e41
```

### [diff.sh](./diff.sh)

How every installed dependency stands against its remote.

```bash
bash .suede/core/diff.sh                  # everything, for you
bash .suede/core/diff.sh --shipped-only   # what the publish guard checks
bash .suede/core/diff.sh --https          # skip SSH: no key here
```

By default it checks **release, transitive and development** dependencies for
two things:

| | Means | What to run |
| --- | --- | --- |
| **local changes** | your files differ from the commit the `.gitrepo` pins | `bash <dep>/.suede/core/diff` to see them |
| **behind** | the remote's branch has moved past that commit | the `diff --sync` it prints, then `sync` |

It exits `1` if anything has local changes, `2` if a comparison could not run,
and `0` otherwise. Being behind is reported, never an exit code.

`--shipped-only` narrows it to what this repository ships a pointer to — each
release dependency and every install their edges reach, declared or not — and
to local changes only. That is exactly what the publish guard runs, so a clean
`diff.sh --shipped-only` means a publish that will not be refused for that
reason. A transitive install counts because consumers install it fresh from the
record of the dependency that needs it, so a local edit to it would ship to no
one. A development dependency's changes are reported by the default run and
never block a publish.

Each dependency is compared with the `diff` that ships in `release/.suede/core`,
so your view and a consumer's cannot disagree. Vendored dependencies are never
checked: one exists precisely *because* it diverges, and it ships as source.

If it reports divergence in something that ships you have three honest options: revert the changes,
[upstream](../../release/core/README.md#upstream) them, or **vendor** the
dependency so the source itself ships. Vendoring is two git commands and a
review of your imports:

```bash
git mv widget release/widget          # the bytes now ship
git rm widget.my-app                  # no longer a pointer, so no declaration
grep -rn 'widget.my-app' release/     # repoint these to ./widget
```

Vendored code ships whole, so whatever `widget` needs beside it has to move
inside `release/` too — `deps.sh --in release/widget` will tell you what.

### [sync.sh](./sync.sh)

Updates every piece of suede machinery this repository vendors, in one command,
from `main`:

```bash
bash .suede/core/sync.sh
```

It finds them rather than being told — every subrepo whose remote is the suede
library. In a fully initialized dependency that is four:

| Path | What it is |
| --- | --- |
| `.suede/core` | this folder, the maintainer's tools |
| `release/.suede/core` | the tools that ship to consumers |
| `.github/workflows` | main's workflows |
| `release/.github/workflows` | the release branch's workflows |

`./release` is deliberately not one of them: it tracks *your* release branch and
is published by [push-release.sh](#push-releasesh), not pulled.

**Why this is a script and not four `git subrepo pull`s.** Two of the four
record a `parent` that is not a commit in this repository's history, so a plain
pull refuses:

- The **workflow** subrepos were cloned into the *template* your repository was
  created from. A repository made from a template starts a fresh history, so
  their parent names a commit that does not exist here at all. (They live in the
  template rather than being cloned at init because an Action is restricted in
  what it may do to `.github/workflows`.)
- A **`.suede/core` vendored onto the `release` branch** before the layout
  changed records a parent that does exist, but is a release-branch commit and
  so not an ancestor of `main`.

Both get repaired the same way — point `parent` at a commit that *is* in this
history, then pull — so `sync.sh` does it and retries instead of making you read
the diagnostic. It also clears the `subrepo/…` branch and `.git/tmp/subrepo/…`
directory that pulling a subrepo nested inside `release/` leaves behind, because
those stop the next `git subrepo push release` — the very next thing publishing
does.

Pass paths to limit it: `bash .suede/core/sync.sh .github/workflows`.

### [lib.sh](./lib.sh)

Not a command. The handful of functions the scripts above share — the
repository's name, the list of release dependencies, the HTTPS spelling of a
remote — so the rule is written once.
