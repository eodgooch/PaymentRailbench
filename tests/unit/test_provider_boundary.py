"""The Ledger talks to the Simulator only through the Provider interface (ADR 0005): no
Ledger or Payments module imports the Simulator, and the Simulator imports neither. And the
Ledger never reads the Fault Manifest (SPEC §6, ADR 0011): only Scenario tooling may import
`railbench.scenarios`, which writes it."""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).parents[2] / "src" / "railbench"

FORBIDDEN = {
    "ledger": ("railbench.simulator", "railbench.scenarios"),
    "payments": ("railbench.simulator", "railbench.scenarios"),
    "common": (
        "railbench.simulator",
        "railbench.ledger",
        "railbench.payments",
        "railbench.scenarios",
    ),
    "simulator": ("railbench.ledger", "railbench.payments", "railbench.scenarios"),
}


def imported_modules(path: Path) -> set[str]:
    modules = set()
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if isinstance(node, ast.Import):
            modules |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module)
    return modules


@pytest.mark.parametrize("package", sorted(FORBIDDEN))
def test_packages_keep_to_their_side_of_the_provider_interface(package: str) -> None:
    files = sorted((SRC / package).rglob("*.py"))
    assert files
    crossings = {
        f"{path.relative_to(SRC)} imports {module}"
        for path in files
        for module in imported_modules(path)
        if module.startswith(FORBIDDEN[package])
    }

    assert crossings == set()


def test_relative_imports_are_not_used_to_cross() -> None:
    relative = [
        path.relative_to(SRC)
        for path in SRC.rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.ImportFrom) and node.level > 0
    ]

    assert relative == []
