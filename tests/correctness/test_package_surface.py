"""The package must actually be importable, and expose what it documents.

WHY THIS EXISTS HERE: the sibling project onewayglass shipped `src/onewayglass/` with no
`__init__.py`, so it installed as an implicit namespace package — `import onewayglass` succeeded
while exposing nothing, and `from onewayglass import ...` failed with "unknown location". The smoke
test passed and every real use broke.

fusegrid did not have that bug, but nothing was checking. These tests are the check, copied
deliberately rather than shared: a published package's import surface is part of the package, and a
shared test helper would not catch a packaging change in one repo.
"""

from __future__ import annotations

import importlib
import pathlib

import fusegrid


def test_it_is_a_real_package_not_a_namespace() -> None:
    """A namespace package has __file__ of None. It installs, imports, and exports nothing."""
    assert fusegrid.__file__ is not None, (
        "fusegrid installed as an implicit namespace package — src/fusegrid/__init__.py "
        "is missing, so the package exposes no public API"
    )


def test_the_init_file_exists_on_disk() -> None:
    root = pathlib.Path(fusegrid.__file__).parent
    assert (root / "__init__.py").is_file()


def test_every_name_in_all_is_actually_importable() -> None:
    """__all__ is a promise. An entry that does not resolve breaks `from fusegrid import *`."""
    missing = [name for name in fusegrid.__all__ if not hasattr(fusegrid, name)]
    assert missing == [], f"__all__ names that do not resolve: {missing}"


def test_the_documented_usage_from_the_docstring_runs() -> None:
    """The example in the module docstring is the first thing anyone tries."""
    from fusegrid import Ledger, MemoryStore, ModelPrice, Pricing

    ledger = Ledger(MemoryStore(), {"team": 1.00})
    pricing = Pricing({"m": ModelPrice(input_per_mtok=1.0, output_per_mtok=2.0)})

    # The whole product in four lines: price the worst case, reserve it atomically, and be refused
    # when the reservation would exceed the ceiling.
    cost = pricing.max_cost("m", input_tokens=1000, max_output_tokens=1000)
    first = ledger.reserve("team", cost)
    assert first.allowed

    over = ledger.reserve("team", 999.0)
    assert not over.allowed, "a reservation past the ceiling must be refused"


def test_py_typed_ships_so_consumers_get_types() -> None:
    """Without this marker mypy treats the package as untyped, whatever annotations it carries."""
    root = pathlib.Path(fusegrid.__file__).parent
    assert (root / "py.typed").is_file()


def test_the_version_is_not_a_placeholder() -> None:
    """0.0.0 published to PyPI is permanent — a version can never be re-uploaded."""
    import tomllib

    root = pathlib.Path(__file__).resolve().parents[2]
    data = tomllib.loads((root / "pyproject.toml").read_text())
    version = data["project"]["version"]
    assert version != "0.0.0", "refusing to publish a placeholder version"
    assert importlib.import_module("fusegrid") is fusegrid
