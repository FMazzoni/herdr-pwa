"""Drive the Chat tab in headless Firefox: it opens on the newest turn, follows new turns while
the reader is at the end, and leaves a reader who scrolled up where they were."""

import time
from collections.abc import Callable
from pathlib import Path

from firefox import Marionette, phone_window
from starlette.types import ASGIApp

from tests.catalogue import AGENT, State, build, long_chat, runner
from tests.webfakes import WORKING_SESSION, assistant_says, snapshot_frame, write_transcript

WIDTH = 390
HEIGHT = 844
WAIT_SECONDS = 15.0
# The swap scrolls in the same task; this covers htmx's settle and the dock's next frame.
SETTLE_SECONDS = 0.5
# A reader's place may move by a rounding error, never by a turn.
DRIFT = 3
TURNS = 24

# Where the newest turn sits against the viewport and the docked composer.
PROBE = r"""
const turns = document.querySelectorAll("#agent-view .turn");
const last = turns[turns.length - 1];
const bar = document.getElementById("composer");
const busy = document.querySelector(".htmx-request, .htmx-swapping, .htmx-settling");
if (!last || !bar || busy || document.readyState !== "complete") return null;
const box = last.getBoundingClientRect();
return {
  text: last.textContent,
  top: box.top,
  bottom: box.bottom,
  dock: bar.getBoundingClientRect().top,
  scrollY: window.scrollY,
  scrollable: document.documentElement.scrollHeight > window.innerHeight,
};
"""
POKE = "htmx.trigger(document.body, 'hp:poke');"


def _probe(driver: Marionette) -> dict[str, object]:
    found = driver.script(PROBE)
    return found if isinstance(found, dict) else {}


def _wait_for(driver: Marionette, text: str) -> dict[str, object]:
    """Wait until the newest turn says ``text`` and nothing is in flight, then let it settle."""
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        if text in str(_probe(driver).get("text", "")):
            time.sleep(SETTLE_SECONDS)
            return _probe(driver)
        time.sleep(0.1)
    return {}


def _num(seen: dict[str, object], key: str) -> float:
    value = seen.get(key)
    return float(value) if isinstance(value, int | float) else float("nan")


def _shown(seen: dict[str, object], after: str) -> list[str]:
    """The newest turn must end inside the viewport and above the dock."""
    bottom, dock = _num(seen, "bottom"), _num(seen, "dock")
    if bottom > 0 and bottom <= dock + 0.5:
        return []
    return [
        f"chat end: after {after}, the newest turn ends at {bottom:.1f}px, dock at {dock:.1f}px"
    ]


def _say(home: Path, turns: int) -> str:
    """Rewrite the transcript with ``turns`` turns plus one newest reply; return its text."""
    text = f"Newest reply number {turns}."
    write_transcript(
        home / "transcripts",
        WORKING_SESSION,
        [*long_chat(turns), assistant_says(text, uuid=f"new{turns}")],
    )
    return text


def walk(driver: Marionette, base: str, home: Path) -> list[str]:
    """Open, follow, then scroll up and stay put; the problems found."""
    driver.send("WebDriver:Navigate", {"url": f"{base}{AGENT}?tab=chat"})
    seen = _wait_for(driver, f"Reply {TURNS - 1}:")
    if not seen:
        return ["chat end: the long chat never drew its newest turn"]
    if not seen.get("scrollable"):
        return ["chat end: the long chat fits on one screen, so it proves nothing"]
    problems = _shown(seen, "opening the Chat tab")
    newest = _say(home, TURNS)
    driver.script(POKE)
    problems += _shown(_wait_for(driver, newest), "a poll that added a turn at the end")
    driver.script("window.scrollTo(0, 200);")
    time.sleep(SETTLE_SECONDS)
    before = _num(_probe(driver), "scrollY")
    newest = _say(home, TURNS + 1)
    driver.script(POKE)
    after = _num(_wait_for(driver, newest), "scrollY")
    if not abs(after - before) <= DRIFT:
        problems.append(
            f"chat end: a poll moved a reader who had scrolled up from {before:.0f}px to {after:.0f}px"
        )
    return problems


def run(
    driver: Marionette, base: str, mount: Callable[[ASGIApp], None], scratch: Path
) -> list[str]:
    """The walk on a fresh app in its own phone window; empty when the chat keeps its end."""
    home = scratch / "chatend"
    home.mkdir(parents=True)
    state = State(
        "chat-end", f"{AGENT}?tab=chat", ".chat .turn", runner(snapshot_frame), long_chat(TURNS)
    )
    mount(build(state, home).http.app)
    first = driver.handles()[0]
    phone_window(driver, WIDTH, HEIGHT)
    try:
        problems = walk(driver, base, home)
    finally:
        driver.send("WebDriver:CloseWindow", {})
        driver.send("WebDriver:SwitchToWindow", {"handle": first})
    print("  walked chat end", flush=True)
    return problems
