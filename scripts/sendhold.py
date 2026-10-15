"""Drive one slow answer in headless Firefox: while the key is in flight, a stream event and a
redraw held back under the thumb both land, and the page must still send the key once, keep Back
and End disabled until the answer is back, and then show what the key did."""

import threading
import time
from collections.abc import Callable
from pathlib import Path

from firefox import Marionette, phone_window
from starlette.types import ASGIApp, Receive, Scope, Send

from herdr_pwa.jsontypes import JsonObject
from tests.catalogue import AGENT, PERMISSION, WORKING, State, build, capture, herd
from tests.webfakes import (
    AGENT_GET,
    READ,
    SEND_KEYS,
    SNAPSHOT,
    WORKING_PANE,
    FakeWire,
    Reply,
    agents_of,
    read_frame,
)

WIDTH = 390
HEIGHT = 844
WAIT_SECONDS = 15.0
# How long the runner sits on the key: several of app.js's 250ms idle waits, so a redraw held
# back under the thumb comes due while the key is still out.
SLOW_SECONDS = 2.0
OPTION = "#answer .button--option"

# Every change to the body's sending class, and every read that finished, stamped on one clock.
INSTRUMENT = r"""
window.__hp = { marks: [], reads: 0 };
const say = () => window.__hp.marks.push(
  [performance.now(), document.body.classList.contains("is-sending")]);
new MutationObserver(say).observe(document.body, { attributes: true, attributeFilter: ["class"] });
document.addEventListener("htmx:afterRequest", (e) => {
  if (e.detail.requestConfig && e.detail.requestConfig.verb === "get") window.__hp.reads += 1;
});
"""
READY = r"""
// A region still settling has not bound its triggers yet, so an event sent to it is lost.
const busy = document.querySelector(".htmx-request, .htmx-swapping, .htmx-settling");
return document.readyState === "complete" && !busy && !!document.querySelector(arguments[0]);
"""
# A finger on the glass, as iOS reports it; only the listeners in app.js read these.
TOUCH = r"""
const e = new Event(arguments[0], { bubbles: true });
Object.defineProperty(e, "touches", { value: arguments[0] === "touchstart" ? [{}] : [] });
document.dispatchEvent(e);
"""
STALE = "htmx.trigger(document.querySelector(arguments[0]), 'hp:stale');"
READS = "return window.__hp.reads;"
# A control a second tap could land on: any button in the region that is live right now.
LIVE = r"""
const live = [...document.querySelectorAll("#answer button")].filter((b) => !b.disabled);
const option = live.find((b) => b.matches(".button--option"));
if (option) option.click();
return { live: live.length, sending: document.body.classList.contains("is-sending") };
"""
OUTCOME = "return !!document.querySelector('#answer .banner[data-hp-sent]');"
GAPS = "return window.__hp.marks.filter(([t, on]) => t >= arguments[0] && !on).length;"


class Writes:
    """The real app behind a count of the key posts that reach it."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.posts = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Count a key post, then serve it."""
        if scope["type"] == "http" and scope["method"] == "POST":
            self.posts += str(scope["path"]).endswith("/keys")
        await self.app(scope, receive, send)


def agent_info(frame: JsonObject) -> JsonObject:
    """The ``agent.get`` reply for the agent the walk taps at, as ``frame`` has it."""
    agent = next(a for a in agents_of(frame) if a.get("pane_id") == WORKING_PANE)
    return {"id": "x", "result": {"type": "agent_info", "agent": agent}}


def asking() -> dict[str, Reply]:
    """The tapped agent blocked on the captured permission question, with a runner that takes keys."""
    frame = herd(p4="blocked")
    return {
        SNAPSHOT: frame,
        READ: read_frame(capture(PERMISSION)),
        AGENT_GET: agent_info(frame),
        SEND_KEYS: {"id": "x", "result": {"type": "ok"}},
    }


def slow_key(release: threading.Event) -> Callable[[FakeWire], None]:
    """The runner holds the key until released, then the question goes and the agent works."""

    def landed(wire: FakeWire) -> None:
        release.wait(WAIT_SECONDS)
        frame = herd(p4="working")
        wire.replies.update(
            {SNAPSHOT: frame, AGENT_GET: agent_info(frame), READ: read_frame(capture(WORKING))}
        )

    return landed


def _until(driver: Marionette, script: str, *args: object) -> bool:
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        if driver.script(script, *args):
            return True
        time.sleep(0.05)
    return False


def _number(driver: Marionette, script: str) -> int:
    found = driver.script(script)
    return found if isinstance(found, int) else 0


def _hold_back_a_redraw(driver: Marionette) -> list[str]:
    # A stream event under the thumb: its answer is held back until the reader is still.
    reads = _number(driver, READS)
    driver.script(TOUCH, "touchstart")
    driver.script(STALE, "#answer")
    if not _until(driver, f"return window.__hp.reads > {reads};"):
        return ["send hold: the answer region never answered its stream event"]
    driver.script(TOUCH, "touchend")
    return []


def _in_flight(driver: Marionette) -> tuple[bool, bool]:
    """Watch the slow key: (a live control was offered, Back and End were ever re-enabled)."""
    offered = released = False
    until = time.monotonic() + SLOW_SECONDS
    while time.monotonic() < until:
        seen = driver.script(LIVE)
        if isinstance(seen, dict):
            offered = offered or bool(seen.get("live"))
            released = released or not seen.get("sending")
        time.sleep(0.05)
    return offered, released


def _gaps(driver: Marionette, clicked: float) -> int:
    """Times the sending class came off since the tap; read before the release, so mid-send."""
    found = driver.script(GAPS, clicked)
    return found if isinstance(found, int) else 0


def walk(driver: Marionette, base: str, writes: Writes, release: threading.Event) -> list[str]:
    """Tap an option while the runner is slow, with a redraw and a stream event landing mid-send."""
    driver.send("WebDriver:Navigate", {"url": f"{base}{AGENT}?tab=pane"})
    if not _until(driver, READY, OPTION):
        return ["send hold: the question never drew its options"]
    driver.script(INSTRUMENT)
    problems = _hold_back_a_redraw(driver)
    if problems:
        return problems
    found = driver.script(
        "document.querySelector(arguments[0]).click(); return performance.now();", OPTION
    )
    clicked = float(found) if isinstance(found, int | float) else 0.0
    # A stream event for another region lands while the key is out.
    driver.script(STALE, "#agent-view")
    offered, released = _in_flight(driver)
    gaps = _gaps(driver, clicked)
    release.set()
    if not _until(driver, OUTCOME):
        problems.append("send hold: the key's outcome never showed")
    if writes.posts != 1:
        problems.append(f"send hold: one tap sent {writes.posts} key posts")
    if offered:
        problems.append("send hold: a live control was drawn while the key was still out")
    if released or gaps:
        problems.append("send hold: Back and End were re-enabled while the key was still out")
    return problems


def run(
    driver: Marionette, base: str, mount: Callable[[ASGIApp], None], scratch: Path
) -> list[str]:
    """The walk on a fresh app in its own phone window; empty when the key went once."""
    home = scratch / "send-hold"
    home.mkdir(parents=True)
    state = State("send hold", f"{AGENT}?tab=pane", OPTION, asking)
    rig = build(state, home)
    release = threading.Event()
    rig.wire.after[SEND_KEYS] = slow_key(release)
    writes = Writes(rig.http.app)
    mount(writes)
    first = driver.handles()[0]
    phone_window(driver, WIDTH, HEIGHT)
    try:
        problems = walk(driver, base, writes, release)
    finally:
        release.set()
        driver.send("WebDriver:CloseWindow", {})
        driver.send("WebDriver:SwitchToWindow", {"handle": first})
    print("  walked send hold", flush=True)
    return problems
