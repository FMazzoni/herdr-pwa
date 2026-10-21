"""Drive one slow answer and one slow prompt in headless Firefox. While the key is in flight, a
stream event and a redraw held back under the thumb both land, and the page must still send the key
once, keep Back and End disabled until the answer is back, and then show what the key did. While
the prompt is in flight, a finger on Back or End reaches nothing and a tap on Back stays on the page,
and once the prompt's outcome shows, both are live again."""

import threading
from collections.abc import Callable
from typing import Final

from firefox import Bench, Marionette, phone_window
from pydantic import JsonValue, TypeAdapter
from starlette.types import ASGIApp, Receive, Scope, Send

from tests.catalogue import AGENT, IDLE, PERMISSION, WORKING, State, build, capture, herd
from tests.waiting import eventually, holds, watch
from tests.webfakes import (
    AGENT_GET,
    READ,
    SEND_KEYS,
    SNAPSHOT,
    WORKING_PANE,
    FakeWire,
    Hold,
    Reply,
    agents_of,
    read_frame,
)

WIDTH: Final = 390
HEIGHT: Final = 844
WAIT_SECONDS: Final = 15.0
# How long the runner sits on the key: several of app.js's 250ms idle waits, so a redraw held
# back under the thumb comes due while the key is still out.
SLOW_SECONDS: Final = 2.0
# How long the outcome must stay once drawn: past app.js's 250ms idle wait, when a redraw held
# back under the thumb comes due.
STAYS_SECONDS: Final = 1.0
# How long a tap on Back is watched for a navigation it should not start.
LEAVE_SECONDS: Final = 1.5
OPTION: Final = "#answer .button--option"

# Every change to the body's sending class, and every read that finished, stamped on one clock.
INSTRUMENT: Final = r"""
window.__hp = { marks: [], reads: 0 };
const say = () => window.__hp.marks.push(
  [performance.now(), document.body.classList.contains("is-sending")]);
new MutationObserver(say).observe(document.body, { attributes: true, attributeFilter: ["class"] });
document.addEventListener("htmx:afterRequest", (e) => {
  if (e.detail.requestConfig && e.detail.requestConfig.verb === "get") window.__hp.reads += 1;
});
"""
READY: Final = r"""
// A region still settling has not bound its triggers yet, so an event sent to it is lost.
const busy = document.querySelector(".htmx-request, .htmx-swapping, .htmx-settling");
return document.readyState === "complete" && !busy && !!document.querySelector(arguments[0]);
"""
# A finger on the glass, as iOS reports it; only the listeners in app.js read these.
TOUCH: Final = r"""
const e = new Event(arguments[0], { bubbles: true });
Object.defineProperty(e, "touches", { value: arguments[0] === "touchstart" ? [{}] : [] });
document.dispatchEvent(e);
"""
# The thumb lifts and taps in one task, so a redraw held back under it is still owed.
LIFT_AND_TAP: Final = (
    TOUCH + "document.querySelector(arguments[1]).click(); return performance.now();"
)
STALE: Final = "htmx.trigger(document.querySelector(arguments[0]), 'hp:stale');"
READS: Final = "return window.__hp.reads;"
# A control a second tap could land on: any button in the region that is live right now.
LIVE: Final = r"""
const live = [...document.querySelectorAll("#answer button")].filter((b) => !b.disabled);
const option = live.find((b) => b.matches(".button--option"));
if (option) option.click();
return { live: live.length, sending: document.body.classList.contains("is-sending") };
"""
OUTCOME: Final = "return !!document.querySelector('#answer .banner[data-hp-sent]');"
# Each way off the page that is on screen, and whether a finger at its middle would land on it.
REACH: Final = r"""
return [...document.querySelectorAll(".leave")].flatMap((a) => {
  const r = a.getBoundingClientRect();
  const x = r.left + r.width / 2, y = r.top + r.height / 2;
  if (!r.width || y < 0 || y > window.innerHeight || x < 0 || x > window.innerWidth) return [];
  const hit = document.elementFromPoint(x, y);
  return [{ name: a.textContent.trim(), live: !!hit && a.contains(hit) }];
});
"""
COMPOSER: Final = "#composer textarea"
TYPE_AND_SEND: Final = r"""
document.querySelector("#composer textarea").value = arguments[0];
document.querySelector("#composer button[type=submit]").click();
return performance.now();
"""
# A tap on Back that pointer-events would not stop: what is left is the script's own guard.
TAP_BACK: Final = r"""
window.__hp.from = location.href;
document.querySelector(".leave.topbar__back").click();
"""
# A page left behind takes its marks with it, so a missing mark is a page that left.
STAYED: Final = "return !!window.__hp && location.href === window.__hp.from;"
PROMPT_OUTCOME: Final = "return !!document.querySelector('#composer .banner[data-hp-sent]');"
PROMPT: Final = "agent.prompt"
_EACH: Final = TypeAdapter(list[dict[str, JsonValue]])
GAPS: Final = "return window.__hp.marks.filter(([t, on]) => t >= arguments[0] && !on).length;"


class Writes:
    """The real app behind a count of the posts to one route that reach it."""

    def __init__(self, app: ASGIApp, route: str = "/keys") -> None:
        self.app = app
        self.route = route
        self.posts = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Count a post to the route, then serve it."""
        if scope["type"] == "http" and scope["method"] == "POST":
            self.posts += str(scope["path"]).endswith(self.route)
        await self.app(scope, receive, send)


def agent_info(frame: dict[str, JsonValue]) -> dict[str, JsonValue]:
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


def idle() -> dict[str, Reply]:
    """The tapped agent idle at its prompt, with a runner that takes a prompt."""
    frame = herd(p4="idle")
    return {
        SNAPSHOT: frame,
        READ: read_frame(capture(IDLE)),
        AGENT_GET: agent_info(frame),
        PROMPT: {"id": "x", "result": {"type": "ok"}},
    }


def landed(wire: FakeWire) -> None:
    """Once the key is let go, the question goes and the agent works."""
    frame = herd(p4="working")
    wire.replies.update(
        {SNAPSHOT: frame, AGENT_GET: agent_info(frame), READ: read_frame(capture(WORKING))}
    )


def _until(driver: Marionette, script: str, *args: object) -> bool:
    return eventually(lambda: bool(driver.script(script, *args)), WAIT_SECONDS)


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
    return []


def _reach(driver: Marionette) -> dict[str, bool]:
    """Each way off the page on screen, by its label: whether a finger there would take it."""
    each = _EACH.validate_python(driver.script(REACH) or [])
    return {str(e.get("name")): bool(e.get("live")) for e in each}


def _in_flight(driver: Marionette) -> tuple[bool, bool]:
    """Watch the slow key: (a live control was offered, Back and End were ever re-enabled)."""
    offered = released = False

    def look() -> None:
        nonlocal offered, released
        seen = driver.script(LIVE)
        if isinstance(seen, dict):
            offered = offered or bool(seen.get("live"))
            released = released or not seen.get("sending")
        released = released or any(_reach(driver).values())

    watch(look, SLOW_SECONDS)
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
    found = driver.script(LIFT_AND_TAP, "touchend", OPTION)
    clicked = float(found) if isinstance(found, int | float) else 0.0
    # A stream event for another region lands while the key is out.
    driver.script(STALE, "#agent-view")
    offered, released = _in_flight(driver)
    gaps = _gaps(driver, clicked)
    release.set()
    if not _until(driver, OUTCOME):
        problems.append("send hold: the key's outcome never showed")
    elif not holds(lambda: driver.script(OUTCOME), STAYS_SECONDS):
        problems.append("send hold: the key's outcome showed, then a held-back redraw took it away")
    if writes.posts != 1:
        problems.append(f"send hold: one tap sent {writes.posts} key posts")
    if offered:
        problems.append("send hold: a live control was drawn while the key was still out")
    if released or gaps:
        problems.append("send hold: Back and End were re-enabled while the key was still out")
    return problems


def _prompt_held(driver: Marionette, hold: Hold) -> list[str]:
    """While the prompt is out: no way off the page takes a finger, and a tap on Back stays."""
    if not hold.taken.wait(WAIT_SECONDS):
        return ["prompt hold: the prompt never reached the runner"]
    # A read finishing mid-send must release nothing: the view redraws while the prompt is out.
    reads = _number(driver, READS)
    driver.script(STALE, "#agent-view")
    problems: list[str] = []
    reached: set[str] = set()
    off = 0

    def look() -> None:
        nonlocal off
        reached.update(name for name, live in _reach(driver).items() if live)
        off += not driver.script("return document.body.classList.contains('is-sending');")

    watch(look, SLOW_SECONDS)
    if _number(driver, READS) <= reads:
        problems.append("prompt hold: the view never redrew while the prompt was out")
    if reached or off:
        problems.append(f"prompt hold: Back and End took a finger mid-send ({sorted(reached)})")
    driver.script(TAP_BACK)
    # Leaving waits on the stream's close first, so give a navigation time to begin.
    if not holds(lambda: driver.script(STAYED), LEAVE_SECONDS):
        problems.append("prompt hold: a tap on Back left the page while the prompt was out")
    return problems


def walk_prompt(driver: Marionette, base: str, writes: Writes, hold: Hold) -> list[str]:
    """Send a prompt while the runner is slow: nothing leaves the page until its outcome shows."""
    driver.send("WebDriver:Navigate", {"url": f"{base}{AGENT}"})
    if not _until(driver, READY, COMPOSER):
        return ["prompt hold: the composer never drew"]
    driver.script(INSTRUMENT)
    if not any(_reach(driver).values()):
        return ["prompt hold: no way off the page took a finger before the send"]
    driver.script(TYPE_AND_SEND, "run the tests again")
    problems = _prompt_held(driver, hold)
    if problems and not driver.script(STAYED):
        return problems
    hold.release.set()
    if not _until(driver, PROMPT_OUTCOME):
        problems.append("prompt hold: the prompt's outcome never showed")
    elif not all(_reach(driver).values()):
        problems.append("prompt hold: Back and End stayed disabled after the outcome showed")
    if writes.posts != 1:
        problems.append(f"prompt hold: one tap sent {writes.posts} prompt posts")
    return problems


def _in_window(
    driver: Marionette, go: Callable[[], list[str]], release: threading.Event
) -> list[str]:
    first = driver.handles()[0]
    phone_window(driver, WIDTH, HEIGHT)
    try:
        return go()
    finally:
        release.set()
        driver.send("WebDriver:CloseWindow", {})
        driver.send("WebDriver:SwitchToWindow", {"handle": first})


def run(bench: Bench) -> list[str]:
    """Both walks, each on a fresh app in its own phone window; empty when each write went once."""
    driver, base = bench.driver, bench.base
    home = bench.scratch / "send-hold"
    home.mkdir(parents=True)
    rig = build(State("send hold", f"{AGENT}?tab=pane", OPTION, asking), home)
    hold = Hold(landed, ceiling=WAIT_SECONDS)
    rig.wire.after[SEND_KEYS] = hold
    writes = Writes(rig.http.app)
    bench.mount(writes)
    problems = _in_window(driver, lambda: walk(driver, base, writes, hold.release), hold.release)
    print("  walked send hold", flush=True)
    home = bench.scratch / "prompt-hold"
    home.mkdir(parents=True)
    rig = build(State("prompt hold", AGENT, COMPOSER, idle), home)
    held = Hold(ceiling=WAIT_SECONDS)
    rig.wire.after[PROMPT] = held
    prompts = Writes(rig.http.app, "/prompt")
    bench.mount(prompts)
    problems += _in_window(driver, lambda: walk_prompt(driver, base, prompts, held), held.release)
    print("  walked prompt hold", flush=True)
    return problems
