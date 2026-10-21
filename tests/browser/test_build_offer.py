"""The "new build" offer is raised by a region that swaps itself out, in headless Firefox.

htmx fires afterRequest on the element that made the request, and an outerHTML swap has
already detached it; htmx 2 then fires it again on the nearest ancestor still in the page.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest

from scripts.firefox import Marionette, phone_window
from scripts.geometry import HEIGHT, HTMX_SETTLED
from tests.browser.conftest import Served
from tests.catalogue import build, pick
from tests.waiting import eventually

pytestmark = pytest.mark.browser

WIDTH = 390
READY_SECONDS = 15.0
DRAWN = f"return ({HTMX_SETTLED})() && !!document.querySelector('#feed-list .summary');"
# Every reply now carries a stamp other than the page's, as one from a restarted server would.
MOVED = r"""
const real = XMLHttpRequest.prototype.getResponseHeader;
XMLHttpRequest.prototype.getResponseHeader = function (name) {
  return name.toLowerCase() === "hp-build" ? "moved" : real.call(this, name);
};
window.hpOldList = document.getElementById("feed-list");
htmx.trigger(document.body, "hp:poke");
"""
OFFERED = r"""
const old = window.hpOldList;
const fresh = document.getElementById("feed-list");
return {
  swapped: !!fresh && fresh !== old && !document.body.contains(old),
  offered: !document.getElementById("hp-update").hidden,
};
"""


@pytest.fixture
def feed(firefox: Marionette, served: Served, tmp_path: Path) -> Iterator[Marionette]:
    """The empty feed, drawn, with its list region polled by an outerHTML swap."""
    state = pick(["feed-empty"])[0]
    served.switch.mount(build(state, tmp_path).http.app)
    first = firefox.handles()[0]
    phone_window(firefox, WIDTH, HEIGHT)
    try:
        firefox.send("WebDriver:Navigate", {"url": served.base + state.path})
        assert eventually(lambda: bool(firefox.script(DRAWN)), READY_SECONDS)
        yield firefox
    finally:
        firefox.send("WebDriver:CloseWindow", {})
        firefox.send("WebDriver:SwitchToWindow", {"handle": first})
    errors = served.switch.drain()
    if errors:
        pytest.fail("\n".join(errors))


def test_a_region_swapped_out_by_its_own_reply_still_raises_the_offer(feed: Marionette) -> None:
    assert feed.script("return document.getElementById('hp-update').hidden;") is True
    feed.script(MOVED)
    assert eventually(lambda: feed.script(OFFERED) == {"swapped": True, "offered": True}, 10.0), (
        feed.script(OFFERED)
    )
