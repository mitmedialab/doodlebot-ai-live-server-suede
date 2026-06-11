#!/usr/bin/env bash

set -euo pipefail

usage() {
	cat <<'EOF'
Usage: install.sh [--force] [file]

Examples:
	./install.sh common.json
	./install.sh
	./install.sh --force node-default.json

Notes:
	- The file must be a .json file located next to this script.
	- If no file is provided, an interactive picker is shown.
EOF
}

err() {
	echo "Error: $*" >&2
}

# Make path absolute relative to $PWD if it isn't already
to_abs() {
	case $1 in
		/*) printf '%s\n' "$1" ;;
		*)  printf '%s\n' "$PWD/$1" ;;
	esac
}

# Collapse . and .. segments (string-based; works on non-existent paths)
normalize() {
	local path=$1 seg
	local -a parts=()
	local IFS=/
	for seg in $path; do
		case $seg in
			''|.) ;;
			..) [[ ${#parts[@]} -gt 0 ]] && unset "parts[$((${#parts[@]}-1))]" ;;
			*)  parts+=("$seg") ;;
		esac
	done
	if [[ ${#parts[@]} -eq 0 ]]; then
		printf '/\n'
	else
		printf '/%s' "${parts[@]}"
		printf '\n'
	fi
}

# Pure-bash relative path: relpath <target> <base>
relpath() {
	local target base
	target=$(normalize "$(to_abs "$1")")
	base=$(normalize "$(to_abs "$2")")

	local common=$base result=
	while [[ "${target#"$common"/}" == "$target" && "$target" != "$common" ]]; do
		common=$(dirname "$common")
		result="../$result"
	done

	if [[ "$target" == "$common" ]]; then
		result=${result%/}
	else
		result="${result}${target#"$common"/}"
	fi

	printf '%s\n' "${result:-.}"
}

self_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
force=false

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
	usage
	exit 0
fi

selected_name=""
while [[ $# -gt 0 ]]; do
	case "$1" in
		--force)
			force=true
			;;
		-*)
			err "Unknown option: $1"
			usage
			exit 1
			;;
		*)
			if [[ -n "$selected_name" ]]; then
				err "Expected at most one file argument."
				usage
				exit 1
			fi
			selected_name="$1"
			;;
	esac
	shift
done

# Collect candidate JSON files co-located with this script.
json_files=()
while IFS= read -r f; do json_files+=("$f"); done < <(find "$self_dir" -maxdepth 1 -type f -name '*.json' -exec basename {} \; | sort)

if [[ ${#json_files[@]} -eq 0 ]]; then
	err "No JSON files found next to this script."
	exit 1
fi

print_options() {
	echo "Available files:" >&2
	local i
	for i in "${!json_files[@]}"; do
		echo "  $((i + 1)). ${json_files[$i]}" >&2
	done
}

select_interactively() {
	if [[ ! -t 0 ]]; then
		err "No file provided and no interactive terminal is available."
		print_options
		exit 1
	fi

	echo "Select a file:" >&2
	print_options
	local choice
	read -r -p "Enter number: " choice

	if [[ ! "$choice" =~ ^[0-9]+$ ]]; then
		err "Invalid selection '$choice'. Expected a number."
		exit 1
	fi

	if (( choice < 1 || choice > ${#json_files[@]} )); then
		err "Selection out of range: $choice"
		exit 1
	fi

	printf '%s\n' "${json_files[$((choice - 1))]}"
}

if [[ -z "$selected_name" ]]; then
	selected_name="$(select_interactively)"
else
	if [[ ! -f "$self_dir/$selected_name" ]]; then
		err "JSON file '$selected_name' was not found next to this script."
		print_options
		exit 1
	fi
fi

source_file="$self_dir/$selected_name"

repo_root="$(git -C "$self_dir" rev-parse --show-toplevel 2>/dev/null || true)"
if [[ -z "$repo_root" ]]; then
	err "Could not identify repository root. Ensure this script is run from within a Git repository."
	exit 1
fi

devcontainer_dir="$repo_root/.devcontainer"
target_link="$devcontainer_dir/devcontainer.json"
relative_source_file="$(relpath "$source_file" "$devcontainer_dir")"

mkdir -p "$devcontainer_dir"

if [[ -e "$target_link" || -L "$target_link" ]]; then
	if [[ "$force" == true ]]; then
		rm -f "$target_link"
	else
		err "$target_link already exists. Use --force to replace it."
		exit 1
	fi
fi

ln -s "$relative_source_file" "$target_link"

echo "Linked $target_link -> $relative_source_file"
