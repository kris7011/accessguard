"""The two assistant flows end to end: page, provider port, validation, review and usage record.

The same scenarios run against two providers:

* ``RecordedFlowTests`` (always, offline): a recording fake at the provider port.
* ``LiveAnthropicFlowTests`` (opt-in, **paid**): the real ``AnthropicProvider`` and the
  Claude API. Runs only with ``ACCESSGUARD_AI_LIVE=paid`` and a locally entered
  ``ANTHROPIC_API_KEY``; at most ``MAX_PAID_CALLS`` model calls per run. See
  docs/ai-live-verification.md.

Failure paths: invalid answers and refusals through the pages with the fake; a timeout
through the real SDK against a local server that never answers (offline); a rejected API
key against the real API (``ACCESSGUARD_AI_LIVE=free`` or ``paid``, no cost).
"""

from __future__ import annotations

import copy
import dataclasses
import json
import os
import socket
import sys
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar
from unittest import mock, skipUnless

from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils.html import escape

from accessguard_sampleapp.app import Variant
from accessguard_sampleapp.data import BILLING, PROJECTS
from accessguard_web.assistant.models import AssistantKind, AssistantOutcome, AssistantRun
from accessguard_web.assistant.providers import (
    ANTHROPIC_MODEL,
    AnthropicProvider,
    AssistantProvider,
    Attempt,
    Completion,
    ProviderError,
)
from accessguard_web.assistant.service import run_task
from accessguard_web.assistant.tasks import (
    EXPLAIN_PROMPT_VERSION,
    EXPLAIN_SCHEMA,
    EXPLAIN_SYSTEM,
    SETUP_PROMPT_VERSION,
    SETUP_SCHEMA,
    explain_user_message,
    validate_explanation,
)
from accessguard_web.scans.models import Finding
from accessguard_web.tests.support import (
    login,
    make_org,
    org_runner,
    sample_project,
    sample_runtime,
    scan_and_ingest,
)
from accessguard_web.tests.test_assistant import GOOD_EXPLANATION, OPENAPI, FakeProvider

LIVE = os.environ.get("ACCESSGUARD_AI_LIVE", "")
# Counts logical SDK calls (``messages.create``). The SDK's own retries (``max_retries=2``)
# and a server-side refusal fallback happen inside one call and are not counted here.
MAX_PAID_CALLS = 3
EVIDENCE = Path(__file__).resolve().parents[3] / ".local" / "ai-live" / "runs.jsonl"
# USD per million tokens (input, output), platform.claude.com/docs/en/about-claude/pricing,
# checked 2026-10-30. Only used to estimate the cost of a run in the evidence file.
# claude-opus-4-8 is where ``fallbacks: "default"`` routes cyber-category refusals.
PRICES = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-5-5": (2.0, 10.0),
}

# Values that exist in the OpenAPI document but must never reach the model.
SPEC_CANARIES = ("CANARY-SERVER-TOKEN-61", "CANARY-SPEC-KEY-27", "CANARY-EXAMPLE-NAME-93")
SPEC: dict[str, Any] = copy.deepcopy(OPENAPI)
SPEC["servers"] = [{"url": f"https://staging.example.test/?token={SPEC_CANARIES[0]}"}]
SPEC["components"]["securitySchemes"] = {
    "key": {"type": "apiKey", "in": "header", "name": "X-Api-Key", "x-example": SPEC_CANARIES[1]}
}
SPEC["components"]["schemas"]["Invoice"]["properties"]["customer_name"]["example"] = SPEC_CANARIES[
    2
]
SPEC["paths"]["/api/v2/invoices/{invoiceId}"]["get"]["description"] = (
    f"Call with X-Api-Key {SPEC_CANARIES[1]}."
)
SPEC_PATHS = {"/api/v2/me", "/api/v2/invoices", "/api/v2/invoices/{invoiceId}"}
INVOICE = "/api/v2/invoices/{invoiceId}"
UNUSED_KEY = "sk-ant-test-not-real"  # pragma: allowlist secret
INVALID_KEY = "sk-ant-api03-not-a-real-key"  # pragma: allowlist secret

SETUP_ANSWER = {
    "session_probe_path": "/api/v2/me",
    "subject_field": "email",
    "collections": ["/api/v2/invoices", "/admin"],
    "resources": [
        {
            "path": INVOICE,
            "category": "invoice",
            "protected_fields": ["data.customer_name", "data.number"],
            "reason": "The customer name and number identify one invoice.",
        }
    ],
}
HANDLER_CODE = """def billing(request, org):
    if org not in request.user.organisations:
        raise PermissionDenied
    return JsonResponse({"data": Billing.objects.get(org=org).as_dict()})
"""


def estimate_usd(attempts: Sequence[Attempt]) -> float | str:
    """List-price estimate of one call, each attempt at its own model's price.

    "unknown" when no usage was reported or a model has no price here, never a silent zero.
    It can overstate (a refusal before any output is billed only in some categories) and
    cannot see requests the SDK retried; the Console's cost page is authoritative.
    """
    total = 0.0
    for attempt in attempts:
        price = PRICES.get(attempt.model)
        if price is None:
            return "unknown"
        total += attempt.input_tokens * price[0] + attempt.output_tokens * price[1]
    return round(total / 1_000_000, 5) if attempts else "unknown"


class RecordingProvider:
    """Passes calls to a provider and keeps what crossed the port (the API key never does)."""

    def __init__(self, inner: AssistantProvider) -> None:
        self.inner = inner
        self.name = inner.name
        self.model = inner.model
        self.sends_data_to_third_party = inner.sends_data_to_third_party
        self.sent: list[str] = []
        self.attempts: list[tuple[Attempt, ...]] = []

    def complete_json(
        self, *, system: str, user: str, schema: dict[str, Any], max_tokens: int
    ) -> Completion:
        self.sent.append(f"{system}\n{user}\n{json.dumps(schema)}")
        try:
            completion = self.inner.complete_json(
                system=system, user=user, schema=schema, max_tokens=max_tokens
            )
        except ProviderError as error:
            self.attempts.append(error.attempts)
            raise
        self.attempts.append(completion.attempts)
        return completion


class FlowScenarios(TestCase):
    """The shared scenarios. Subclasses supply the provider and expose them as tests."""

    expected_provider: ClassVar[str]

    def make_provider(self) -> AssistantProvider:
        raise NotImplementedError

    def setUp(self) -> None:
        self.org, self.owner = make_org("Flows")
        self.org.ai_assistance_enabled = True
        self.org.save()
        runner, _ = org_runner(self.org)
        with sample_runtime(Variant.FLAWED) as (app, config):
            self.project = sample_project(self.org, app.origin)
            scan_and_ingest(self.project, self.owner, runner, config)
            self.secrets = [
                *app.api_keys.values(),
                *(billing.account_ref for billing in BILLING.values()),
                *(billing.plan_note for billing in BILLING.values()),
                *(project.budget_code for project in PROJECTS.values()),
                *(project.name for project in PROJECTS.values()),
                *SPEC_CANARIES,
            ]
        self.provider = RecordingProvider(self.make_provider())
        for target in (
            "accessguard_web.assistant.service.configured_provider",
            "accessguard_web.scans.views.configured_provider",
        ):
            patcher = mock.patch(target, return_value=self.provider)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.browser = login(self.owner)

    # --- checks ---------------------------------------------------------------------------

    def assert_recorded(self, run: AssistantRun, kind: str, prompt_version: str) -> None:
        """Provider, model, tokens, latency and outcome are stored for every request."""
        self.record(run)  # first, so a failed check still leaves its evidence
        self.assertEqual((run.kind, run.prompt_version), (kind, prompt_version))
        self.assertEqual(run.provider, self.expected_provider)
        self.assertTrue(run.model)
        self.assertGreater(run.latency_ms, 0)
        self.assertGreater(run.input_tokens or 0, 0)
        self.assertGreater(run.output_tokens or 0, 0)
        self.assertEqual(run.outcome, AssistantOutcome.OK, run.error)

    def assert_nothing_secret_was_sent(self) -> None:
        self.assertTrue(self.provider.sent, "the provider was called")
        for payload in self.provider.sent:
            for secret in self.secrets:
                self.assertNotIn(secret, payload)

    def record(self, run: AssistantRun) -> None:
        """Hook for the live run's evidence file."""

    # --- scenarios ------------------------------------------------------------------------

    def check_setup_suggestions_need_review(self) -> None:
        before = copy.deepcopy(self.project.config)
        response = self.browser.post(
            reverse("projects:import_openapi", args=[self.project.pk]),
            {"pasted": json.dumps(SPEC), "use_assistant": "on"},
        )
        run = AssistantRun.objects.get(kind=AssistantKind.SETUP_DRAFT)
        self.assert_recorded(run, AssistantKind.SETUP_DRAFT, SETUP_PROMPT_VERSION)
        self.assertContains(response, f"Suggestions by {run.model} ({SETUP_PROMPT_VERSION})")
        draft = self.browser.session[f"openapi_draft_{self.project.pk}"]
        suggested = [t for t in draft["templates"] if t.get("suggested_by_ai")]
        self.assertTrue(suggested, "the model suggested protected fields for a record template")
        for template in suggested:
            self.assertContains(response, escape(template["reason"]))
            self.assertLessEqual(set(template["protected_fields"]), set(template["fields"]))
        referenced = {draft["session_probe_path"]} | {c["path"] for c in draft["collections"]}
        referenced |= {t["path"] for t in draft["templates"]}
        self.assertLessEqual(referenced - {""}, SPEC_PATHS, "nothing outside the document")
        self.project.refresh_from_db()
        self.assertEqual(self.project.config, before, "a draft changes nothing")
        self.assertEqual(self.project.endpoint_templates, [])

        # The person accepts one template and declines the prefix, session check and lists.
        self.browser.post(
            reverse("projects:apply_openapi", args=[self.project.pk]), {"templates": [INVOICE]}
        )
        self.project.refresh_from_db()
        self.assertEqual([t["path"] for t in self.project.endpoint_templates], [INVOICE])
        self.assertEqual(self.project.config["target"], before["target"], "scope unchanged")
        self.assertEqual(self.project.config["policy"], before["policy"])
        self.assert_nothing_secret_was_sent()

    def check_finding_explanation_is_a_reviewed_draft(self) -> None:
        finding = Finding.objects.get(project=self.project, check_id="orbit-billing@ben")
        status = finding.status
        response = self.browser.post(
            reverse("scans:explain", args=[finding.pk]), {"code": HANDLER_CODE}, follow=True
        )
        run = AssistantRun.objects.get(kind=AssistantKind.EXPLAIN_FINDING)
        self.assert_recorded(run, AssistantKind.EXPLAIN_FINDING, EXPLAIN_PROMPT_VERSION)
        assert run.result is not None
        self.assertContains(response, "Review it before acting on it.")
        self.assertContains(response, f"Written by {run.model} ({EXPLAIN_PROMPT_VERSION})")
        self.assertContains(response, escape(run.result["summary"]))
        for step in run.result["fix_steps"]:
            self.assertContains(response, escape(step))
        finding.refresh_from_db()
        self.assertEqual(finding.status, status, "an explanation never changes the status")
        self.assertIn("<untrusted_code>", self.provider.sent[0])
        self.assertNotIn("reproduction", self.provider.sent[0])
        self.assert_nothing_secret_was_sent()


class TaskFake(FakeProvider):
    """Answers each task with its own reply or error."""

    def __init__(self, setup: dict[str, Any] | Exception, explain: dict[str, Any] | Exception):
        super().__init__(explain)
        self.setup, self.explain = setup, explain

    def complete_json(
        self, *, system: str, user: str, schema: dict[str, Any], max_tokens: int
    ) -> Completion:
        self.answer = self.setup if schema is SETUP_SCHEMA else self.explain
        return super().complete_json(system=system, user=user, schema=schema, max_tokens=max_tokens)


class RecordedFlowTests(FlowScenarios):
    expected_provider = "fake"
    setup_answer: ClassVar[dict[str, Any] | Exception] = SETUP_ANSWER
    explain_answer: ClassVar[dict[str, Any] | Exception] = GOOD_EXPLANATION

    def make_provider(self) -> AssistantProvider:
        return TaskFake(self.setup_answer, self.explain_answer)

    def test_setup_suggestions_need_review(self) -> None:
        self.check_setup_suggestions_need_review()

    def test_finding_explanation_is_a_reviewed_draft(self) -> None:
        self.check_finding_explanation_is_a_reviewed_draft()


class RecordedFailureTests(RecordedFlowTests):
    setup_answer: ClassVar[dict[str, Any] | Exception] = {**SETUP_ANSWER, "extra": "x"}
    explain_answer: ClassVar[dict[str, Any] | Exception] = ProviderError(
        "the AI provider declined this request"
    )

    def test_setup_suggestions_need_review(self) -> None:
        response = self.browser.post(
            reverse("projects:import_openapi", args=[self.project.pk]),
            {"pasted": json.dumps(SPEC), "use_assistant": "on"},
        )
        run = AssistantRun.objects.get()
        self.assertEqual((run.outcome, run.result), (AssistantOutcome.INVALID, None))
        self.assertContains(response, "The setup assistant failed: The answer was rejected")
        self.assertContains(response, "The draft below is rule-based.")
        draft = self.browser.session[f"openapi_draft_{self.project.pk}"]
        self.assertFalse(any(t.get("suggested_by_ai") for t in draft["templates"]))

    def test_finding_explanation_is_a_reviewed_draft(self) -> None:
        finding = Finding.objects.get(project=self.project, check_id="orbit-billing@ben")
        response = self.browser.post(reverse("scans:explain", args=[finding.pk]), follow=True)
        run = AssistantRun.objects.get()
        self.assertEqual((run.outcome, run.result), (AssistantOutcome.FAILED, None))
        self.assertEqual(run.error, "the AI provider declined this request")
        self.assertContains(
            response, "The assistant could not help this time: the AI provider declined"
        )
        self.assertNotContains(response, "Written by", msg_prefix="no stand-in explanation")


class CostEstimateTests(SimpleTestCase):
    def test_each_attempt_is_priced_at_its_model_and_an_unknown_price_is_not_zero(self) -> None:
        declined = Attempt("claude-opus-5-5", 535, 0)
        served = Attempt("claude-opus-4-8", 412, 264)
        self.assertEqual(estimate_usd([declined, served]), 0.0108)  # 535*4 + 412*5 + 264*25
        self.assertEqual(estimate_usd([declined, Attempt("claude-unlisted-1", 10, 0)]), "unknown")
        self.assertEqual(estimate_usd([]), "unknown", "no usage reported")


@contextmanager
def silent_server() -> Iterator[str]:
    """A local HTTP endpoint that accepts connections and never answers."""
    server = socket.create_server(("127.0.0.1", 0))
    held: list[socket.socket] = []
    stop = threading.Event()

    def accept() -> None:
        server.settimeout(0.1)
        while not stop.is_set():
            try:
                held.append(server.accept()[0])
            except TimeoutError:
                continue

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.getsockname()[1]}"
    finally:
        stop.set()
        thread.join()
        for connection in held:
            connection.close()
        server.close()


def anthropic_settings(key: str, timeout: float = 60.0) -> dict[str, Any]:
    return {
        "PROVIDER": "anthropic",
        "MODEL": "",
        "ANTHROPIC_API_KEY": key,
        "OLLAMA_PORT": 11434,
        "TIMEOUT_SECONDS": timeout,
    }


class RealAdapterFailureTests(TestCase):
    """The real adapter and SDK; the pages report each failure as it happened."""

    def setUp(self) -> None:
        self.org, self.owner = make_org("Failures")
        self.org.ai_assistance_enabled = True
        self.org.save()
        runner, _ = org_runner(self.org)
        with sample_runtime(Variant.FLAWED) as (app, config):
            self.project = sample_project(self.org, app.origin)
            scan_and_ingest(self.project, self.owner, runner, config)
        self.finding = Finding.objects.get(project=self.project, check_id="orbit-billing@ben")

    def explain(self) -> Any:
        return login(self.owner).post(reverse("scans:explain", args=[self.finding.pk]), follow=True)

    def test_a_timeout_is_reported_as_a_timeout(self) -> None:
        with (
            silent_server() as url,
            mock.patch.dict(os.environ, {"ANTHROPIC_BASE_URL": url}),
            override_settings(ASSISTANT=anthropic_settings(UNUSED_KEY, 0.3)),
        ):
            response = self.explain()
        run = AssistantRun.objects.get()
        self.assertEqual((run.provider, run.outcome, run.result), ("anthropic", "failed", None))
        self.assertEqual(run.error, "the AI provider did not answer within 0.3 seconds")
        self.assertGreaterEqual(run.latency_ms, 900, "three attempts of 0.3 s (two SDK retries)")
        self.assertContains(response, "did not answer within 0.3 seconds")
        self.assertNotContains(response, "Written by")

    @skipUnless(LIVE in {"free", "paid"}, "set ACCESSGUARD_AI_LIVE=free (real API, no cost)")
    def test_the_real_api_rejects_an_invalid_key_and_the_page_says_so(self) -> None:
        with override_settings(ASSISTANT=anthropic_settings(INVALID_KEY)):
            response = self.explain()
        run = AssistantRun.objects.get()
        self.assertEqual(
            (run.outcome, run.error), ("failed", "the AI provider rejected the API key")
        )
        self.assertContains(response, "rejected the API key")
        self.assertNotContains(response, "Written by")


@skipUnless(
    LIVE == "paid" and os.environ.get("ANTHROPIC_API_KEY"),
    "paid real-model test not enabled (set ACCESSGUARD_AI_LIVE=paid and ANTHROPIC_API_KEY)",
)
class LiveAnthropicFlowTests(FlowScenarios):
    """Real Claude calls, at most MAX_PAID_CALLS per run; evidence in .local/ai-live/."""

    expected_provider = "anthropic"
    calls: ClassVar[int] = 0
    requests: list[dict[str, Any]]

    def make_provider(self) -> AssistantProvider:
        provider = AnthropicProvider(
            os.environ["ANTHROPIC_API_KEY"],
            os.environ.get("ACCESSGUARD_AI_MODEL") or ANTHROPIC_MODEL,
            float(os.environ.get("ACCESSGUARD_AI_TIMEOUT_SECONDS", "120")),
        )
        sdk_create = provider._client.beta.messages.create
        self.requests = []

        def counted(**kwargs: Any) -> Any:
            if LiveAnthropicFlowTests.calls >= MAX_PAID_CALLS:
                raise AssertionError(f"budget guard: more than {MAX_PAID_CALLS} paid calls")
            LiveAnthropicFlowTests.calls += 1
            self.requests.append(kwargs)
            return sdk_create(**kwargs)

        patcher = mock.patch.object(provider._client.beta.messages, "create", side_effect=counted)
        patcher.start()
        self.addCleanup(patcher.stop)
        return provider

    def record(self, run: AssistantRun) -> None:
        """One line per call: the stored record plus every attempt behind it, priced."""
        attempts = self.provider.attempts[-1] if self.provider.attempts else ()
        entry = {
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "test": self.id().rsplit(".", 1)[-1],
            "provider": run.provider,
            "model": run.model,
            "prompt_version": run.prompt_version,
            "outcome": run.outcome,
            "error": run.error,
            "input_tokens": run.input_tokens,
            "output_tokens": run.output_tokens,
            "latency_ms": run.latency_ms,
            "attempts": [dataclasses.asdict(attempt) for attempt in attempts],
            "estimated_usd": estimate_usd(attempts),
        }
        EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
        with EVIDENCE.open("a", encoding="utf-8") as file:
            file.write(json.dumps(entry) + "\n")
        print(json.dumps(entry), file=sys.stderr)

    def assert_wire_request(self, schema: dict[str, Any]) -> None:
        """What the SDK actually sent: structured output, fallbacks, nothing secret."""
        self.assertEqual(len(self.requests), 1)
        sent = self.requests[0]
        self.assertEqual(sent["output_config"]["format"], {"type": "json_schema", "schema": schema})
        self.assertEqual(sent["fallbacks"], "default")
        wire = json.dumps(sent, default=str)
        for secret in [*self.secrets, os.environ["ANTHROPIC_API_KEY"]]:
            self.assertNotIn(secret, wire)

    def test_setup_suggestions_need_review(self) -> None:
        self.check_setup_suggestions_need_review()
        self.assert_wire_request(SETUP_SCHEMA)

    def test_finding_explanation_is_a_reviewed_draft(self) -> None:
        self.check_finding_explanation_is_a_reviewed_draft()
        self.assert_wire_request(EXPLAIN_SCHEMA)

    def test_a_cut_off_answer_is_reported_not_replaced(self) -> None:
        finding = Finding.objects.get(project=self.project, check_id="orbit-billing@ben")
        run = run_task(
            kind=AssistantKind.EXPLAIN_FINDING, organization=self.org, user=self.owner,
            system=EXPLAIN_SYSTEM, message=explain_user_message(finding.details, ""),
            schema=EXPLAIN_SCHEMA, validate=validate_explanation,
            prompt_version=EXPLAIN_PROMPT_VERSION, max_tokens=16, provider=self.provider,
            finding=finding,
        )  # fmt: skip
        self.record(run)
        self.assertGreater(run.latency_ms, 0)
        self.assertEqual((run.outcome, run.result), (AssistantOutcome.FAILED, None))
        self.assertEqual(run.error, "the AI answer was cut off before it was complete")
        self.assertGreater(run.output_tokens or 0, 0, "the cut-off answer's tokens are recorded")
        page = self.browser.get(reverse("scans:finding", args=[finding.pk]))
        self.assertNotContains(page, "Written by")
