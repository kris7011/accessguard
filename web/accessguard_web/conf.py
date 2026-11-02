"""Typed access to AccessGuard's own settings dictionaries (see settings.py)."""

from __future__ import annotations

from typing import Any

from django.conf import settings


def product() -> dict[str, Any]:
    return settings.ACCESSGUARD


def stripe() -> dict[str, Any]:
    return settings.STRIPE


def assistant() -> dict[str, Any]:
    return settings.ASSISTANT
