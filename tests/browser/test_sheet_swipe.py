"""The status row's sheet follows a finger down from its grabber or header, in headless Firefox.

A touch is sent as the pointer events a phone makes, each to the element the finger began on;
the browser's own scrolling and its compat click are not, so the click is sent by hand.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import JsonValue, TypeAdapter

from scripts.firefox import Marionette, phone_window
from scripts.geometry import HEIGHT, HTMX_SETTLED
from tests.browser.conftest import Served
from tests.catalogue import State, build, pick
from tests.waiting import eventually

pytestmark = pytest.mark.browser

WIDTH = 390
READY_SECONDS = 15.0
GRABBER = "#agent-tray .tray__grabber"
HEAD = "#agent-tray .tray__title"
STOP = "#agent-tray .tray__slot--stop .tray__arm"
KEY = "#agent-tray .tray__pad .tray__slot:not(.tray__slot--esc) button"
LIST = "#agent-tray .tray__group .sheet__heading"
ROW = "#agent-tray .tray__row"
# The ceiling on a settle, which runs well inside it, spring or close.
SETTLE_MS = 2000

# What a swipe reports, as JSON.
_FOUND = TypeAdapter(dict[str, JsonValue])

OPENED = f"return ({HTMX_SETTLED})() && !!document.querySelector(arguments[0]);"

# Down `moves` px from where the finger lands, one move per `frame` ms, then let go; what the
# sheet did as it was let go, what it did after the settle, and the compat click's fate.
SWIPE = r"""
const [from, moves, frame, settleMs, click] = arguments;
const done = arguments[arguments.length - 1];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const el = document.querySelector(from);
const sheet = el.closest("details");
const body = sheet.querySelector(".tray__body");
const r = el.getBoundingClientRect();
const x = r.left + r.width / 2, y = r.top + r.height / 2;
const send = (type, dy) => el.dispatchEvent(new PointerEvent(type, {
  bubbles: true, cancelable: true, composed: true, pointerId: 7, pointerType: "touch",
  isPrimary: true, clientX: x, clientY: y + dy, button: type === "pointermove" ? -1 : 0,
  buttons: type === "pointerup" ? 0 : 1,
}));
const shift = () => new DOMMatrix(getComputedStyle(body).transform).m42;
const before = { length: history.length, href: location.href };
const head = document.querySelector("#agent-tray .tray__head");
(async () => {
  send("pointerdown", 0);
  const followed = [];
  for (const dy of moves) {
    await wait(frame);
    send("pointermove", dy);
    followed.push(shift());
  }
  send("pointerup", moves[moves.length - 1] || 0);
  const openAtUp = sheet.open;
  // The click a phone sends after a touch, on what the finger began on.
  const clickTaken = click
    ? el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, detail: 1 }))
    : null;
  // Until the sheet has settled, closed or back at rest, under a ceiling.
  const started = Date.now();
  while (body.classList.contains("is-settling") && Date.now() - started < settleMs) {
    await wait(frame);
  }
  done({
    followed, openAtUp, clickTaken,
    open: sheet.open,
    shift: sheet.open ? shift() : 0,
    arming: head ? head.getAttribute("data-hp-arming") : null,
    sameHistory: history.length === before.length && location.href === before.href,
  });
})();
"""


@dataclass(frozen=True)
class Sheet:
    """The browser on one state's page with the row's sheet drawn open."""

    driver: Marionette

    def swipe(
        self, start: str, moves: list[int], frame: int, *, click: bool = False
    ) -> dict[str, JsonValue]:
        """Drag from ``start`` through ``moves`` (px down), ``frame`` ms apart; what happened."""
        return _FOUND.validate_python(
            self.driver.script_async(SWIPE, start, moves, frame, SETTLE_MS, click)
        )

    def js(self, body: str, *args: object) -> object:
        """Run ``body`` on the page."""
        return self.driver.script(body, *args)

    def reopen(self) -> None:
        """Tap the row, as a person would, and wait for the sheet."""
        self.js(f"document.querySelector({ROW!r}).click();")
        assert eventually(lambda: bool(self.js(OPENED, GRABBER)), READY_SECONDS)


def _open(firefox: Marionette, served: Served, state: State, home: Path) -> Iterator[Sheet]:
    rig = build(state, home)
    served.switch.mount(rig.http.app)
    first = firefox.handles()[0]
    phone_window(firefox, WIDTH, HEIGHT)
    try:
        firefox.send("WebDriver:Navigate", {"url": served.base + state.path})
        assert eventually(lambda: bool(firefox.script(OPENED, state.armed)), READY_SECONDS)
        firefox.script(state.act)
        assert eventually(lambda: bool(firefox.script(OPENED, state.ready)), READY_SECONDS)
        yield Sheet(firefox)
    finally:
        firefox.send("WebDriver:CloseWindow", {})
        firefox.send("WebDriver:SwitchToWindow", {"handle": first})
    errors = served.switch.drain()
    if errors:
        pytest.fail("\n".join(errors))


@pytest.fixture
def sheet(firefox: Marionette, served: Served, tmp_path: Path) -> Iterator[Sheet]:
    """The working agent's sheet, open."""
    yield from _open(firefox, served, pick(["chat-working-sheet"])[0], tmp_path)


@pytest.fixture
def armed(firefox: Marionette, served: Served, tmp_path: Path) -> Iterator[Sheet]:
    """The working agent's sheet, open with Stop armed."""
    yield from _open(firefox, served, pick(["chat-working-armed"])[0], tmp_path)


# Firefox under Marionette cannot be told Reduce Motion is on, so the page is told what it asks.
REDUCE_MOTION = r"""
const real = window.matchMedia.bind(window);
window.matchMedia = (query) => query.includes("prefers-reduced-motion: reduce")
  ? { matches: true, media: query, addEventListener() {}, removeEventListener() {} }
  : real(query);
"""

FLICK = [12, 40, 80, 130]
# Slow: about 0.1px per ms, nowhere near a flick.
CRAWL = [10, 20, 30, 40]
CRAWL_FRAME = 90


def test_a_flick_down_on_the_grabber_closes_the_sheet_after_a_settle_and_leaves_history(
    sheet: Sheet,
) -> None:
    found = sheet.swipe(GRABBER, FLICK, 16)
    assert found["openAtUp"] is True, "the sheet vanished at once rather than settling down"
    assert found["open"] is False
    assert found["sameHistory"] is True


def test_the_sheet_follows_a_finger_on_its_header_and_springs_back_short_of_the_distance(
    sheet: Sheet,
) -> None:
    found = sheet.swipe(HEAD, CRAWL, CRAWL_FRAME)
    # The drag is taken 10px down, past the slop, and from there the sheet moves with the finger.
    assert found["followed"] == [0, 10, 20, 30]
    assert found["open"] is True
    assert found["shift"] == 0


def test_a_slow_drag_past_the_distance_closes(sheet: Sheet) -> None:
    found = sheet.swipe(HEAD, list(range(20, 260, 20)), 50)
    assert found["open"] is False


def test_a_flick_back_up_after_a_long_drag_springs_back(sheet: Sheet) -> None:
    found = sheet.swipe(GRABBER, [40, 80, 120, 160, 200, 160, 110, 60], 16)
    assert found["open"] is True
    assert found["shift"] == 0


def test_an_armed_sheet_swiped_shut_opens_again_disarmed(armed: Sheet) -> None:
    found = armed.swipe(GRABBER, FLICK, 16)
    assert found["open"] is False
    armed.reopen()
    assert (
        armed.js(
            "return document.querySelector('#agent-tray .tray__head').getAttribute('data-hp-arming');"
        )
        == ""
    )


@pytest.mark.parametrize("start", [STOP, KEY], ids=["stop", "key"])
def test_a_drag_that_begins_on_a_control_never_ends_as_its_tap(sheet: Sheet, start: str) -> None:
    found = sheet.swipe(start, CRAWL, CRAWL_FRAME, click=True)
    assert found["clickTaken"] is False, "the click after the drag reached the control"
    assert found["arming"] == ""
    assert found["open"] is True


def test_a_drag_in_a_scrolled_list_scrolls_it_and_one_at_its_top_drags_the_sheet(
    sheet: Sheet,
) -> None:
    # A sheet taller than its room, scrolled a little way down.
    sheet.js(
        "const b = document.querySelector('#agent-tray .tray__body'); b.style.maxHeight = '160px'; b.scrollTop = 40;"
    )
    assert sheet.js("return document.querySelector('#agent-tray .tray__body').scrollTop;") == 40
    found = sheet.swipe(KEY, FLICK, 16)
    assert found["followed"] == [0, 0, 0, 0]
    assert found["open"] is True
    sheet.js("document.querySelector('#agent-tray .tray__body').scrollTop = 0;")
    upward = sheet.swipe(LIST, [-12, -40, -80], 16)
    assert upward["followed"] == [0, 0, 0]
    assert upward["open"] is True
    found = sheet.swipe(LIST, FLICK, 16)
    assert found["open"] is False


def test_under_reduce_motion_the_sheet_closes_at_once_without_the_spring(sheet: Sheet) -> None:
    sheet.js(REDUCE_MOTION)
    found = sheet.swipe(GRABBER, FLICK, 16)
    assert found["openAtUp"] is False
    assert found["sameHistory"] is True
