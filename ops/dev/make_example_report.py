"""Regenerate the public example report by really scanning the flawed sample app.

Usage: python ops/dev/make_example_report.py  (PYTHONPATH must include src and sampleapp)
Writes web/accessguard_web/pages/example_report.json. The report is real engine output; a
test (web tests: ExampleReportTests) checks that its findings match the sample answer key.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from accessguard.adapters.runner_config import parse_runner_config
from accessguard.adapters.scan_spec import parse_scan_spec
from accessguard.entrypoints.runner_service import execute_spec
from accessguard_sampleapp.app import Variant
from accessguard_sampleapp.server import run_sample_app

ROOT = Path(__file__).resolve().parents[2]
SPEC = ROOT / "sampleapp" / "accessguard_sampleapp" / "config" / "scan-spec.json"
OUT = ROOT / "web" / "accessguard_web" / "pages" / "example_report.json"


def main() -> int:
    document = json.loads(SPEC.read_text("utf-8"))
    with run_sample_app(Variant.FLAWED, port=8800) as app, tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "credentials.json").write_text(json.dumps(dict(app.api_keys)), "utf-8")
        config = parse_runner_config(
            {
                "targets": [
                    {
                        "origin": app.origin,
                        "zone": "loopback",
                        "credentials_file": "credentials.json",
                    }
                ]
            },
            base_dir=Path(tmp),
        )
        document["target"]["origin"] = app.origin
        report = execute_spec(parse_scan_spec(document), config, environ={}).report
    OUT.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", "utf-8", newline="\n")
    print(f"{report['status']['overall']}: {report['summary']} -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
