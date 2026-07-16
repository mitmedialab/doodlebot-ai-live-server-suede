#!/usr/bin/env bash
#
# run_tests.sh — run all Python files in tests/ in parallel.
#
# Usage:
#   ./run_tests.sh           # run tests
#   ./run_tests.sh --clean   # delete tests/output first, then run tests
#
# Each test's stdout/stderr is written to tests/<name>.log.
# Exits 0 if all tests pass, 1 if any test fails.

set -u

# Optionally clean the output directory
if [[ "${1:-}" == "--clean" ]]; then
    echo "Cleaning tests/output ..."
    rm -rf tests/output
fi

# Collect test files
shopt -s nullglob
test_files=(tests/*.py)
shopt -u nullglob

if [[ ${#test_files[@]} -eq 0 ]]; then
    echo "No Python files found in tests/" >&2
    exit 1
fi

# Launch each test in its own background process
declare -A pid_to_name
for file in "${test_files[@]}"; do
    name=$(basename "$file" .py)
    log_file="tests/${name}.log"

    python "$file" > "$log_file" 2>&1 &
    pid_to_name[$!]=$name
    echo "Started $file (pid $!) -> $log_file"
done

# Wait for all processes and track failures
exit_code=0
for pid in "${!pid_to_name[@]}"; do
    if wait "$pid"; then
        echo "PASS: ${pid_to_name[$pid]}"
    else
        echo "FAIL: ${pid_to_name[$pid]} (see tests/${pid_to_name[$pid]}.log)"
        exit_code=1
    fi
done

exit $exit_code