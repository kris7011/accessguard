"""Contract tests: OllamaModelClient against a local fake Ollama HTTP server (no real model).

The fake implements the documented shapes of POST /api/chat, GET /api/tags and
GET /api/version (https://docs.ollama.com/api). It proves the adapter's wire format,
parsing, limits and error handling; it says nothing about any real model's behaviour.
"""

from __future__ import annotations

import json
import threading
import time
import unittest
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler
from typing import Any

from accessguard.adapters.ollama_client import OllamaModelClient
from accessguard.adapters.scripted_model import in_order_responder
from accessguard.application.agent import AgentLimits
from accessguard.application.gate import RunLimits
from accessguard.application.planning import DeterministicPlanner
from accessguard.application.ports import ModelMessage, ModelRequest, RequestLimit
from accessguard.application.scan import AgentStrategy
from accessguard.domain.errors import ModelError
from accessguard_lab import ground_truth
from accessguard_lab.dataset import DOCUMENTS
from accessguard_lab.profiles import PROFILES
from accessguard_lab.scan import load_lab_policy, scan_lab
from accessguard_lab.server import QuietHTTPServer

DIGEST = "sha256:" + "ab" * 32
Reply = tuple[int, object]
ChatHandler = Callable[[dict[str, Any]], Reply]


def envelope(content: str, **extra: object) -> dict[str, object]:
    return {
        "model": "fake-model:latest",
        "created_at": "2026-10-26T10:00:00Z",
        "message": {"role": "assistant", "content": content},
        "done": True,
        "done_reason": "stop",
        "prompt_eval_count": 120,
        "eval_count": 9,
        "total_duration": 1_000_000,
        **extra,
    }


class FakeOllama:
    def __init__(self, chat: ChatHandler) -> None:
        self.chat = chat
        self.version: Reply = (200, {"version": "0.0.0-fake"})
        self.tags: Reply = (
            200,
            {
                "models": [
                    {
                        "name": "fake-model:latest",
                        "model": "fake-model:latest",
                        "modified_at": "2026-10-01T00:00:00Z",
                        "size": 1,
                        "digest": DIGEST,
                        "details": {"format": "gguf", "family": "fake", "parameter_size": "1B",
                                    "quantization_level": "Q4_0"},
                    }
                ]
            },
        )  # fmt: skip
        self.requests: list[tuple[str, str, bytes]] = []
        self.lock = threading.Lock()

    def respond(self, method: str, path: str, body: bytes) -> Reply:
        with self.lock:
            self.requests.append((method, path, body))
        if (method, path) == ("GET", "/api/version"):
            return self.version
        if (method, path) == ("GET", "/api/tags"):
            return self.tags
        if (method, path) == ("POST", "/api/chat"):
            return self.chat(json.loads(body))
        return 404, {"error": "not found"}


@contextmanager
def fake_ollama(chat: ChatHandler) -> Iterator[tuple[FakeOllama, int]]:
    fake = FakeOllama(chat)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            """Keep test output quiet."""

        def _respond(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            status, body = fake.respond(self.command, self.path, self.rfile.read(length))
            payload = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = _respond  # noqa: N815
        do_POST = _respond  # noqa: N815

    server = QuietHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.daemon = True
    thread.start()
    try:
        yield fake, server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


REQUEST = ModelRequest(
    messages=(ModelMessage("system", "rules"), ModelMessage("user", '{"task": "x"}')),
    response_schema={"type": "object"},
    max_output_tokens=32,
)


def fixed(reply: Reply) -> ChatHandler:
    return lambda payload: reply


def timed(function: Callable[[], object]) -> tuple[object, float]:
    started = time.monotonic()
    try:
        return function(), time.monotonic() - started
    except Exception as error:  # returned for assertions
        return error, time.monotonic() - started


class OllamaWireFormatTests(unittest.TestCase):
    def test_chat_request_shape_and_reply_parsing(self) -> None:
        with fake_ollama(fixed((200, envelope('{"action":"stop"}', unknown_field="x")))) as (
            fake,
            port,
        ):
            client = OllamaModelClient("fake-model", port=port, seed=7, num_ctx=4096)
            reply = client.complete(REQUEST, RequestLimit(5.0))
        method, path, body = fake.requests[-1]
        self.assertEqual((method, path), ("POST", "/api/chat"))
        payload = json.loads(body)
        self.assertEqual(payload["model"], "fake-model")
        self.assertIs(payload["stream"], False)
        self.assertEqual(payload["format"], {"type": "object"})
        self.assertEqual(
            payload["options"], {"temperature": 0.0, "seed": 7, "num_ctx": 4096, "num_predict": 32}
        )
        self.assertEqual(
            payload["messages"],
            [{"role": "system", "content": "rules"}, {"role": "user", "content": '{"task": "x"}'}],
        )
        self.assertEqual(reply.content, '{"action":"stop"}')
        self.assertEqual(
            dict(reply.provider_metadata),
            {
                "model": "fake-model:latest",
                "done_reason": "stop",
                "prompt_eval_count": 120,
                "eval_count": 9,
                "total_duration": 1_000_000,
            },
        )
        self.assertGreaterEqual(reply.latency_ms, 0.0)

    def test_without_a_schema_json_mode_is_requested(self) -> None:
        with fake_ollama(fixed((200, envelope("{}")))) as (fake, port):
            request = ModelRequest(messages=(ModelMessage("user", "x"),))
            OllamaModelClient("fake-model", port=port).complete(request, RequestLimit(5.0))
        payload = json.loads(fake.requests[-1][2])
        self.assertEqual(payload["format"], "json")
        self.assertNotIn("num_predict", payload["options"])

    def test_metadata_with_wrong_types_is_not_recorded(self) -> None:
        reply = envelope("{}", eval_count="9", prompt_eval_count=True, done_reason=None)
        with fake_ollama(fixed((200, reply))) as (_, port):
            result = OllamaModelClient("fake-model", port=port).complete(REQUEST, RequestLimit(5))
        self.assertNotIn("eval_count", result.provider_metadata)
        self.assertNotIn("prompt_eval_count", result.provider_metadata)
        self.assertNotIn("done_reason", result.provider_metadata)

    def test_identify_records_only_what_the_provider_reports(self) -> None:
        with fake_ollama(fixed((200, envelope("{}")))) as (fake, port):
            reported = OllamaModelClient("fake-model", port=port).identify(RequestLimit(5.0))
            self.assertEqual(
                dict(reported),
                {
                    "ollama_version": "0.0.0-fake",
                    "digest": DIGEST,
                    "modified_at": "2026-10-01T00:00:00Z",
                    "format": "gguf",
                    "family": "fake",
                    "parameter_size": "1B",
                    "quantization_level": "Q4_0",
                },
            )
            # A model that is not listed gets no digest; nothing is invented.
            missing = OllamaModelClient("other-model", port=port).identify(RequestLimit(5.0))
            self.assertEqual(dict(missing), {"ollama_version": "0.0.0-fake"})
            fake.version = (200, {"unexpected": True})
            fake.tags = (200, {"models": "not a list"})
            self.assertEqual(
                dict(OllamaModelClient("fake-model", port=port).identify(RequestLimit(5.0))), {}
            )
            fake.version = (404, {"error": "not found"})
            with self.assertRaises(ModelError):
                OllamaModelClient("fake-model", port=port).identify(RequestLimit(5.0))


class OllamaFailureTests(unittest.TestCase):
    def assert_model_error(self, reply: Reply, expected: str) -> None:
        with fake_ollama(fixed(reply)) as (_, port), self.assertRaises(ModelError) as raised:
            OllamaModelClient("fake-model", port=port).complete(REQUEST, RequestLimit(5.0))
        self.assertIn(expected, str(raised.exception))

    def test_provider_errors_are_reported_not_hidden(self) -> None:
        self.assert_model_error((500, {"error": "the model failed\n"}), "HTTP 500")
        self.assert_model_error((404, {"error": "model 'x' not found"}), "model 'x' not found")
        self.assert_model_error((200, b"not json"), "not valid JSON")
        self.assert_model_error((200, []), "not a JSON object")
        self.assert_model_error((200, {"done": True}), "message.content")
        self.assert_model_error((200, {"message": {"content": 42}}), "message.content")

    def test_unreachable_server_is_a_model_error(self) -> None:
        with fake_ollama(fixed((200, {}))) as (_, port):
            pass  # the port is closed again
        with self.assertRaises(ModelError):
            OllamaModelClient("fake-model", port=port).complete(REQUEST, RequestLimit(2.0))

    def test_oversized_reply_is_rejected(self) -> None:
        self.assert_model_error((200, envelope("x" * 300_000)), "limit")

    def test_slow_model_is_bounded_by_the_time_limit(self) -> None:
        def slow(payload: dict[str, Any]) -> Reply:
            time.sleep(1.5)
            return 200, envelope("{}")

        with fake_ollama(slow) as (_, port):
            client = OllamaModelClient("fake-model", port=port)
            result, elapsed = timed(lambda: client.complete(REQUEST, RequestLimit(0.3)))
        self.assertIsInstance(result, ModelError)
        self.assertLess(elapsed, 1.2)

    def test_in_flight_model_call_is_cancellable(self) -> None:
        def slow(payload: dict[str, Any]) -> Reply:
            time.sleep(1.5)
            return 200, envelope("{}")

        cancel = threading.Event()
        with fake_ollama(slow) as (_, port):
            client = OllamaModelClient("fake-model", port=port)
            threading.Timer(0.2, cancel.set).start()
            result, elapsed = timed(lambda: client.complete(REQUEST, RequestLimit(10.0, cancel)))
        self.assertIsInstance(result, ModelError)
        self.assertIn("cancelled", str(result))
        self.assertLess(elapsed, 1.0)

    def test_invalid_configuration(self) -> None:
        for kwargs in ({"port": 0}, {"port": 70_000}, {"temperature": 3}, {"num_ctx": 1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ModelError):
                OllamaModelClient("fake-model", **kwargs)  # type: ignore[arg-type]
        for name in ("", "bad name", "x" * 200, "../etc"):
            with self.subTest(name=name), self.assertRaises(ModelError):
                OllamaModelClient(name)


def first_pending_chat(payload: dict[str, Any]) -> Reply:
    """Fake model: the first pending check, computed from the state like a responder."""
    request = ModelRequest(
        messages=tuple(ModelMessage(m["role"], m["content"]) for m in payload["messages"])
    )
    return 200, envelope(in_order_responder(request))


def agent_scan(port: int, **limits: Any) -> dict[str, Any]:
    agent_limits = AgentLimits(**limits)
    return scan_lab(
        profile="vulnerable",
        flaws=PROFILES["vulnerable"],
        policy=load_lab_policy("lab"),
        planner=AgentStrategy(OllamaModelClient("fake-model", port=port), agent_limits),
        limits=RunLimits(),
    ).report


class AgentOverOllamaContractTests(unittest.TestCase):
    """The agent loop driven over real HTTP by a fake Ollama server, against the lab."""

    def test_complete_run_records_provenance_and_matches_ground_truth(self) -> None:
        with fake_ollama(first_pending_chat) as (fake, port):
            report = agent_scan(port)
        agent = report["run"]["agent"]
        self.assertEqual(agent["status"], "completed")
        self.assertTrue(report["run"]["planner"]["uses_ai"])
        self.assertEqual(agent["provider_metadata"]["digest"], DIGEST)
        self.assertEqual(agent["model_calls"], 56)
        self.assertEqual(agent["steps"][0]["provider_metadata"]["eval_count"], 9)
        self.assertTrue(ground_truth.compare(report, PROFILES["vulnerable"]).exact)
        baseline = scan_lab(
            profile="vulnerable",
            flaws=PROFILES["vulnerable"],
            policy=load_lab_policy("lab"),
            planner=DeterministicPlanner(),
            limits=RunLimits(),
        ).report
        outcomes = {r["check_id"]: r["outcome"] for r in report["results"]}
        self.assertEqual(outcomes, {r["check_id"]: r["outcome"] for r in baseline["results"]})
        sent = b"".join(body for _, _, body in fake.requests).decode()
        for document in DOCUMENTS:
            self.assertNotIn(document.canary, sent)
            self.assertNotIn(document.title, sent)
        self.assertNotIn("Bearer", sent)
        self.assertNotIn("/documents", sent)

    def test_provider_failure_mid_run_gives_a_partial_report(self) -> None:
        calls: list[int] = []

        def failing(payload: dict[str, Any]) -> Reply:
            calls.append(1)
            if len(calls) == 3:
                return 500, {"error": "out of memory"}
            return first_pending_chat(payload)

        with fake_ollama(failing) as (_, port):
            report = agent_scan(port)
        agent = report["run"]["agent"]
        self.assertEqual(agent["status"], "model_failed")
        self.assertIn("out of memory", agent["failure"])
        self.assertEqual(report["run"]["interrupted"], "model failure ended the agent run")
        self.assertEqual(report["coverage"]["executed_checks"], 2)
        self.assertEqual(report["summary"]["SKIPPED"], 54)
        self.assertFalse(report["run"]["completed"])
        self.assertNotEqual(report["status"]["overall"], "checks_passed")

    def test_model_timeout_is_reported_as_model_failure(self) -> None:
        def slow(payload: dict[str, Any]) -> Reply:
            time.sleep(1.5)
            return first_pending_chat(payload)

        with fake_ollama(slow) as (_, port):
            report, elapsed = timed(lambda: agent_scan(port, model_timeout_seconds=0.3))
        assert isinstance(report, dict)
        self.assertLess(elapsed, 3.0)
        self.assertEqual(report["run"]["agent"]["status"], "model_failed")
        self.assertIn("time limit", report["run"]["agent"]["failure"])

    def test_stop_signal_cancels_an_in_flight_model_call(self) -> None:
        def slow(payload: dict[str, Any]) -> Reply:
            time.sleep(2.0)
            return first_pending_chat(payload)

        stop = threading.Event()
        with fake_ollama(slow) as (_, port):
            threading.Timer(0.3, stop.set).start()
            started = time.monotonic()
            report = scan_lab(
                profile="fixed",
                flaws=PROFILES["fixed"],
                policy=load_lab_policy("lab"),
                planner=AgentStrategy(OllamaModelClient("fake-model", port=port), AgentLimits()),
                limits=RunLimits(),
                stop=stop,
            ).report
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.5)
        self.assertEqual(report["run"]["interrupted"], "run cancelled by stop signal")
        self.assertEqual(report["run"]["agent"]["status"], "interrupted")
        self.assertEqual(report["summary"]["SKIPPED"], 56)


if __name__ == "__main__":
    unittest.main()
