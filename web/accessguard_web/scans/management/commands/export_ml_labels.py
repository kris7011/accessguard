"""Write consented, de-identified labels as JSON lines (input for ``python -m accessguard_ml``).

Write the file outside the repository; customer-derived data is never committed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from accessguard_web.scans.learning import export_rows


class Command(BaseCommand):
    help = "Export consented, de-identified triage labels for ranking research."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--out", type=Path, required=True)

    def handle(self, *args: Any, **options: Any) -> None:
        rows = list(export_rows())
        path: Path = options["out"]
        path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), "utf-8")
        groups = len({row["application"] for row in rows})
        self.stdout.write(
            f"{len(rows)} labelled checks from {groups} applications written to {path}"
        )
