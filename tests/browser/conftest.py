"""The browser rig as fixtures: one headless Firefox and one app server per session, and per test
a fresh app in its own phone window, whose uncaught page errors fail the test."""

import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

# The rig's modules import each other as top-level names, as they do when run as scripts.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from scripts.chatend import Tab, WalkCtx
from scripts.firefox import Marionette, available, browser, phone_window
from scripts.geometry import Switch, serve
from tests.browser.receipts import (
    pytest_collection_modifyitems,
    pytest_runtest_makereport,
)
from tests.catalogue import build

__all__ = ["pytest_collection_modifyitems", "pytest_runtest_makereport"]


@dataclass(frozen=True)
class Served:
    """The session's app server: its front door, pointed at each test's app, and its address."""

    switch: Switch
    base: str


@pytest.fixture(scope="session")
def served() -> Iterator[Served]:
    """One server for the session on an ephemeral loopback port, every whole page collecting errors."""
    switch = Switch()
    with serve(switch) as base:
        yield Served(switch, base)


@pytest.fixture(scope="session")
def firefox() -> Iterator[Marionette]:
    """One headless Firefox for the session, under the machine-wide browser lock."""
    # A gate that skips itself when its tool is missing is not a gate.
    if not available():
        pytest.fail("firefox is not on PATH; install it, the browser tests never skip")
    with browser() as driver:
        yield driver


@pytest.fixture
def ctx(firefox: Marionette, served: Served, tab: Tab, tmp_path: Path) -> Iterator[WalkCtx]:
    """A fresh app for ``tab`` in a phone window of its size; any uncaught page error fails."""
    rig = build(tab.state, tmp_path)
    served.switch.mount(rig.http.app)
    first = firefox.handles()[0]
    phone_window(firefox, tab.width, tab.height)
    try:
        yield WalkCtx(firefox, served.base, tab, rig, tmp_path)
    finally:
        firefox.send("WebDriver:CloseWindow", {})
        firefox.send("WebDriver:SwitchToWindow", {"handle": first})
    errors = served.switch.drain()
    if errors:
        pytest.fail("\n".join(errors))
