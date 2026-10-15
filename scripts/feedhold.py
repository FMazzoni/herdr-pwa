"""Drive the feed in headless Firefox: when an agent above the screen changes state, the poll
that re-sorts the cards leaves the card under the thumb where it was, so a tap opens the agent
the reader was looking at."""

from collections.abc import Callable
from pathlib import Path

from firefox import Marionette, phone_window
from settle import READER_STILL_MS, settle
from starlette.types import ASGIApp

from herdr_pwa.jsontypes import JsonArray, JsonObject, get_object
from tests.catalogue import State, build
from tests.waiting import eventually
from tests.webfakes import READ, SNAPSHOT, Rig, agents_of, read_frame, snapshot_frame

WIDTH = 390
HEIGHT = 844
WAIT_SECONDS = 15.0
# A card may move by a rounding error, never by a card.
DRIFT = 3
AGENTS = 24
# Where the thumb rests: the middle of the screen, sideways and down.
THUMB_Y = 0.55
# The cache keeps a snapshot this long on the rig's clock; moving past it forces a fresh read.
STALE_SECONDS = 60.0

# The card under the thumb and the agents whose cards lie wholly above the screen.
PROBE = r"""
const [y, watched] = arguments;
const busy = document.querySelector(".htmx-request, .htmx-swapping, .htmx-settling, .boot");
const cards = document.querySelectorAll("#feed-list .card");
if (busy || !cards.length || document.readyState !== "complete") return null;
const bar = document.querySelector(".topbar").getBoundingClientRect().bottom;
const hit = document.elementFromPoint(innerWidth / 2, innerHeight * y);
const card = hit && hit.closest(".card");
const pane = (c) => c.querySelector(".card__pane").textContent;
return {
  under: card ? pane(card) : null,
  top: card ? card.getBoundingClientRect().top : null,
  above: [...cards].filter((c) => c.getBoundingClientRect().bottom < bar).map(pane),
  blocked: [...document.querySelectorAll("#feed-list .card--blocked")].map(pane),
  watched: [...cards].filter((c) => pane(c) === watched).map((c) => c.getBoundingClientRect().top)[0],
};
"""
POKE = "htmx.trigger(document.body, 'hp:poke');"


def crowd(blocked: tuple[str, ...] = ()) -> JsonObject:
    """The recorded snapshot copied out to a herd several screens tall, ``blocked`` waiting."""
    frame = snapshot_frame()
    seeds = agents_of(frame)
    agents: JsonArray = []
    for n in range(AGENTS):
        agent = dict(seeds[n % len(seeds)])
        pane = f"{agent['pane_id']}{n}"
        agent["pane_id"] = pane
        agent["agent_session"] = {
            "agent": "claude",
            "kind": "id",
            "source": "herdr:claude",
            "value": f"00000000-0000-4000-8000-{n:012d}",
        }
        agent["agent_status"] = "blocked" if pane in blocked else ("working", "idle")[n % 2]
        agent["terminal_title"] = agent["terminal_title_stripped"] = f"Agent number {n}"
        agents.append(agent)
    snapshot = get_object(get_object(frame, "result"), "snapshot")
    if snapshot is None:
        raise RuntimeError("the snapshot fixture changed shape")
    snapshot["agents"] = agents
    return frame


def _probe(driver: Marionette, watched: str = "") -> dict[str, object]:
    found = driver.script(PROBE, THUMB_Y, watched)
    return found if isinstance(found, dict) else {}


def _wait(
    driver: Marionette, done: Callable[[dict[str, object]], bool], watched: str = ""
) -> dict[str, object]:
    if not eventually(
        lambda: bool(seen := _probe(driver, watched)) and done(seen), seconds=WAIT_SECONDS
    ):
        return {}
    settle(driver)
    return _probe(driver, watched)


def _num(seen: dict[str, object], key: str) -> float:
    value = seen.get(key)
    return float(value) if isinstance(value, int | float) else float("nan")


def walk(driver: Marionette, base: str, rig: Rig) -> list[str]:
    """Scroll into the herd, block an agent above the screen, and find the same card underfoot."""
    driver.send("WebDriver:Navigate", {"url": base + "/"})
    if not _wait(driver, lambda _seen: True):
        return ["feed hold: the feed never drew its cards"]
    driver.script("window.scrollTo(0, 700);")
    settle(driver, READER_STILL_MS)
    before = _probe(driver)
    above = before.get("above")
    if not before.get("under") or not isinstance(above, list) or not above:
        return ["feed hold: no card lies above the screen, so it proves nothing"]
    mover = str(above[0])
    rig.wire.replies[SNAPSHOT] = crowd(blocked=(mover,))
    rig.clock.sleep(STALE_SECONDS)
    driver.script(POKE)
    under = str(before.get("under"))
    after = _wait(driver, lambda seen: mover in str(seen.get("blocked")), under)
    if not after:
        return [f"feed hold: {mover} never showed as blocked after the poll"]
    moved = _num(after, "watched") - _num(before, "top")
    if after.get("under") == under and abs(moved) <= DRIFT:
        return []
    said = (
        f"feed hold: {mover} above the screen started needing you and the poll moved {under} "
        f"from {_num(before, 'top'):.0f}px to {_num(after, 'watched'):.0f}px; "
        f"the thumb is now over {after.get('under')}"
    )
    return [said]


def run(
    driver: Marionette, base: str, mount: Callable[[ASGIApp], None], scratch: Path
) -> list[str]:
    """The walk on a fresh app in its own phone window; empty when the card stays underfoot."""
    home = scratch / "feed-hold"
    home.mkdir(parents=True)
    state = State("feed hold", "/", "", lambda: {SNAPSHOT: crowd(), READ: read_frame("")})
    rig = build(state, home)
    mount(rig.http.app)
    first = driver.handles()[0]
    phone_window(driver, WIDTH, HEIGHT)
    try:
        problems = walk(driver, base, rig)
    finally:
        driver.send("WebDriver:CloseWindow", {})
        driver.send("WebDriver:SwitchToWindow", {"handle": first})
    print("  walked feed hold", flush=True)
    return problems
