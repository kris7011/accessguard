"""Typed errors shared across layers. Each represents an expected, reportable failure mode."""


class AccessGuardError(Exception):
    """Base class for expected AccessGuard errors."""


class PolicyError(AccessGuardError):
    """The access policy is malformed or violates a domain invariant."""


class PlanValidationError(AccessGuardError):
    """A proposed check order is not an exact permutation of the approved checks."""


class ScopeViolationError(AccessGuardError):
    """A request would leave the authorized target scope. Raised before any network I/O."""


class TransportError(AccessGuardError):
    """A request could not be completed: network error, timeout or oversized response."""


class PlannerError(AccessGuardError):
    """The optional model-backed planner failed or returned invalid output."""
