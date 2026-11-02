"""Scan specs and the runner's local configuration are strict trust boundaries."""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from accessguard.adapters.http_target import AuthKind
from accessguard.adapters.runner_config import (
    RunnerConfigError,
    credential_env_name,
    load_credentials,
    load_runner_config,
    parse_runner_config,
)
from accessguard.adapters.scan_spec import parse_scan_spec, sha256_of
from accessguard.domain.errors import PolicyError, ScopeViolationError
from accessguard.domain.scope import NetworkZone
from tests.helpers import REPO_ROOT

SAMPLE_SPEC = json.loads(
    (REPO_ROOT / "sampleapp" / "accessguard_sampleapp" / "config" / "scan-spec.json").read_text(
        "utf-8"
    )
)


def spec(**changes: object) -> dict[str, object]:
    document: dict[str, object] = copy.deepcopy(SAMPLE_SPEC)
    document.update(changes)
    return document


class ScanSpecTests(unittest.TestCase):
    def test_sample_spec_parses(self) -> None:
        parsed = parse_scan_spec(spec())
        self.assertEqual(parsed.target.origin, "http://127.0.0.1:8800")
        self.assertEqual(parsed.target.path_prefixes, ("/api/v1",))
        self.assertEqual(parsed.target.auth.kind, AuthKind.HEADER)
        self.assertEqual(parsed.target.auth.name, "X-Api-Key")
        self.assertEqual(parsed.limits.max_requests, 300)
        self.assertIsNone(parsed.checks)
        self.assertEqual(parsed.sha256, sha256_of(spec()))
        self.assertEqual(parsed.policy.sha256, sha256_of(SAMPLE_SPEC["policy"]))

    def test_hash_ignores_formatting_but_not_content(self) -> None:
        reordered = json.loads(json.dumps(spec(), sort_keys=True))
        self.assertEqual(parse_scan_spec(reordered).sha256, parse_scan_spec(spec()).sha256)
        self.assertNotEqual(
            parse_scan_spec(spec(label="other")).sha256, parse_scan_spec(spec()).sha256
        )

    def test_invalid_specs_are_rejected(self) -> None:
        bad_target = copy.deepcopy(SAMPLE_SPEC["target"])
        bad_target["token"] = "x"  # credentials must never be part of a spec
        cases = {
            "unknown key": spec(extra=1),
            "credential in target": spec(target=bad_target),
            "version": spec(spec_version="2"),
            "auth kind": spec(target={"origin": "http://127.0.0.1:1", "auth": {"kind": "basic"}}),
            "header without name": spec(
                target={"origin": "http://127.0.0.1:1", "auth": {"kind": "header"}}
            ),
            "prefix": spec(target={"origin": "http://127.0.0.1:1", "path_prefixes": ["api"]}),
            "budget": spec(limits={"max_requests": 0}),
            "budget type": spec(limits={"max_requests": True}),
            "duration": spec(limits={"max_duration_seconds": 7200}),
            "checks empty": spec(checks=[]),
            "checks type": spec(checks=[1]),
            "policy": spec(policy={"schema_version": "1"}),
        }
        for name, document in cases.items():
            with self.subTest(case=name), self.assertRaises(PolicyError):
                parse_scan_spec(document)

    def test_check_selection_is_deduplicated(self) -> None:
        parsed = parse_scan_spec(spec(checks=["orbit-apollo@cleo", "orbit-apollo@cleo"]))
        self.assertEqual(parsed.checks, ("orbit-apollo@cleo",))


def config(**changes: object) -> dict[str, object]:
    document: dict[str, object] = {
        "control_plane": {"url": "https://app.example.test"},
        "targets": [
            {
                "origin": "https://staging.example.test",
                "zone": "public",
                "credentials_file": "c.json",
            }
        ],
    }
    document.update(changes)
    return document


class RunnerConfigTests(unittest.TestCase):
    def test_trusted_origin_is_matched_after_normalisation(self) -> None:
        parsed = parse_runner_config(config(), base_dir=Path("/x"))
        target = parsed.trusted_target("https://STAGING.example.test:443")
        self.assertEqual(target.zone, NetworkZone.PUBLIC)
        with self.assertRaises(ScopeViolationError):
            parsed.trusted_target("https://evil.example.test")
        with self.assertRaises(ScopeViolationError):
            parsed.trusted_target("http://staging.example.test")

    def test_invalid_configurations_are_rejected(self) -> None:
        cases = {
            "http control plane": config(control_plane={"url": "http://app.example.test"}),
            "unknown key": config(extra=1),
            "no targets": config(targets=[]),
            "bad zone": config(
                targets=[{"origin": "https://a.test", "zone": "dmz", "credentials_file": "c"}]
            ),
            "public http": config(
                targets=[{"origin": "http://a.test", "zone": "public", "credentials_file": "c"}]
            ),
            "no credential source": config(
                targets=[{"origin": "https://a.test", "zone": "public"}]
            ),
            "duplicate": config(
                targets=[
                    {"origin": "https://a.test", "zone": "public", "credentials_file": "c"},
                    {"origin": "https://a.test:443", "zone": "public", "credentials_file": "c"},
                ]
            ),
            "bad env name": config(
                targets=[
                    {"origin": "https://a.test", "zone": "public", "credentials_env_prefix": "x-"}
                ]
            ),
        }
        for name, document in cases.items():
            with self.subTest(case=name), self.assertRaises(RunnerConfigError):
                parse_runner_config(document, base_dir=Path("/x"))

    def test_plain_http_to_a_private_host_needs_an_explicit_flag(self) -> None:
        plane = {"url": "http://web:8000", "allow_plain_http": True}
        parsed = parse_runner_config(config(control_plane=plane), base_dir=Path("/x"))
        self.assertEqual(parsed.control_plane_url, "http://web:8000")
        with self.assertRaises(RunnerConfigError):
            parse_runner_config(config(control_plane={**plane, "allow_plain_http": "yes"}),
                                base_dir=Path("/x"))  # fmt: skip

    def test_loopback_http_control_plane_is_allowed_for_development(self) -> None:
        parsed = parse_runner_config(
            config(control_plane={"url": "http://127.0.0.1:8000/"}), base_dir=Path("/x")
        )
        self.assertEqual(parsed.control_plane_url, "http://127.0.0.1:8000")

    def test_credentials_come_from_the_local_file_and_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "creds.json").write_text(json.dumps({"ana": "key-ana-0123456789"}), "utf-8")
            Path(tmp, "runner.toml").write_text(
                '[[targets]]\norigin = "http://127.0.0.1:8800"\nzone = "loopback"\n'
                'credentials_file = "creds.json"\ncredentials_env_prefix = "AG_CRED_"\n',
                "utf-8",
            )
            parsed = load_runner_config(Path(tmp, "runner.toml"))
            target = parsed.targets[0]
            environ = {credential_env_name("AG_CRED_", "cleo.ops"): "key-cleo-0123456789"}
            self.assertEqual(credential_env_name("AG_CRED_", "cleo.ops"), "AG_CRED_CLEO_OPS")
            found = load_credentials(target, ["ana", "cleo.ops", "dev"], environ)
        self.assertEqual(sorted(found), ["ana", "cleo.ops"])
        self.assertEqual(found["ana"].reveal(), "key-ana-0123456789")

    def test_unreadable_credentials_never_echo_their_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "creds.json").write_text('{"ana": "key-ana-secret-value"', "utf-8")
            parsed = parse_runner_config(
                {"targets": [{"origin": "http://127.0.0.1:1", "zone": "loopback",
                              "credentials_file": "creds.json"}]},
                base_dir=Path(tmp),
            )  # fmt: skip
            with self.assertRaises(RunnerConfigError) as raised:
                load_credentials(parsed.targets[0], ["ana"], {})
        self.assertNotIn("secret-value", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
