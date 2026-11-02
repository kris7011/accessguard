"""Unit economics and scenario model for AccessGuard (hypotheses, not forecasts).

    python ops/business/economics.py [--assumptions ops/business/assumptions.json]

Prints a Markdown table per scenario. The model separates three things that are often mixed:
revenue (what customers pay, excluding VAT), operating profit (revenue minus costs, before
tax) and private payout (what reaches the owner after corporate and dividend tax, assuming a
Danish ApS that distributes the whole profit as dividends).
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT = Path(__file__).with_name("assumptions.json")


@dataclass(frozen=True)
class Result:
    name: str
    customers: int
    revenue_month: float
    payment_fees: float
    ai_cost: float
    hosting: float
    fixed_costs: float
    acquisition: float
    operating_profit_month: float
    operating_profit_year: float
    corporate_tax_year: float
    dividend_tax_year: float
    private_payout_year: float
    replacement_customers_month: float


def ai_cost_per_customer(a: dict[str, Any]) -> float:
    ai = a["ai"]
    usd = (
        ai["input_tokens_per_request"] * ai["usd_per_million_input"]
        + ai["output_tokens_per_request"] * ai["usd_per_million_output"]
    ) / 1_000_000
    return float(usd * ai["eur_per_usd"] * ai["requests_per_paying_customer_month"])


def scenario(name: str, s: dict[str, Any], a: dict[str, Any]) -> Result:
    customers = int(s["paying_customers"])
    share = a["plan_mix_business_share"]
    arpa = (1 - share) * a["prices_eur_month"]["team"] + share * a["prices_eur_month"]["business"]
    revenue = customers * arpa
    fees = revenue * a["payment_fee_percent"] + customers * a["payment_fee_fixed_eur"]
    ai = customers * ai_cost_per_customer(a)
    hosting = a["hosting_fixed_eur_month"] + customers * a["hosting_per_customer_eur_month"]
    support = customers * a["support_cost_per_customer_eur_month"]
    fixed = (
        a["tools_and_services_eur_month"]
        + a["accounting_and_legal_eur_month"]
        + a["owner_salary_eur_month"]
    )
    acquisition = s["new_customers_month"] * a["acquisition_cost_per_new_customer_eur"]
    profit = revenue - fees - ai - hosting - support - fixed - acquisition
    year = profit * 12
    corporate = max(year, 0.0) * a["danish_corporate_tax"]
    distributable = max(year - corporate, 0.0)
    low = min(distributable, a["dividend_low_bracket_eur_year"])
    dividend_tax = (
        low * a["dividend_tax_low_rate"] + (distributable - low) * a["dividend_tax_high_rate"]
    )
    return Result(
        name, customers, revenue, fees, ai, hosting + support, fixed, acquisition, profit, year,
        corporate, dividend_tax, distributable - dividend_tax, customers * a["monthly_churn"],
    )  # fmt: skip


def table(results: list[Result]) -> str:
    columns = [
        "Scenario", "Paying customers", "Revenue / month", "Payment fees", "AI cost",
        "Hosting + support", "Fixed costs", "Acquisition", "Operating profit / month",
        "Operating profit / year", "Private payout / year", "Customers lost / month (churn)",
    ]  # fmt: skip
    rows = ["| " + " | ".join(columns) + " |", "| --- |" + " ---: |" * (len(columns) - 1)]
    for r in results:
        rows.append(
            f"| {r.name} | {r.customers} | {r.revenue_month:,.0f} | {r.payment_fees:,.0f} | "
            f"{r.ai_cost:,.0f} | {r.hosting:,.0f} | {r.fixed_costs:,.0f} | {r.acquisition:,.0f} | "
            f"{r.operating_profit_month:,.0f} | {r.operating_profit_year:,.0f} | "
            f"{r.private_payout_year:,.0f} | {r.replacement_customers_month:.1f} |"
        )
    return "\n".join(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assumptions", type=Path, default=DEFAULT)
    args = parser.parse_args()
    a = json.loads(args.assumptions.read_text("utf-8"))
    results = [scenario(name, s, a) for name, s in a["scenarios"].items()]
    print(f"AI cost per paying customer and month: EUR {ai_cost_per_customer(a):.2f}")
    print(table(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
