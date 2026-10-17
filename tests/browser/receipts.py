"""Skip a browser test whose receipt exists for this exact tree, and leave one when it passes.

The tree's fingerprint is the geometry gate's, so a test is skipped only while nothing that
decides what the browser lays out has changed since it last passed.
"""

from collections.abc import Generator

import pytest

from scripts import geometry_gate
from scripts.firefox import available

BROWSER = "browser"
FORCE = "--browser-force"
_FINGERPRINT = pytest.StashKey[str]()
# Whether every phase so far passed, and how long the call took.
_CLEAN = pytest.StashKey[bool]()
_SECONDS = pytest.StashKey[float]()


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip every browser test already green for this tree, unless forced."""
    walks = [item for item in items if item.get_closest_marker(BROWSER)]
    # A gate that skips itself when its tool is missing is not a gate: the fixture fails instead.
    if not walks or not available():
        return
    current = geometry_gate.fingerprint()
    config.stash[_FINGERPRINT] = current
    if config.getoption(FORCE, default=False):
        return
    for item in walks:
        if geometry_gate.walk_receipt(current, item.nodeid).is_file():
            item.add_marker(pytest.mark.skip(reason=f"browser green for {current}"))


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
        geometry_gate.record_walk(current, item.nodeid, item.stash.get(_SECONDS, 0.0))
    return report
