"""Self-serve runner download: the configured wheel, its SHA-256, members only."""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

from django.conf import settings
from django.test import TestCase, override_settings
from django.urls import reverse

from accessguard_web.tests.support import login, make_org

WHEEL_BYTES = b"PK\x03\x04 stand-in for a runner wheel"


class RunnerDownloadTests(TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)
        self.wheel = self.folder / "accessguard-0.3.0-py3-none-any.whl"
        self.wheel.write_bytes(WHEEL_BYTES)
        _, owner = make_org("Downloads")
        self.browser = login(owner)

    def serving(self, path: Path | str) -> override_settings:
        return override_settings(ACCESSGUARD={**settings.ACCESSGUARD, "RUNNER_WHEEL": str(path)})

    def test_members_download_the_configured_wheel_and_see_its_checksum(self) -> None:
        digest = hashlib.sha256(WHEEL_BYTES).hexdigest()
        with self.serving(self.wheel):
            response = self.browser.get(reverse("scans:runner_download"))
            self.assertEqual(response.content, WHEEL_BYTES)
            self.assertIn(self.wheel.name, response["Content-Disposition"])
            for page in (reverse("scans:runners"), reverse("pages:help_page", args=["runner"])):
                shown = self.browser.get(page)
                self.assertContains(shown, digest)
                self.assertContains(shown, reverse("scans:runner_download"))
                self.assertNotContains(shown, "you received from us")

    def test_anonymous_visitors_are_sent_to_sign_in(self) -> None:
        with self.serving(self.wheel):
            response = self.client.get(reverse("scans:runner_download"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("accounts:login"), response["Location"])

    def test_without_a_configured_wheel_there_is_no_download(self) -> None:
        with self.serving(""):
            self.assertEqual(self.browser.get(reverse("scans:runner_download")).status_code, 404)
            self.assertContains(self.browser.get(reverse("scans:runners")), "you received from us")

    def test_only_an_accessguard_wheel_is_served(self) -> None:
        other = self.folder / "settings.py"
        other.write_bytes(b"SECRET = 1")
        with self.serving(other):
            self.assertEqual(self.browser.get(reverse("scans:runner_download")).status_code, 404)
        with self.serving(self.folder / "accessguard-9.9.9-py3-none-any.whl"):
            self.assertEqual(self.browser.get(reverse("scans:runner_download")).status_code, 404)
