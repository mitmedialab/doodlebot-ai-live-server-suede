# devcontainers-suede (release)

This folder is a ready-to-use bundle of Dev Container templates.

It includes:
- a set of prebuilt `.json` Dev Container configurations
- an `install.sh` helper script that links one config into your repository as `.devcontainer/devcontainer.json`

## Contents

Files in this release:
- `common.json`
- `docker-outside-of-docker.json`
- `node-default.json`
- `poetry-default.json`
- `python-default.json`
- `python-docker.json`
- `svelte-default.json`
- `svelte-docker-default.json`
- `svelte-tailwind-default.json`
- `typescript-default.json`
- `typescript-docker-default.json`
- `typescript-tailwind.json`
- `youtube.json`

You can open each file to inspect the exact settings before installing.

## Requirements

- `bash`
- `git`
- run the script from inside a Git repository

The script uses Git to detect your repository root.

## Quick Start

1. Place this release folder somewhere inside your repository.
2. Run the installer:

```bash
cd path/to/release
./install.sh common.json
```

> You may need to first make `install.sh` executable with `chmod +x install.sh`.

This creates:
- `.devcontainer/` at your repository root (if missing)
- `.devcontainer/devcontainer.json` as a symlink to the selected file in this folder

## Usage

```bash
./install.sh [--force] [file]
```

Examples:

```bash
./install.sh common.json
./install.sh --force common.json
./install.sh common.json --force
./install.sh
```

Behavior:
- If `file` is provided, it must exactly match a `.json` file in this folder.
- If no `file` is provided, the script shows a numbered interactive picker.
- If `.devcontainer/devcontainer.json` already exists, the script errors unless `--force` is set.
- `--force` can appear before or after the file argument.

## Interactive Mode

Running with no file argument:

```bash
./install.sh
```

The script prints available config files with numbers and asks you to pick one.

## Replacing an Existing Link

If you already have `.devcontainer/devcontainer.json`, replace it with:

```bash
./install.sh --force <file>.json
```

## Verify Installation

From your repository root:

```bash
ls -l .devcontainer/devcontainer.json
```

You should see a symlink pointing to the selected JSON file in this release folder.

## Errors and Troubleshooting

- `JSON file '<name>' was not found next to this script.`
	- Use an exact filename from the options list printed by the script.

- `<repo>/.devcontainer/devcontainer.json already exists. Use --force to replace it.`
	- Re-run with `--force` if replacement is intended.

- `Could not identify repository root.`
	- Ensure you are inside a Git repository (`git rev-parse --show-toplevel` should work).

- `No file provided and no interactive terminal is available.`
	- Pass a filename explicitly when running in non-interactive environments (CI, pipes, etc.).