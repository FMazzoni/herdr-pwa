import os
import sys
from collections.abc import Generator
from pathlib import Path
from typing import cast

import pytest

from herdr_pwa.writes import prompt, resolve

FIXTURES = Path(__file__).parent / "fixtures"
# Fixtures no test here has to open, each with its reason.
UNOPENED_FIXTURES: dict[str, str] = {
    "ledger.json": "the golden record keeps every row, used or not",
    "README.md": "for people",
    "foreign/collie/LICENSE": "collie's licence, kept beside its screens",
    "elapsed.json": "shared with the JS tests, which this hook cannot see",
    "transcript/lineage/README.md": "for people",
}
# `just check` sets HP_FIXTURE_USE=1, so only a full run fails on a fixture no test opens.
_FIXTURE_USE = os.environ.get("HP_FIXTURE_USE") == "1"
_OPENED: set[str] = set()
# A guard that reads every fixture to scrub it says nothing about which ones a test needs.
_SWEEPERS = frozenset({"test_fixture_hygiene.py"})
_SWEEPING: set[str] = set()
_PREFIX = f"{FIXTURES}{os.sep}"
_UNUSED = pytest.StashKey[list[str]]()


def _record_open(event: str, args: tuple[object, ...]) -> None:
    if event != "open" or not args or _SWEEPING:
        return
    target = args[0]
    if not isinstance(target, str | bytes | Path):
        return
    path = os.path.abspath(os.fsdecode(target))  # noqa: PTH100 - a hook on every open stays cheap
    if path.startswith(_PREFIX):
        _OPENED.add(path.removeprefix(_PREFIX))


if _FIXTURE_USE:
    sys.addaudithook(_record_open)


def _never_opened(opened: set[str]) -> list[str]:
    every = (path.relative_to(FIXTURES).as_posix() for path in FIXTURES.rglob("*"))
    files = {name for name in every if (FIXTURES / name).is_file()}
    return sorted(files - opened - UNOPENED_FIXTURES.keys())


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item: pytest.Item) -> Generator[None, object, object]:
    if item.path.name in _SWEEPERS:
        _SWEEPING.add(item.nodeid)
    try:
        return (yield)
    finally:
        _SWEEPING.discard(item.nodeid)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if not _FIXTURE_USE:
        return
    workeroutput: dict[str, object] | None = getattr(session.config, "workeroutput", None)
    if workeroutput is not None:
        workeroutput["fixtures_opened"] = sorted(_OPENED)
        return
    if exitstatus not in {pytest.ExitCode.OK, pytest.ExitCode.TESTS_FAILED}:
        return
    unused = _never_opened(_OPENED)
    if unused:
        session.config.stash[_UNUSED] = unused
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node: object, error: object) -> None:
    del error
    output = cast("dict[str, list[str]]", getattr(node, "workeroutput", {}))
    _OPENED.update(output.get("fixtures_opened", []))


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    unused = terminalreporter.config.stash.get(_UNUSED, None)
    if unused:
        terminalreporter.section("fixtures no test opened")
        for name in unused:
            terminalreporter.write_line(f"tests/fixtures/{name}")


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--browser-force",
        action="store_true",
        help="run the browser tests even where a receipt for this tree exists",
    )


@pytest.fixture(autouse=True)
def _isolated_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> Path:
    # No test may reach the real ~/.config, nor inherit the operator's HP_* settings.
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    for name in [key for key in os.environ if key.startswith("HP_")]:
        monkeypatch.delenv(name)
    # A started app warms its caches on a thread of its own; no test races one it did not ask for.
    monkeypatch.setenv("HP_PREWARM", "false")
    return home


@pytest.fixture(autouse=True)
def _fresh_write_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    # Which writes are in flight, and which may have landed, is per process; never across tests.
    monkeypatch.setattr(resolve, "IN_FLIGHT", resolve.InFlight())
    monkeypatch.setattr(prompt, "UNSURE", prompt.Unsure())
