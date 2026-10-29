"""The ML export respects consent, carries no identifiers and only verified labels."""

from __future__ import annotations

import io
import json
import tempfile
from pathlib import Path

from django.core.management import call_command
from django.test import TestCase

from accessguard_ml.data import read_export
from accessguard_sampleapp.app import Variant
from accessguard_web.scans import services
from accessguard_web.scans.learning import export_rows
from accessguard_web.scans.models import Finding, FindingStatus
from accessguard_web.tests.support import (
    make_org,
    org_runner,
    sample_project,
    sample_runtime,
    scan_and_ingest,
)


class ExportTests(TestCase):
    def setUp(self) -> None:
        self.org, self.owner = make_org("Learner")
        runner, _ = org_runner(self.org)
        with sample_runtime(Variant.FLAWED) as (app, config):
            self.project = sample_project(self.org, app.origin)
            scan_and_ingest(self.project, self.owner, runner, config)

    def test_nothing_is_exported_without_consent(self) -> None:
        self.assertEqual(list(export_rows()), [])

    def test_only_verified_labels_without_identifiers_are_exported(self) -> None:
        self.org.ml_data_consent = True
        self.org.save()
        confirmed = Finding.objects.get(check_id="orbit-billing@ben")
        services.triage(confirmed, self.owner, FindingStatus.CONFIRMED, "real")
        rejected = Finding.objects.get(check_id="list:projects@ana")
        services.triage(rejected, self.owner, FindingStatus.REJECTED, "intended for this demo")
        rows = list(export_rows())
        labels = sorted(row["label"] for row in rows)
        # 20 engine passes + 1 rejected = 21 negatives; 1 confirmed positive; 8 unreviewed
        # findings are not exported.
        self.assertEqual((labels.count(0), labels.count(1)), (21, 1))
        text = json.dumps(rows)
        for identifier in (
            "orbit",
            "quasar",
            "ben",
            "billing",
            "/api",
            "Learner",
            "data.name",
            "prj-",
        ):
            self.assertNotIn(identifier, text)
        self.assertEqual(len({row["application"] for row in rows}), 1)

    def test_export_file_is_readable_by_the_ml_package(self) -> None:
        self.org.ml_data_consent = True
        self.org.save()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "export.jsonl")
            call_command("export_ml_labels", out=path, stdout=io.StringIO())
            examples = read_export(path)
        self.assertEqual(len(examples), 20)
        self.assertTrue(
            all(e.provenance["source"] == "customer-consented-export" for e in examples)
        )
