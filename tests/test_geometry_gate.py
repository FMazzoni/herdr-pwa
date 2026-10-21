"""The geometry gate runs the checks in its own interpreter and writes a receipt only on green;
a browser test leaves its own receipt when it passes, and is reused while that receipt holds."""

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
    assert geometry_gate.run("abc", geometry_gate.GateConfig(receipts=tmp_path)) == code
    # An explicit empty argv: the gate's own flags (--force) are not the checks' flags.
    assert calls == [[]]
    assert (tmp_path / "abc.json").is_file() is receipted


def test_an_edit_under_render_changes_the_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A renderer decides what a page draws, so its edit must not reuse the receipts before it."""
    monkeypatch.setattr(geometry_gate, "version", lambda: "Mozilla Firefox 1.0")
    # The tree is the repo's own; a copy holding one renderer stands in for it.
    monkeypatch.setattr(geometry_gate, "ROOT", tmp_path)
    source = tmp_path / "src/herdr_pwa/render/markdown.py"
    source.parent.mkdir(parents=True)
    _ = source.write_text('HEADING = "h2"\n')
    before = geometry_gate.fingerprint()
    _ = source.write_text('HEADING = "h1"\n')
    assert geometry_gate.fingerprint() != before


@pytest.mark.parametrize(
    "name",
    [
        "src/herdr_pwa/render/markdown.py",
        "src/herdr_pwa/files/view.py",
        "tests/conftest.py",
        "tests/fixtures/ledger.json",
        "pyproject.toml",
        "uv.lock",
    ],
)
def test_the_fingerprint_guards_what_a_page_is_built_from(name: str) -> None:
    assert geometry_gate.ROOT / name in geometry_gate.guarded_files()


# The plugin as the browser tests' conftest loads it, with the gate's --browser-force flag and
# its receipts under RECEIPTS, which the fixture fills in.
_CONFTEST = """
from pathlib import Path

from scripts.geometry_gate import GateConfig
from tests.browser.receipts import (
    GATE,
    pytest_collection_modifyitems,
    pytest_runtest_makereport,
    pytest_runtest_protocol,
)
from tests.browser.reused import pytest_report_teststatus


def pytest_addoption(parser):
    parser.addoption("--browser-force", action="store_true")


def pytest_configure(config):
    config.stash[GATE] = GateConfig(receipts=Path(RECEIPTS))
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
    monkeypatch.setattr(geometry_gate, "fingerprint", lambda: "tree")
    monkeypatch.setattr(receipts, "available", lambda: True)
    _ = pytester.makeconftest(_CONFTEST.replace("RECEIPTS", repr(str(tmp_path))))
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


def test_a_receipted_browser_test_is_reused_until_the_tree_changes_or_the_run_is_forced(
    pytester: pytest.Pytester, walks: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = pytester.runpytest("-p", "no:cacheprovider")
    again = pytester.runpytest("-p", "no:cacheprovider")
    # Reuse is its own word in the summary, never a skip: the green test is not run again.
    again.assert_outcomes(passed=2, failed=1, errors=1)
    assert again.parseoutcomes()["reused"] == 1
    forced = pytester.runpytest("-p", "no:cacheprovider", "--browser-force")
    forced.assert_outcomes(passed=3, failed=1, errors=1)
    monkeypatch.setattr(geometry_gate, "fingerprint", lambda: "changed")
    pytester.runpytest("-p", "no:cacheprovider").assert_outcomes(passed=3, failed=1, errors=1)
    assert walks.joinpath("changed").is_dir()


@pytest.mark.usefixtures("walks")
def test_without_firefox_no_browser_test_is_reused_on_a_receipt(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = pytester.runpytest("-p", "no:cacheprovider")
    monkeypatch.setattr(receipts, "available", lambda: False)
    # The rig's own fixture then fails the test; a gate never passes for want of its tool.
    pytester.runpytest("-p", "no:cacheprovider").assert_outcomes(passed=3, failed=1, errors=1)
