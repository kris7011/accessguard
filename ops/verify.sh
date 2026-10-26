#!/usr/bin/env bash
# Local and CI verification. Usage: bash ops/verify.sh [STEP ...]
#
# Every step is a single Python invocation whose output goes to a log file; the exit code is
# captured from the command itself (never from a pipe such as `cmd | tail`). All requested
# steps run, and the script exits with the original exit code of the first failing step.
#
# Steps: lint format types tests audit secrets build demo (default: all of them)
# Negative-control steps (never run by default): selftest-pass selftest-fail
# Environment: PYTHON (default: python), VERIFY_LOG_DIR (default: reports/verify)

set -uo pipefail  # no `set -e`: every step runs and failures are aggregated explicitly

cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 2
PYTHON="${PYTHON:-python}"
LOG_DIR="${VERIFY_LOG_DIR:-reports/verify}"
mkdir -p "$LOG_DIR" || exit 2

# Relative paths work for both POSIX and Windows interpreters; the separator comes from Python.
SEP="$("$PYTHON" -c 'import os; print(os.pathsep)')" || exit 2
export PYTHONPATH="src${SEP}lab${SEP}evaluations"

step_args() {
  case "$1" in
    lint) ARGS=(-m ruff check .) ;;
    format) ARGS=(-m ruff format --check .) ;;
    types) ARGS=(-m mypy) ;;
    tests) ARGS=(-m unittest discover -s tests -t .) ;;
    audit) ARGS=(-m pip_audit --strict --require-hashes -r requirements-dev.txt) ;;
    secrets) ARGS=(ops/ci/secret_scan.py) ;;
    build) ARGS=(ops/ci/build_check.py) ;;
    demo) ARGS=(-m accessguard_lab demo --out "$LOG_DIR/demo") ;;
    selftest-pass) ARGS=(-c "print('negative control: passing step')") ;;
    selftest-fail) ARGS=(-c "print('negative control: deliberately failing step'); raise SystemExit(3)") ;;
    *) return 1 ;;
  esac
}

if [ "$#" -eq 0 ]; then
  set -- lint format types tests audit secrets build demo
fi
for step in "$@"; do
  step_args "$step" || { echo "Unknown step: $step" >&2; exit 2; }
done

first_failure=0
summary=()
for step in "$@"; do
  step_args "$step"
  log="$LOG_DIR/$step.log"
  echo "==> $step: $PYTHON ${ARGS[*]}"
  "$PYTHON" "${ARGS[@]}" >"$log" 2>&1
  code=$?
  tail -n 15 "$log"  # display only; $code was captured above
  if [ "$code" -eq 0 ]; then
    summary+=("ok      $step")
  else
    summary+=("FAILED  $step (exit $code, log: $log)")
    [ "$first_failure" -eq 0 ] && first_failure=$code
  fi
done

echo
echo "Verification summary:"
printf '  %s\n' "${summary[@]}"
if [ "$first_failure" -ne 0 ]; then
  echo "Verification FAILED (exit $first_failure)"
else
  echo "Verification passed"
fi
exit "$first_failure"
