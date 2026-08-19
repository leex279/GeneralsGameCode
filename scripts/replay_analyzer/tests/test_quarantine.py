from __future__ import annotations

import ast
from pathlib import Path

ANALYZER_ROOT = Path(__file__).parents[1]


def _import_targets(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    targets: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            targets.add(node.module)
        elif (
            isinstance(node, ast.Call)
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and (
                (isinstance(node.func, ast.Name) and node.func.id == "__import__")
                or (
                    isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "importlib"
                    and node.func.attr == "import_module"
                )
            )
        ):
            targets.add(node.args[0].value)
    return targets


def test_prototype_is_quarantined_outside_package_and_test_import_graph() -> None:
    assert list(ANALYZER_ROOT.glob("*.py")) == []

    checked_files = sorted((ANALYZER_ROOT / "src").rglob("*.py")) + sorted((ANALYZER_ROOT / "tests").rglob("*.py"))
    violations = {
        str(path.relative_to(ANALYZER_ROOT)): sorted(
            target for target in _import_targets(path) if "legacy_prototype" in target.split(".")
        )
        for path in checked_files
    }
    assert {path: targets for path, targets in violations.items() if targets} == {}
