"""Database backup and restore for the two supported databases.

* SQLite: an online, consistent copy with the ``sqlite3`` backup API (safe while the
  application runs).
* PostgreSQL: ``pg_dump --format=custom`` and ``pg_restore --clean --if-exists``; the
  password is passed through ``PGPASSWORD`` in the child environment, never on the
  command line.

A restore refuses to overwrite an existing SQLite file unless ``replace`` is set, so a
mistyped path cannot destroy data (CLAUDE.md: no destructive step without a backup).
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path
from typing import Any


class BackupError(Exception):
    pass


def backup_sqlite(source: Path, target: Path) -> Path:
    if not source.exists():
        raise BackupError(f"database {source} does not exist")
    target.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(target)) as dst:
        src.backup(dst)
    with closing(sqlite3.connect(target)) as check:
        if check.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise BackupError(f"integrity check of {target} failed")
    return target


def restore_sqlite(backup: Path, target: Path, *, replace: bool = False) -> Path:
    if not backup.exists():
        raise BackupError(f"backup {backup} does not exist")
    # An empty file is what opening a missing SQLite database creates; it holds no data.
    if target.exists() and target.stat().st_size > 0 and not replace:
        raise BackupError(f"{target} exists; pass --replace after taking a fresh backup of it")
    with closing(sqlite3.connect(backup)) as src, closing(sqlite3.connect(target)) as dst:
        src.backup(dst)
    return target


def _pg_env(db: dict[str, Any]) -> tuple[list[str], dict[str, str]]:
    args = ["--host", str(db["HOST"] or "localhost"), "--username", str(db["USER"])]
    if db.get("PORT"):
        args += ["--port", str(db["PORT"])]
    env = dict(os.environ, PGPASSWORD=str(db.get("PASSWORD") or ""))
    return args, env


def _tool(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise BackupError(f"{name} is not installed (install the PostgreSQL client tools)")
    return path


def backup_postgres(db: dict[str, Any], target: Path) -> Path:
    args, env = _pg_env(db)
    target.parent.mkdir(parents=True, exist_ok=True)
    command = [_tool("pg_dump"), *args, "--format=custom", "--file", str(target), str(db["NAME"])]
    if subprocess.run(command, env=env, check=False).returncode != 0:  # noqa: S603
        raise BackupError("pg_dump failed")
    return target


def restore_postgres(db: dict[str, Any], backup: Path) -> None:
    args, env = _pg_env(db)
    command = [
        _tool("pg_restore"),
        *args,
        "--clean",
        "--if-exists",
        "--no-owner",
        "--dbname",
        str(db["NAME"]),
        str(backup),
    ]
    if subprocess.run(command, env=env, check=False).returncode != 0:  # noqa: S603
        raise BackupError("pg_restore failed")
