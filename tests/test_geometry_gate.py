"""The geometry gate runs the checks in its own interpreter and writes a receipt only on green;
a browser test leaves its own receipt when it passes, and is skipped while that receipt holds."""

import sys
from pathlib import Path
from types import ModuleType

import pytest
from pydantic import JsonValue, TypeAdapter

from scripts import geometry_gate
from tests.browser import receipts

pytest_plugins = ("pytester",)


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


# The plugin as the browser tests' conftest loads it, with the gate's --browser-force flag.
_CONFTEST = """
from tests.browser.receipts import pytest_collection_modifyitems, pytest_runtest_makereport


def pytest_addoption(parser):
    parser.addoption("--browser-force", action="store_true")
"""
# A walk that passes, one that fails, one whose page error fails its teardown, and a plain test.
_WALKS = """
import pytest


@pytest.fixture
def page_error():
    yield
    pytest.fail("uncaught error on the page")


@pytest.mark.browser
def test_green():
    pass


@pytest.mark.browser
def test_red():
    assert False


@pytest.mark.browser
def test_page_error(page_error):
    pass


def test_not_a_walk():
    pass
"""


@pytest.fixture
def walks(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """The walks above over a tree whose fingerprint is fixed, receipted under ``tmp_path``."""
    monkeypatch.setattr(geometry_gate, "WALK_RECEIPTS", tmp_path / "walks")
    monkeypatch.setattr(geometry_gate, "fingerprint", lambda: "tree")
    monkeypatch.setattr(receipts, "available", lambda: True)
    _ = pytester.makeconftest(_CONFTEST)
    _ = pytester.makepyfile(test_walks=_WALKS)
    return tmp_path / "walks"


_RECEIPT = TypeAdapter(dict[str, JsonValue])


def _receipted(root: Path) -> list[str]:
    return sorted(str(_RECEIPT.validate_json(p.read_bytes())["test"]) for p in root.rglob("*.json"))


def test_only_a_browser_test_that_passed_every_phase_leaves_a_receipt(
    pytester: pytest.Pytester, walks: Path
) -> None:
    pytester.runpytest("-p", "no:cacheprovider").assert_outcomes(passed=3, failed=1, errors=1)
    assert _receipted(walks) == ["test_walks.py::test_green"]


def test_a_receipted_browser_test_is_skipped_until_the_tree_changes_or_the_run_is_forced(
    pytester: pytest.Pytester, walks: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = pytester.runpytest("-p", "no:cacheprovider")
    again = pytester.runpytest("-p", "no:cacheprovider")
    again.assert_outcomes(passed=2, skipped=1, failed=1, errors=1)
    forced = pytester.runpytest("-p", "no:cacheprovider", "--browser-force")
    forced.assert_outcomes(passed=3, failed=1, errors=1)
    monkeypatch.setattr(geometry_gate, "fingerprint", lambda: "changed")
    pytester.runpytest("-p", "no:cacheprovider").assert_outcomes(passed=3, failed=1, errors=1)
    assert walks.joinpath("changed").is_dir()


@pytest.mark.usefixtures("walks")
def test_without_firefox_no_browser_test_is_skipped_on_a_receipt(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = pytester.runpytest("-p", "no:cacheprovider")
    monkeypatch.setattr(receipts, "available", lambda: False)
    # The rig's own fixture then fails the test; a gate never skips for want of its tool.
    pytester.runpytest("-p", "no:cacheprovider").assert_outcomes(passed=3, failed=1, errors=1)
