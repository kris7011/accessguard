"""Process exit codes shared by the product CLI and the lab CLI."""

from accessguard.domain.results import OverallStatus

OK = 0
FINDINGS = 1
USAGE_ERROR = 2  # argparse convention
INCOMPLETE = 3
CONFIG_ERROR = 4
PLANNER_ERROR = 5

_BY_STATUS = {
    OverallStatus.CHECKS_PASSED: OK,
    OverallStatus.FINDINGS: FINDINGS,
    OverallStatus.NEEDS_REVIEW: INCOMPLETE,
    OverallStatus.INCOMPLETE: INCOMPLETE,
}


def for_status(status: str) -> int:
    """Map an overall report status to an exit code; only a full pass returns 0."""
    return _BY_STATUS[OverallStatus(status)]
