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
    },
    "application": {
        "accessguard.adapters",
        "accessguard.entrypoints",
        "http",
        "socket",
        "urllib.request",
        "subprocess",
        "accessguard_lab",
    },
    "adapters": {"accessguard.entrypoints", "accessguard_lab"},
    "entrypoints": {"accessguard_lab"},
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
                self.assertNotIn("Flaw.", text)
                self.assertNotIn("demo-a", text)

    def test_model_planner_cannot_send_target_requests(self) -> None:
        modules = imported_modules(PRODUCT / "adapters" / "ollama_planner.py")
        self.assertNotIn("accessguard.adapters.http_target", modules)
        self.assertNotIn("accessguard.application.runner", modules)


if __name__ == "__main__":
    unittest.main()
