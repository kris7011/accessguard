# ADR 0005: Tooling and CI (unittest, Ruff, mypy strict, locked dev dependencies)

- Status: accepted (milestone 1)
- Date: 2026-10-26

## Problem

Requirements §5 and §11 ask for lint, formatting, type checking, tests, dependency and secret
scanning and a build in CI, with locked dependencies and pinned actions, while the local demo must
keep working without installing anything.

## Options

- Test runner: `pytest` (richer fixtures, a dependency) or `unittest` (standard library).
- Locking: plain `==` pins, or hashes generated with pip-tools.
- Secret scanning: GitHub-hosted scanning (repository setting), gitleaks action, or
  `detect-secrets` (a Python tool that also runs locally).

## Decision

- **unittest** so `py -3 -m unittest discover -s tests -t .` works on a clean Python install.
  Tests are organized as `tests/unit`, `integration`, `security` and `e2e`.
- **Ruff** (lint + format) and **mypy `--strict`** over `src`, `lab`, `tests` and `ops`.
- **`requirements-dev.in` → `requirements-dev.txt`** compiled by pip-tools 7.6.2 with
  `--generate-hashes`; installed with `--require-hashes`. The build backend (setuptools) is in the
  lock so editable installs run with `--no-build-isolation`.
- **pip-audit** over the lock file; **detect-secrets** with a reviewed `.secrets.baseline`.
- **GitHub Actions** with `permissions: contents: read`, `persist-credentials: false`, actions
  pinned to full commit SHAs, and a test matrix of Ubuntu 3.11/3.13 and Windows 3.13.

## Consequences

- The lock file was resolved on Windows/CPython 3.13 and contains no environment markers; CI on
  Linux uses the same file. If a platform needs a different dependency set, the lock must be
  compiled per platform.
- Pinned SHAs need deliberate updates (e.g. Dependabot for actions).
- The workflow has not yet run on GitHub; it is verified only by running the same commands locally.

## Amendment 2026-10-26: verification scripts that preserve exit codes

A CI step piped `git ls-files` into `xargs` without `pipefail`, and the first local verification
judged results from piped output. Decision: one verification entry point per shell,
`ops/verify.sh` and `ops/verify.ps1`, with identical step names. Each step is a single Python
invocation with output written to a log file; the exit code is taken from the command, all
requested steps run, and the script exits with the first failing step's original code (a
missing interpreter fails too). CI uses `defaults.run.shell: bash` (`-eo pipefail`) and calls the
same scripts; the Windows job uses the PowerShell script. Python helpers replace shell pipelines
(`ops/ci/secret_scan.py`, `ops/ci/build_check.py`). Deliberately failing `selftest-fail` steps
serve as negative controls in `tests/e2e/test_verify_scripts.py`.

## Revisit when

The test suite needs fixtures or parallelism that justify pytest, when a frontend is added (ESLint,
TypeScript strict), or when the repository enables GitHub secret scanning and push protection.
