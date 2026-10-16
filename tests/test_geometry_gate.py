"""The geometry gate runs the checks in its own interpreter and writes a receipt only on green."""

import sys
from pathlib import Path
from types import ModuleType

import pytest

from scripts import geometry_gate


def _geometry(code: int, calls: list[list[str]]) -> ModuleType:
    """A stand-in for scripts/geometry.py whose main() records its argv and answers ``code``."""
    module = ModuleType("geometry")

    def main(argv: list[str] | None = None) -> int:
        calls.append(list(argv) if argv is not None else ["<sys.argv>"])
        return code

    module.__dict__["main"] = main
    return module


@pytest.mark.parametrize(("code", "receipted"), [(0, True), (1, False)])
def test_the_checks_run_in_process_and_only_green_leaves_a_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int, receipted: bool
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setitem(sys.modules, "geometry", _geometry(code, calls))
    monkeypatch.setattr(geometry_gate, "RECEIPTS", tmp_path)
    assert geometry_gate.run("abc") == code
    # An explicit empty argv: the gate's own flags (--force) are not the checks' flags.
    assert calls == [[]]
    assert (tmp_path / "abc.json").is_file() is receipted
