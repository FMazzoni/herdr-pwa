"""Drive the Chat and Pane tabs in headless Firefox: each opens on its newest line, follows new
lines while the reader is at the end, and leaves a reader who scrolled up where they were. An
agent that is asking opens at the top instead, with its question on the first screen. A tool's
result the reader opened stays open across a poll, and the page does not move under them."""

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from firefox import Marionette, phone_window
from starlette.types import ASGIApp

from herdr_pwa.jsontypes import JsonObject
from tests.catalogue import (
    AGENT,
    BLOCKED,
    MENU,
    State,
    build,
    capture,
    herd,
    long_chat,
    long_pane,
    runner,
)
from tests.webfakes import (
    READ,
    WORKING_SESSION,
    Rig,
    assistant_says,
    read_frame,
    snapshot_frame,
    write_transcript,
)

WIDTH = 390
HEIGHT = 844
WAIT_SECONDS = 15.0
# The swap scrolls in the same task; this covers htmx's settle and the dock's next frame.
SETTLE_SECONDS = 0.5
# A reader's place may move by a rounding error, never by a line.
DRIFT = 3
TURNS = 24
LINES = 120

# Where the newest turn or terminal line, and the question if any, sit against the dock.
PROBE = r"""
const units = document.querySelectorAll(
  "#agent-view .turn, #agent-view > .pane:not(.pane--status) > .pane__line");
const last = units[units.length - 1];
const bar = document.getElementById("composer");
const answer = document.getElementById("answer");
const busy = document.querySelector(".htmx-request, .htmx-swapping, .htmx-settling");
if (!last || !bar || !answer || busy || document.readyState !== "complete") return null;
const box = last.getBoundingClientRect();
const question = document.querySelector("#answer .answer__question");
const asked = question ? question.getBoundingClientRect() : null;
return {
  text: last.textContent,
  bottom: box.bottom,
  dock: bar.getBoundingClientRect().top,
  scrollY: window.scrollY,
  scrollable: document.documentElement.scrollHeight > window.innerHeight,
  questionTop: asked ? asked.top : null,
  questionBottom: asked ? asked.bottom : null,
};
"""
POKE = "htmx.trigger(document.body, 'hp:poke');"
# Lines in the tool's result: open, it is taller than the screen.
RESULT_LINES = 120
# How far under the dock's top edge the newest turn may end and still count as the end (app.js).
END_SLACK = 120

# Open the last tool result and bring the middle of its output to the middle of the screen.
OPEN_RESULT = r"""
const details = [...document.querySelectorAll("#agent-view details.turn__result")].pop();
if (!details) return false;
details.querySelector("summary").click();
const out = details.querySelector("pre").getBoundingClientRect();
window.scrollBy(0, out.top + out.height / 2 - innerHeight / 2);
window.__hpSwaps = 0;
document.addEventListener("htmx:afterSettle", () => { window.__hpSwaps += 1; });
return true;
"""
READ_RESULT = r"""
const details = [...document.querySelectorAll("#agent-view details.turn__result")].pop();
const busy = document.querySelector(".htmx-request, .htmx-swapping, .htmx-settling");
return { open: !!(details && details.open), scrollY: window.scrollY,
  swaps: window.__hpSwaps || 0, busy: !!busy };
"""


@dataclass(frozen=True)
class Tab:
    """One tab's walk: the state it opens on, its newest line's text, and how it grows one."""

    name: str
    state: State
    newest: str
    grow: Callable[[Rig, Path, int], str]


def _probe(driver: Marionette) -> dict[str, object]:
    found = driver.script(PROBE)
    return found if isinstance(found, dict) else {}


def _wait_for(driver: Marionette, text: str) -> dict[str, object]:
    """Wait until the newest line says ``text`` and nothing is in flight, then let it settle."""
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


def _shown(seen: dict[str, object], where: str) -> list[str]:
    """The newest line must end inside the viewport and above the dock."""
    bottom, dock = _num(seen, "bottom"), _num(seen, "dock")
    if bottom > 0 and bottom <= dock + 0.5:
        return []
    return [f"{where}: the newest line ends at {bottom:.1f}px, dock at {dock:.1f}px"]


def _say(_rig: Rig, home: Path, n: int) -> str:
    """Rewrite the transcript with one more newest reply; return its text."""
    text = f"Newest reply number {n}."
    records = [*long_chat(TURNS), assistant_says(text, uuid=f"new{n}")]
    write_transcript(home / "transcripts", WORKING_SESSION, records)
    return text


def _print(rig: Rig, _home: Path, n: int) -> str:
    """Make the terminal print one more newest line; return its text."""
    text = f"newest output line {n}"
    rig.wire.replies[READ] = read_frame(long_pane(LINES) + text + "\n")
    return text


def _scrolled_up_stays(driver: Marionette, tab: Tab, rig: Rig, home: Path) -> list[str]:
    driver.script("window.scrollTo(0, 200);")
    time.sleep(SETTLE_SECONDS)
    before = _num(_probe(driver), "scrollY")
    newest = tab.grow(rig, home, 2)
    driver.script(POKE)
    after = _num(_wait_for(driver, newest), "scrollY")
    if abs(after - before) <= DRIFT:
        return []
    return [
        f"{tab.name}: a poll moved a reader who had scrolled up from {before:.0f}px to {after:.0f}px"
    ]


def follow(driver: Marionette, base: str, tab: Tab, rig: Rig, home: Path) -> list[str]:
    """Open on the end, follow a new line there, then scroll up and stay put."""
    driver.send("WebDriver:Navigate", {"url": base + tab.state.path})
    seen = _wait_for(driver, tab.newest)
    if not seen:
        return [f"{tab.name}: never drew its newest line"]
    if not seen.get("scrollable"):
        return [f"{tab.name}: fits on one screen, so it proves nothing"]
    problems = _shown(seen, f"{tab.name}, opened")
    newest = tab.grow(rig, home, 1)
    driver.script(POKE)
    problems += _shown(_wait_for(driver, newest), f"{tab.name}, after a poll at the end")
    return problems + _scrolled_up_stays(driver, tab, rig, home)


def asked(driver: Marionette, base: str, tab: Tab, _rig: Rig, _home: Path) -> list[str]:
    """An agent with a question open opens at the top, the question on the first screen."""
    driver.send("WebDriver:Navigate", {"url": base + tab.state.path})
    seen = _wait_for(driver, tab.newest)
    if not seen or seen.get("questionTop") is None:
        return [f"{tab.name}: never drew its question and its newest line"]
    if not seen.get("scrollable"):
        return [f"{tab.name}: fits on one screen, so it proves nothing"]
    top, bottom = _num(seen, "questionTop"), _num(seen, "questionBottom")
    if top >= 0 and bottom <= _num(seen, "dock"):
        return []
    return [f"{tab.name}: the question sits at {top:.0f}..{bottom:.0f}px, off the first screen"]


def _tool_chat(turns: int) -> list[JsonObject]:
    """A long chat whose last exchange is a tool call with a long result, then a short reply."""
    call: JsonObject = {
        "type": "assistant",
        "uuid": "call",
        "timestamp": "2026-10-10T10:00:01.000Z",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "tu1", "name": "Bash", "input": {"command": "make test"}}
            ],
        },
    }
    output = "\n".join(f"test {n:03d} passed" for n in range(RESULT_LINES))
    result: JsonObject = {
        "type": "user",
        "uuid": "res",
        "timestamp": "2026-10-10T10:00:02.000Z",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "tu1", "content": output}],
        },
    }
    return [
        *long_chat(turns),
        call,
        result,
        assistant_says("Reply after the tests: all green.", uuid="end"),
    ]


def kept_open(driver: Marionette, base: str, tab: Tab, _rig: Rig, _home: Path) -> list[str]:
    """Open a tool's result far from the end; a poll leaves it open and the page where it was."""
    driver.send("WebDriver:Navigate", {"url": base + tab.state.path})
    if not _wait_for(driver, tab.newest) or not driver.script(OPEN_RESULT):
        return [f"{tab.name}: never drew its tool result"]
    time.sleep(SETTLE_SECONDS)
    seen = _probe(driver)
    if _num(seen, "bottom") <= _num(seen, "dock") + END_SLACK:
        return [f"{tab.name}: the reader is at the end, so it proves nothing"]
    before = driver.script(READ_RESULT)
    driver.script(POKE)
    deadline = time.monotonic() + WAIT_SECONDS
    after: object = None
    while time.monotonic() < deadline:
        after = driver.script(READ_RESULT)
        if isinstance(after, dict) and after.get("swaps") and not after.get("busy"):
            break
        time.sleep(0.1)
    time.sleep(SETTLE_SECONDS)
    after = driver.script(READ_RESULT)
    if not isinstance(before, dict) or not isinstance(after, dict) or not after.get("swaps"):
        return [f"{tab.name}: the poll never redrew the chat"]
    problems: list[str] = []
    if not after.get("open"):
        problems.append(f"{tab.name}: the tool result the reader opened closed on a poll")
    start, end = _num(before, "scrollY"), _num(after, "scrollY")
    if abs(end - start) > DRIFT:
        problems.append(f"{tab.name}: a poll moved the reader from {start:.0f}px to {end:.0f}px")
    return problems


def _tabs() -> tuple[tuple[Tab, Callable[[Marionette, str, Tab, Rig, Path], list[str]]], ...]:
    blocked = runner(lambda: herd(**BLOCKED), long_pane(LINES) + capture(MENU))
    chat = State("chat", f"{AGENT}?tab=chat", "", runner(snapshot_frame), long_chat(TURNS))
    pane = State("pane", f"{AGENT}?tab=pane", "", runner(snapshot_frame, long_pane(LINES)))
    asking_chat = State("asking chat", chat.path, "", blocked, long_chat(TURNS))
    asking_pane = State("asking pane", pane.path, "", blocked)
    last_reply = f"Reply {TURNS - 1}:"
    last_line = f"step {LINES - 1:03d}"
    tool = State("tool result", chat.path, "", runner(snapshot_frame), _tool_chat(TURNS))
    return (
        (Tab("tool result", tool, "Reply after the tests", _say), kept_open),
        (Tab("chat end", chat, last_reply, _say), follow),
        (Tab("pane end", pane, last_line, _print), follow),
        (Tab("chat question", asking_chat, last_reply, _say), asked),
        (Tab("pane question", asking_pane, "", _print), asked),
    )


def run(
    driver: Marionette, base: str, mount: Callable[[ASGIApp], None], scratch: Path
) -> list[str]:
    """Every walk on a fresh app in its own phone window; empty when each tab keeps its end."""
    problems: list[str] = []
    for tab, walk in _tabs():
        home = scratch / f"end-{tab.name.replace(' ', '-')}"
        home.mkdir(parents=True)
        rig = build(tab.state, home)
        mount(rig.http.app)
        first = driver.handles()[0]
        phone_window(driver, WIDTH, HEIGHT)
        try:
            problems += walk(driver, base, tab, rig, home)
        finally:
            driver.send("WebDriver:CloseWindow", {})
            driver.send("WebDriver:SwitchToWindow", {"handle": first})
        print(f"  walked {tab.name}", flush=True)
    return problems
