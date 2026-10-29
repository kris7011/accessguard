"""Mark running scans whose runner stopped sending heartbeats as lost (run every minute)."""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand

from accessguard_web.scans.services import sweep_lost_scans


class Command(BaseCommand):
    help = "Mark scans without recent runner heartbeats as lost."

    def handle(self, *args: Any, **options: Any) -> None:
        self.stdout.write(f"{sweep_lost_scans()} scans marked lost")
