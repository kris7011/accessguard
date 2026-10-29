"""Plans and their limits. Prices and limits are hypotheses until validated with customers.

The single source for the pricing page, Checkout and server-side enforcement. Demo scans
of the synthetic project never count towards a limit.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class PlanId(StrEnum):
    FREE = "free"
    TEAM = "team"
    BUSINESS = "business"


@dataclass(frozen=True)
class Plan:
    id: PlanId
    name: str
    monthly_price_eur: int
    summary: str
    projects: int
    scans_per_month: int
    checks_per_scan: int
    runners: int
    members: int
    assistant_requests_per_month: int
    history_days: int

    @property
    def is_paid(self) -> bool:
        return self.monthly_price_eur > 0


PLANS = {
    PlanId.FREE: Plan(
        PlanId.FREE, "Free", 0, "Try AccessGuard on one test environment.",
        projects=1, scans_per_month=10, checks_per_scan=60, runners=1, members=2,
        assistant_requests_per_month=10, history_days=30,
    ),
    PlanId.TEAM: Plan(
        PlanId.TEAM, "Team", 49, "Regular access-control regression testing for one product.",
        projects=5, scans_per_month=300, checks_per_scan=600, runners=3, members=10,
        assistant_requests_per_month=200, history_days=365,
    ),
    PlanId.BUSINESS: Plan(
        PlanId.BUSINESS, "Business", 199, "Several products, CI on every change, longer history.",
        projects=25, scans_per_month=3000, checks_per_scan=3000, runners=15, members=50,
        assistant_requests_per_month=1000, history_days=730,
    ),
}  # fmt: skip


def plan(plan_id: str) -> Plan:
    return PLANS[PlanId(plan_id)]
