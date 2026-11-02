"""Create (or rotate) the platform demo runner and write its token to a local file.

The demo runner may claim demo scans only, and its local configuration trusts only the two
demo origins. The token is written to ``--token-file`` (never printed), with owner-only
permissions where the platform supports them.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandParser
from django.utils import timezone

from accessguard_web.scans.models import Runner


class Command(BaseCommand):
    help = "Create or rotate the platform demo runner; writes its token to a file."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--token-file", type=Path, required=True)

    def handle(self, *args: Any, **options: Any) -> None:
        Runner.objects.filter(is_platform_demo=True, revoked_at__isnull=True).update(
            revoked_at=timezone.now()
        )
        runner, token = Runner.issue(
            name="AccessGuard demo runner", organization=None, is_platform_demo=True
        )
        path: Path = options["token_file"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(token, "utf-8")
        with contextlib.suppress(OSError):
            path.chmod(0o600)
        self.stdout.write(f"Demo runner {runner.pk} created; token written to {path}")
