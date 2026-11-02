"""Back up the configured database (SQLite copy or PostgreSQL custom-format dump)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import connection

from accessguard_web.pages.backup import BackupError, backup_postgres, backup_sqlite


class Command(BaseCommand):
    help = "Write a consistent backup of the database to --out (a directory)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--out", type=Path, required=True)

    def handle(self, *args: Any, **options: Any) -> None:
        settings = connection.settings_dict
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        out: Path = options["out"]
        try:
            if connection.vendor == "sqlite":
                path = backup_sqlite(Path(settings["NAME"]), out / f"accessguard-{stamp}.sqlite3")
            elif connection.vendor == "postgresql":
                path = backup_postgres(dict(settings), out / f"accessguard-{stamp}.dump")
            else:
                raise CommandError(f"unsupported database {connection.vendor}")
        except BackupError as error:
            raise CommandError(str(error)) from error
        self.stdout.write(f"Backup written to {path} ({path.stat().st_size} bytes)")
