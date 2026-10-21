"""Reuse a browser test whose receipt exists for this exact tree, and leave one when it passes.

The tree's fingerprint is the geometry gate's, so a test is reused only while nothing that
decides what the browser lays out has changed since it last passed.
"""

from collections.abc import Generator

import pytest

from scripts import geometry_gate
from scripts.firefox import available
from tests.browser.reused import REUSED

BROWSER = "browser"
FORCE = "--browser-force"
_FINGERPRINT = pytest.StashKey[str]()
# Where the receipts go; a run that sets none uses the gate's own.
GATE = pytest.StashKey[geometry_gate.GateConfig]()
# Whether every phase so far passed, and how long the call took.
_CLEAN = pytest.StashKey[bool]()
_SECONDS = pytest.StashKey[float]()
# The tree a test's receipt holds for, set on a test that is reused rather than run.
_GREEN_FOR = pytest.StashKey[str]()


def _gate(config: pytest.Config) -> geometry_gate.GateConfig:
    return config.stash.get(GATE, geometry_gate.CONFIG)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Mark every browser test already green for this tree for reuse, unless forced."""
    walks = [item for item in items if item.get_closest_marker(BROWSER)]
    # A gate that passes itself when its tool is missing is not a gate: the fixture fails instead.
    if not walks or not available():
        return
    current = geometry_gate.fingerprint()
    config.stash[_FINGERPRINT] = current
    if config.getoption(FORCE, default=False):
        return
    for item in walks:
        if geometry_gate.walk_receipt(current, item.nodeid, _gate(config)).is_file():
            item.stash[_GREEN_FOR] = current


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_protocol(item: pytest.Item) -> bool | None:
    """Report a browser test green for this tree as reused, without running it."""
    green_for = item.stash.get(_GREEN_FOR, "")
    if not green_for:
        return None
    item.ihook.pytest_runtest_logstart(nodeid=item.nodeid, location=item.location)
    report = pytest.TestReport(
        item.nodeid,
        item.location,
        keywords={},
        outcome="passed",
        longrepr=None,
        when="call",
        user_properties=[(REUSED, green_for)],
    )
    item.ihook.pytest_runtest_logreport(report=report)
    item.ihook.pytest_runtest_logfinish(nodeid=item.nodeid, location=item.location)
    return True


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item,
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    """Leave a receipt once a browser test's setup, call and teardown have all passed."""
    report = yield
    current = item.config.stash.get(_FINGERPRINT, "")
    if not current or not item.get_closest_marker(BROWSER):
        return report
    clean = item.stash.get(_CLEAN, True) and report.passed
    item.stash[_CLEAN] = clean
    if report.when == "call":
        item.stash[_SECONDS] = report.duration
    # The fixture's teardown judges page errors, so a pass is only known after it.
    if report.when == "teardown" and clean:
        seconds = item.stash.get(_SECONDS, 0.0)
        geometry_gate.record_walk(current, item.nodeid, seconds, _gate(item.config))
    return report
