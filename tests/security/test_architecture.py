"""Static guards: dependency direction and separation of the vulnerable lab from the product."""

import ast
import unittest
from pathlib import Path

from tests.helpers import REPO_ROOT

PRODUCT = REPO_ROOT / "src" / "accessguard"

# Modules that would let a layer perform I/O or depend on an outer layer.
FORBIDDEN = {
    "domain": {
        "accessguard.application",
        "accessguard.adapters",
        "accessguard.entrypoints",
        "http",
        "socket",
        "urllib.request",
        "subprocess",
        "pathlib",
        "json",
        "accessguard_lab",
        "accessguard_eval",
        "accessguard_sampleapp",
    },
    "application": {
        "accessguard.adapters",
        "accessguard.entrypoints",
        "http",
        "socket",
        "urllib.request",
        "subprocess",
        "accessguard_lab",
        "accessguard_eval",
        "accessguard_sampleapp",
    },
    "adapters": {
        "accessguard.entrypoints",
        "accessguard_lab",
        "accessguard_eval",
        "accessguard_sampleapp",
    },
    "entrypoints": {"accessguard_lab", "accessguard_eval", "accessguard_sampleapp"},
}


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text("utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


class ArchitectureTests(unittest.TestCase):
    def test_dependencies_point_inwards(self) -> None:
        for layer, forbidden in FORBIDDEN.items():
            for path in (PRODUCT / layer).rglob("*.py"):
                for module in imported_modules(path):
                    with self.subTest(file=path.name, module=module):
                        self.assertFalse(
                            any(module == f or module.startswith(f + ".") for f in forbidden),
                            f"{path.relative_to(REPO_ROOT)} imports {module}",
                        )

    def test_product_contains_no_lab_code_or_fixed_tokens(self) -> None:
        for path in PRODUCT.rglob("*.py"):
            text = path.read_text("utf-8")
            with self.subTest(file=path.name):
                self.assertNotIn("SYNTHETIC-CANARY", text)
                self.assertNotIn("EVAL-CANARY", text)
                self.assertNotIn("answer_key", text)
                self.assertNotIn("Flaw.", text)
                self.assertNotIn("demo-a", text)

    def test_engine_has_no_rules_for_the_sample_app(self) -> None:
        """The integration is verified without special cases: no sample-app names or markers."""
        for path in PRODUCT.rglob("*.py"):
            text = path.read_text("utf-8")
            with self.subTest(file=path.name):
                for marker in ("sampleapp", "X-Api-Key", "SAMPLE-", "whoami", "orbit", "quasar"):
                    self.assertNotIn(marker, text)

    def test_sample_app_is_independent_of_accessguard(self) -> None:
        for path in (REPO_ROOT / "sampleapp" / "accessguard_sampleapp").rglob("*.py"):
            for module in imported_modules(path):
                with self.subTest(file=path.name, module=module):
                    self.assertFalse(module.startswith("accessguard."), module)

    def test_model_adapters_cannot_send_target_requests(self) -> None:
        for name in ("ollama_planner.py", "ollama_client.py", "scripted_model.py"):
            modules = imported_modules(PRODUCT / "adapters" / name)
            with self.subTest(adapter=name):
                self.assertNotIn("accessguard.adapters.http_target", modules)
                self.assertNotIn("accessguard.application.runner", modules)
                self.assertNotIn("accessguard.application.agent", modules)

    def test_agent_has_no_provider_or_transport_dependency(self) -> None:
        """The agent talks to models only through the ModelClient port."""
        for path in (PRODUCT / "application").glob("agent*.py"):
            modules = imported_modules(path)
            with self.subTest(file=path.name):
                self.assertFalse(any(m.startswith("accessguard.adapters") for m in modules))
                self.assertNotIn("accessguard.application.reporting", modules)

    def test_evaluation_strategies_never_load_the_answer_key(self) -> None:
        """Only the scoring step of the harness may read answer keys or flaw definitions."""
        harness = REPO_ROOT / "evaluations" / "accessguard_eval" / "harness.py"
        tree = ast.parse(harness.read_text("utf-8"))
        callers = {
            function.name
            for function in ast.walk(tree)
            if isinstance(function, ast.FunctionDef)
            for call in ast.walk(function)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "load_answer_key"
        }
        self.assertEqual(callers, {"evaluate"})
        run_once = next(
            f for f in ast.walk(tree) if isinstance(f, ast.FunctionDef) and f.name == "run_once"
        )
        names = {n.attr for n in ast.walk(run_once) if isinstance(n, ast.Attribute)}
        self.assertNotIn("flaws", names)
        self.assertIn("policy", names)


if __name__ == "__main__":
    unittest.main()
