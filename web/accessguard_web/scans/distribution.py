"""The runner wheel this server offers for download, so customers install it without help.

The operator copies a built and checked wheel (docs/install.md, "We build the package") to
the server and points ``ACCESSGUARD_RUNNER_WHEEL`` at it. The SHA-256 shown next to the link
is computed from that file, so a customer can compare it with what they downloaded. PyPI
stays an optional, separate distribution channel.
"""

from __future__ import annotations

import functools
import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from accessguard_web import conf

logger = logging.getLogger(__name__)

WHEEL_NAME = re.compile(r"^accessguard-\d[\w.+]*-py3-none-any\.whl$")


@dataclass(frozen=True)
class RunnerWheel:
    path: Path
    sha256: str

    @property
    def name(self) -> str:
        return self.path.name


def runner_wheel() -> RunnerWheel | None:
    """The configured wheel, or None when none is configured or the file is not a runner wheel."""
    configured = conf.product()["RUNNER_WHEEL"]
    if not configured:
        return None
    path = Path(configured)
    if not WHEEL_NAME.match(path.name) or not path.is_file():
        logger.warning("ACCESSGUARD_RUNNER_WHEEL is not an AccessGuard wheel file; no download")
        return None
    info = path.stat()
    return _hashed(path, info.st_mtime_ns, info.st_size)


@functools.lru_cache(maxsize=4)
def _hashed(path: Path, mtime_ns: int, size: int) -> RunnerWheel:
    """Hashed once per file version; a replaced file has a new mtime or size."""
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1 << 16), b""):
            digest.update(chunk)
    return RunnerWheel(path, digest.hexdigest())
