"""Delete data older than each organisation's plan history (run daily).

* Finished scans (with their reports) older than the plan's history period.
* Resolved or rejected findings that have not changed within that period (with history).
* Assistant usage records older than the period, and processed Stripe event ids after 90 days.

Open findings are kept regardless of age: deleting them would hide unfixed problems.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.core.management.base import BaseCommand, CommandParser
from django.utils import timezone

from accessguard_web.accounts.models import Organization
from accessguard_web.assistant.models import AssistantRun
from accessguard_web.billing.entitlements import entitlements_for
from accessguard_web.billing.models import StripeEvent
from accessguard_web.scans.models import ACTIVE_STATUSES, Finding, FindingStatus, Scan


class Command(BaseCommand):
    help = "Delete scans, closed findings and usage records older than the plan's history."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args: Any, **options: Any) -> None:
        dry_run = options["dry_run"]
        now = timezone.now()
        totals = {"scans": 0, "findings": 0, "assistant_runs": 0, "stripe_events": 0}
        for organization in Organization.objects.all():
            cutoff = now - timedelta(days=entitlements_for(organization).plan.history_days)
            scans = Scan.objects.filter(organization=organization, created_at__lt=cutoff).exclude(
                status__in=ACTIVE_STATUSES
            )
            findings = Finding.objects.filter(
                organization=organization,
                status__in=[FindingStatus.RESOLVED, FindingStatus.REJECTED],
                updated_at__lt=cutoff,
            )
            runs = AssistantRun.objects.filter(organization=organization, created_at__lt=cutoff)
            totals["scans"] += scans.count()
            totals["findings"] += findings.count()
            totals["assistant_runs"] += runs.count()
            if not dry_run:
                findings.delete()
                scans.delete()
                runs.delete()
        events = StripeEvent.objects.filter(received_at__lt=now - timedelta(days=90))
        totals["stripe_events"] = events.count()
        if not dry_run:
            events.delete()
        verb = "Would delete" if dry_run else "Deleted"
        self.stdout.write(f"{verb}: " + ", ".join(f"{v} {k}" for k, v in totals.items()))
