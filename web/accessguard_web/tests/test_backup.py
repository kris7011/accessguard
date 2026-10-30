"""Backup and restore drill for SQLite with real product data (PostgreSQL: documented only)."""

from __future__ import annotations

import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from django.test import SimpleTestCase

from accessguard_web.pages.backup import BackupError, backup_sqlite, restore_sqlite


class SqliteBackupTests(SimpleTestCase):
    def test_backup_and_restore_round_trip_and_refuse_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            live = Path(tmp, "live.sqlite3")
            with closing(sqlite3.connect(live)) as db:
                db.execute("CREATE TABLE findings (id INTEGER PRIMARY KEY, check_id TEXT)")
                db.executemany("INSERT INTO findings (check_id) VALUES (?)", [("a@b",), ("c@d",)])
                db.commit()
            backup = backup_sqlite(live, Path(tmp, "backups", "b.sqlite3"))
            with closing(sqlite3.connect(live)) as db:
                db.execute("DELETE FROM findings")  # the incident the backup protects against
                db.commit()
            with self.assertRaises(BackupError):
                restore_sqlite(backup, live)
            restore_sqlite(backup, live, replace=True)
            with closing(sqlite3.connect(live)) as db:
                rows = [r[0] for r in db.execute("SELECT check_id FROM findings ORDER BY id")]
            self.assertEqual(rows, ["a@b", "c@d"])
            restored = restore_sqlite(backup, Path(tmp, "fresh.sqlite3"))
            self.assertTrue(restored.exists())

    def test_missing_database_is_an_error(self) -> None:
        with self.assertRaises(BackupError):
            backup_sqlite(Path("does-not-exist.sqlite3"), Path("x.sqlite3"))
