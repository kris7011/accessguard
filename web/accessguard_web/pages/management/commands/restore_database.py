"""Restore the configured database from a backup made by ``backup_database``.

Stop the web processes and runners' access first; take a fresh backup of the current
state before restoring over it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import connection

from accessguard_web.pages.backup import BackupError, restore_postgres, restore_sqlite


class Command(BaseCommand):
    help = "Restore the database from a backup file."
    requires_system_checks = ()  # do not open the database before restoring it

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("backup", type=Path)
        parser.add_argument(
            "--replace", action="store_true", help="Overwrite an existing SQLite file"
        )

    def handle(self, *args: Any, **options: Any) -> None:
        settings = connection.settings_dict
        connection.close()
        try:
            if connection.vendor == "sqlite":
                restore_sqlite(
                    options["backup"], Path(settings["NAME"]), replace=options["replace"]
                )
            elif connection.vendor == "postgresql":
                restore_postgres(dict(settings), options["backup"])
            else:
                raise CommandError(f"unsupported database {connection.vendor}")
        except BackupError as error:
            raise CommandError(str(error)) from error
        self.stdout.write("Restore finished. Run `manage.py migrate` and check /readyz.")
