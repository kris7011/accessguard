"""Credential value object that resists accidental disclosure."""

from __future__ import annotations


class Secret:
    """Wraps a credential so that repr(), str() and logging never render its value.

    Call ``reveal()`` only at the point where the value is placed on the wire.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not value:
            raise ValueError("A secret must not be empty")
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret('***')"

    __str__ = __repr__
