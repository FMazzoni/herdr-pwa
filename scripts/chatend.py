"""Drive the Chat and Pane tabs in headless Firefox: each opens on its newest line, follows new
lines while the reader is at the end, and leaves a reader who scrolled up where they were. An
agent that is asking lands at the end too, its question after the newest line and on the first
screen, and a reader who scrolled up from it is offered the way back. A tool's
result the reader opened stays open across a poll, and the page does not move under them. A
burst of tool calls lands folded, its failure in plain sight, and a fold the reader opened
stays open while calls are added to it.

The chat is paged by messages and live over an event stream: a tool-heavy transcript opens on
twenty messages, "Load earlier" lands the page above without moving the reader, one change is
one refresh, and a lost stream falls back to polling and then recovers.

A reader who scrolls up from the end in small steps while the agent works is never pulled back
to it, and the line they are reading stays where it was across every redraw, even one that lands
before their scroll is heard."""

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from firefox import Marionette, phone_window
from pydantic import JsonValue
from settle import READER_STILL_MS, settle
from starlette.types import ASGIApp, Receive, Scope, Send

from herdr_pwa.web.events import Hub
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
    pick,
    runner,
    tool_heavy_chat,
)
from tests.waiting import STEP as POLL
from tests.waiting import eventually
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
# A reader's place may move by a rounding error, never by a line.
DRIFT = 3
TURNS = 24
LINES = 120

# Where the newest turn or terminal line, and the question if any, sit against the dock.
PROBE = r"""
const units = document.querySelectorAll(
  "#agent-view .turn, #agent-view > .pane > .pane__line");
const last = units[units.length - 1];
const bar = document.getElementById("composer");
const answer = document.getElementById("answer");
const busy = document.querySelector(".htmx-request, .htmx-swapping, .htmx-settling");
if (!last || !bar || !answer || busy || document.readyState !== "complete") return null;
const box = last.getBoundingClientRect();
const question = document.querySelector("#answer .answer__question");
const asked = question ? question.getBoundingClientRect() : null;
const options = [...document.querySelectorAll("#answer .option")];
const lowest = Math.max(0, ...options.map((o) => o.getBoundingClientRect().bottom));
return {
  under: document.querySelector(".topbar").getBoundingClientRect().bottom,
  options: options.length,
  optionsBottom: lowest,
  gap: document.documentElement.scrollHeight - innerHeight - scrollY,
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
const details = [...document.querySelectorAll("#agent-view details.tool")].pop();
if (!details) return false;
details.querySelector("summary").click();
const out = details.querySelector(".tool__result").getBoundingClientRect();
window.scrollBy(0, out.top + out.height / 2 - innerHeight / 2);
window.__hpSwaps = 0;
document.addEventListener("htmx:afterSettle", () => { window.__hpSwaps += 1; });
return true;
"""
READ_RESULT = r"""
const details = [...document.querySelectorAll("#agent-view details.tool")].pop();
const busy = document.querySelector(".htmx-request, .htmx-swapping, .htmx-settling");
return { open: !!(details && details.open), scrollY: window.scrollY,
  swaps: window.__hpSwaps || 0, busy: !!busy };
"""


# The burst: calls in a row, the third of them failing, then the agent's reply.
BURST = 6
FAILS_AT = 2
# Open the burst's first fold and its first call, and bring the fold to the middle of the screen.
OPEN_FOLD = r"""
const fold = document.querySelector("#agent-view details.tools");
if (!fold) return false;
fold.querySelector("summary").click();
fold.querySelector("details.tool summary").click();
const box = fold.getBoundingClientRect();
window.scrollBy(0, box.top + box.height / 2 - innerHeight / 2);
window.__hpSwaps = 0;
document.addEventListener("htmx:afterSettle", () => { window.__hpSwaps += 1; });
return true;
"""
READ_FOLD = r"""
const folds = [...document.querySelectorAll("#agent-view details.tools")];
const fold = folds[0];
const failed = document.querySelector("#agent-view .tool--failed");
const shut = failed && failed.parentElement.closest("details:not([open])");
const busy = document.querySelector(".htmx-request, .htmx-swapping, .htmx-settling");
return {
  folds: folds.length,
  open: folds.filter((f) => f.open).length,
  head: fold ? fold.querySelector("summary").textContent.trim() : "",
  inner: fold ? !!fold.querySelector("details.tool[open]") : false,
  failedShown: !!failed && !shut && failed.getBoundingClientRect().height > 0,
  scrollY: window.scrollY, swaps: window.__hpSwaps || 0, busy: !!busy,
};
"""

# Close the first fold, leaving the call opened inside it open.
CLOSE_FOLD = r"""
const fold = document.querySelector("#agent-view details.tools");
if (!fold) return null;
fold.querySelector("summary").click();
return { open: fold.open, inner: !!fold.querySelector("details.tool[open]") };
"""
# Redraw the named regions: the answer region alone, or every region on the page.
REDRAW = r"""
const ids = arguments[0];
for (const id of ids) document.getElementById(id).__hpOld = true;
if (ids.length === 1) htmx.trigger(document.getElementById(ids[0]), "hp:stale");
else htmx.trigger(document.body, "hp:poke");
"""
REDRAWN = r"""
const fold = document.querySelector("#agent-view details.tools");
const busy = document.querySelector(".htmx-request, .htmx-swapping, .htmx-settling");
const fresh = arguments[0].every((id) => !document.getElementById(id).__hpOld);
return { fresh: fresh, busy: !!busy, open: !!(fold && fold.open) };
"""


@dataclass(frozen=True)
class Tab:
    """One tab's walk: the state it opens on, its newest line's text, and how it grows one."""

    name: str
    state: State
    newest: str
    grow: Callable[[Rig, Path, int], str]
    width: int = WIDTH
    height: int = HEIGHT
    # iPhone Text Size, as a multiple of the default root.
    scale: float = 1.0


def _probe(driver: Marionette) -> dict[str, object]:
    found = driver.script(PROBE)
    return found if isinstance(found, dict) else {}


def _idle(driver: Marionette) -> dict[str, object]:
    """The probe once nothing is in flight: the stream may start a redraw at any moment, and a
    page caught mid-request has no place to measure."""
    seen: dict[str, object] = {}

    def found() -> bool:
        nonlocal seen
        seen = _probe(driver)
        return bool(seen)

    _until(found)
    return seen


def _wait_for(driver: Marionette, text: str) -> dict[str, object]:
    """Wait until the newest line says ``text`` and nothing is in flight, then let it settle."""
    if not _until(lambda: text in str(_probe(driver).get("text", ""))):
        return {}
    settle(driver)
    return _idle(driver)


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
    settle(driver, READER_STILL_MS)
    before = _num(_idle(driver), "scrollY")
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


# How often the page fetched its own view: a swap would land it by the swap's pin, not the first.
VIEW_READS = r"""
const view = document.getElementById("agent-view");
if (!view) return -1;
const own = new URL(view.getAttribute("data-hp-poll"), location.href).href;
return performance.getEntriesByType("resource").filter((e) => e.name === own).length;
"""


def cold(driver: Marionette, base: str, tab: Tab, _rig: Rig, _home: Path) -> list[str]:
    """A tab loaded straight from its address, as a link, a restored tab or a bookmark opens it,
    lands on its newest line with no swap to land it."""
    driver.send("WebDriver:Navigate", {"url": base + tab.state.path})
    seen = _wait_for(driver, tab.newest)
    if not seen:
        return [f"{tab.name}: never drew its newest line"]
    if not seen.get("scrollable"):
        return [f"{tab.name}: fits on one screen, so it proves nothing"]
    reads = driver.script(VIEW_READS)
    if reads != 0:
        return [f"{tab.name}: fetched its view {reads} times, so a swap landed it, not the load"]
    problems = _shown(seen, f"{tab.name}, loaded")
    if _num(seen, "gap") > DRIFT:
        problems.append(f"{tab.name}: loaded {_num(seen, 'gap'):.0f}px above the end")
    return problems


def asked(driver: Marionette, base: str, tab: Tab, _rig: Rig, _home: Path) -> list[str]:
    """An agent with a question open lands at the end like any other page, with the question
    after the newest line and it and every option on the first screen, above the dock."""
    driver.send("WebDriver:Navigate", {"url": base + tab.state.path})
    seen = _wait_for(driver, tab.newest)
    if not seen or seen.get("questionTop") is None or not seen.get("options"):
        return [f"{tab.name}: never drew its question, its options and its newest line"]
    if not seen.get("scrollable"):
        return [f"{tab.name}: fits on one screen, so it proves nothing"]
    problems: list[str] = []
    if _num(seen, "gap") > DRIFT:
        problems.append(f"{tab.name}: landed {_num(seen, 'gap'):.0f}px above the end")
    top, low = _num(seen, "questionTop"), _num(seen, "optionsBottom")
    if not (top >= _num(seen, "under") and low <= _num(seen, "dock")):
        problems.append(
            f"{tab.name}: the question and its options sit at {top:.0f}..{low:.0f}px, "
            "not all on the first screen above the dock"
        )
    if top < _num(seen, "bottom"):
        problems.append(f"{tab.name}: the question sits above the newest line, not after it")
    return problems


GROW_TEXT = r"""
const root = document.documentElement;
root.style.fontSize = parseFloat(getComputedStyle(root).fontSize) * arguments[0] + "px";
return true;
"""
# Where the question's first line sits, and whether the region is taller than the room for it.
TALL = r"""
const busy = document.querySelector(".htmx-request, .htmx-swapping, .htmx-settling");
const question = document.querySelector("#answer .answer__question");
const region = document.getElementById("answer");
if (busy || !question || !region) return null;
const under = document.querySelector(".topbar").getBoundingClientRect().bottom;
const dock = document.getElementById("composer").getBoundingClientRect().top;
const line = parseFloat(getComputedStyle(question).lineHeight) || 24;
return { top: question.getBoundingClientRect().top, line: line, under: under, dock: dock,
  tall: region.getBoundingClientRect().height > dock - under };
"""


def _tall(driver: Marionette) -> dict[str, object]:
    found = driver.script(TALL)
    return found if isinstance(found, dict) else {}


def tall_question(driver: Marionette, base: str, tab: Tab, _rig: Rig, _home: Path) -> list[str]:
    """A question taller than the room between the bar and the dock lands with its first line
    just under the bar, not at the very end with only Interrupt showing."""
    driver.send("WebDriver:Navigate", {"url": base + tab.state.path})
    if not _until(lambda: bool(_tall(driver))):
        return [f"{tab.name}: never drew its question"]
    if tab.scale != 1.0:
        # The dock grows with the text, and the page lands again for its new height.
        driver.script(GROW_TEXT, tab.scale)
    settle(driver)
    seen: dict[str, object] = {}
    if not _until(lambda: bool(seen.update(_tall(driver)) or seen)):
        return [f"{tab.name}: never settled"]
    if not seen.get("tall"):
        return [f"{tab.name}: the answer fits between the bar and the dock, so it proves nothing"]
    top, under, dock = _num(seen, "top"), _num(seen, "under"), _num(seen, "dock")
    if top >= under - DRIFT and top + _num(seen, "line") <= dock:
        return []
    where = f"between the bar ({under:.0f}px) and the dock ({dock:.0f}px)"
    return [f"{tab.name}: the question's first line sits at {top:.0f}px, not {where}"]


JUMP = r"""
const jump = document.getElementById("hp-jump");
const busy = document.querySelector(".htmx-request, .htmx-swapping, .htmx-settling");
const gap = document.documentElement.scrollHeight - innerHeight - scrollY;
return { exists: !!jump, shown: !!jump && !jump.hidden && jump.getBoundingClientRect().height > 0,
  busy: !!busy, gap: gap };
"""


def _jump(driver: Marionette) -> dict[str, object]:
    found = driver.script(JUMP)
    return found if isinstance(found, dict) else {}


def waiting_pill(driver: Marionette, base: str, tab: Tab, _rig: Rig, _home: Path) -> list[str]:
    """Scrolled up from a waiting question, the reader is offered a way back, which works."""
    driver.send("WebDriver:Navigate", {"url": base + tab.state.path})
    if not _wait_for(driver, tab.newest):
        return [f"{tab.name}: never drew its newest line"]
    if _jump(driver).get("shown"):
        return [f"{tab.name}: offers the way to the question while the question is on screen"]
    driver.script("window.scrollTo(0, 0);")
    if not _until(lambda: bool(_jump(driver).get("shown"))):
        return [f"{tab.name}: scrolled up from a waiting question, no way back to it appeared"]
    driver.script('document.getElementById("hp-jump").click(); return true;')
    if not _until(lambda: _num(_jump(driver), "gap") <= DRIFT and not _jump(driver).get("shown")):
        return [f"{tab.name}: the way back to the question did not land at the end"]
    return []


def _tool_chat(turns: int) -> list[dict[str, JsonValue]]:
    """A long chat whose last exchange is a tool call with a long result, then a short reply."""
    call: dict[str, JsonValue] = {
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
    result: dict[str, JsonValue] = {
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
    settle(driver, READER_STILL_MS)
    seen = _probe(driver)
    if _num(seen, "bottom") <= _num(seen, "dock") + END_SLACK:
        return [f"{tab.name}: the reader is at the end, so it proves nothing"]
    before = driver.script(READ_RESULT)
    driver.script(POKE)
    _until(lambda: _swapped(driver.script(READ_RESULT)))
    settle(driver)
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


def _swapped(seen: object) -> bool:
    """A redraw has landed and nothing is in flight."""
    return isinstance(seen, dict) and bool(seen.get("swaps")) and not seen.get("busy")


def _call(n: int, *, failed: bool = False) -> list[dict[str, JsonValue]]:
    """One tool call and its result, with a result long enough to scroll when it is open."""
    call: dict[str, JsonValue] = {
        "type": "assistant",
        "uuid": f"call{n}",
        "timestamp": "2026-10-10T10:00:01.000Z",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": f"tu{n}",
                    "name": "Bash",
                    "input": {"command": f"pytest -k case{n}"},
                }
            ],
        },
    }
    output = "\n".join(f"case{n} line {k:03d}" for k in range(RESULT_LINES // 4))
    result: dict[str, JsonValue] = {
        "type": "user",
        "uuid": f"res{n}",
        "timestamp": "2026-10-10T10:00:02.000Z",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": f"tu{n}",
                    "content": output,
                    "is_error": failed,
                }
            ],
        },
    }
    return [call, result]


def _burst_chat(calls: int) -> list[dict[str, JsonValue]]:
    """A long chat, then ``calls`` tool calls in a row with one failing, then the reply."""
    records = list(long_chat(TURNS))
    for n in range(calls):
        records += _call(n, failed=n == FAILS_AT)
    return [*records, assistant_says("Reply after the burst: fixed.", uuid="end")]


def _fold_problems(name: str, seen: dict[str, object], *, opened: bool) -> list[str]:
    problems: list[str] = []
    if not seen.get("failedShown"):
        problems.append(f"{name}: the failed call is hidden inside a closed fold")
    if not opened and seen.get("open"):
        problems.append(f"{name}: a burst of tool calls landed unfolded")
    return problems


def burst(driver: Marionette, base: str, tab: Tab, _rig: Rig, home: Path) -> list[str]:
    """Land folded; open a fold and a call in it; a poll that adds a call leaves both open."""
    driver.send("WebDriver:Navigate", {"url": base + tab.state.path})
    if not _wait_for(driver, tab.newest):
        return [f"{tab.name}: never drew its reply"]
    landed = driver.script(READ_FOLD)
    if not isinstance(landed, dict) or not landed.get("folds"):
        return [f"{tab.name}: the burst drew no fold"]
    problems = _fold_problems(tab.name, landed, opened=False)
    driver.script(OPEN_FOLD)
    settle(driver, READER_STILL_MS)
    before = driver.script(READ_FOLD)
    # One more call joins the first run, before the failure: the run's text changes, its
    # first call does not.
    grown = _burst_chat(BURST)
    at = TURNS + 2 * FAILS_AT
    write_transcript(home / "transcripts", WORKING_SESSION, [*grown[:at], *_call(99), *grown[at:]])
    driver.script(POKE)
    _until(lambda: _swapped(driver.script(READ_FOLD)))
    settle(driver)
    after = driver.script(READ_FOLD)
    if not isinstance(before, dict) or not isinstance(after, dict) or not after.get("swaps"):
        return [*problems, f"{tab.name}: the poll never redrew the chat"]
    if before.get("head") == after.get("head"):
        return [*problems, f"{tab.name}: the poll added no call to the fold, so it proves nothing"]
    if not after.get("open") or not after.get("inner"):
        problems.append(
            f"{tab.name}: the fold or the call the reader opened closed when a call joined it"
        )
    start, end = _num(before, "scrollY"), _num(after, "scrollY")
    if abs(end - start) > DRIFT:
        problems.append(f"{tab.name}: a poll moved the reader from {start:.0f}px to {end:.0f}px")
    return problems + _fold_problems(tab.name, after, opened=True) + _closed_stays(driver, tab)


def _redrawn(driver: Marionette, tab: Tab, ids: list[str]) -> list[str]:
    """Redraw ``ids``; the fold the reader closed must still be closed once they land."""
    driver.script(REDRAW, ids)
    after: dict[str, object] = {}

    def fresh() -> bool:
        nonlocal after
        found = driver.script(REDRAWN, ids)
        after = found if isinstance(found, dict) else {}
        return bool(after.get("fresh")) and not after.get("busy")

    _ = eventually(fresh, WAIT_SECONDS)
    if not after.get("fresh"):
        return [f"{tab.name}: {' and '.join(ids)} never redrew"]
    if after.get("open"):
        return [
            f"{tab.name}: a fold the reader closed opened again when {' and '.join(ids)} redrew"
        ]
    return []


def _closed_stays(driver: Marionette, tab: Tab) -> list[str]:
    """Close the fold the poll kept open; another region's redraw, then the view's, leave it shut."""
    shut = driver.script(CLOSE_FOLD)
    if not isinstance(shut, dict) or shut.get("open") or not shut.get("inner"):
        return [f"{tab.name}: the fold would not close over an open call, so it proves nothing"]
    return _redrawn(driver, tab, ["answer"]) or _redrawn(driver, tab, ["agent-view", "answer"])


# A finger, and a reader's scroll that has not been heard yet.

# A finger on the glass as iOS reports it, and a count of what the stats strip did since.
FINGER = r"""
const [kind] = arguments;
if (!window.__hpStrip) {
  window.__hpStrip = { answered: 0, swaps: 0 };
  // The answer is in hand once its swap is offered, whether the swap is then taken or put off.
  document.addEventListener("htmx:beforeSwap", (e) => {
    if (e.detail.target && e.detail.target.id === "agent-stats") window.__hpStrip.answered += 1;
  });
  document.addEventListener("htmx:afterSwap", (e) => {
    if (e.detail.target && e.detail.target.id === "agent-stats") window.__hpStrip.swaps += 1;
  });
}
const e = new Event(kind, { bubbles: true });
Object.defineProperty(e, "touches", { value: kind === "touchstart" ? [{}] : [] });
document.dispatchEvent(e);
return true;
"""
READ_STRIP = r"""
const busy = document.querySelector(".htmx-request, .htmx-swapping, .htmx-settling");
return { ...window.__hpStrip, busy: !!busy, there: !!document.getElementById("agent-stats") };
"""
STRIP_STALE = "htmx.trigger(document.getElementById('agent-stats'), 'hp:stale');"


def strip_under_finger(
    driver: Marionette, base: str, tab: Tab, _rig: Rig, _home: Path
) -> list[str]:
    """The stats strip's redraw waits while a finger is down, and lands once it lifts."""
    driver.send("WebDriver:Navigate", {"url": base + tab.state.path})
    if not _read_until(driver, READ_STRIP, lambda s: bool(s.get("there")) and not s.get("busy")):
        return [f"{tab.name}: the strip never drew"]
    driver.script(FINGER, "touchstart")
    driver.script(STRIP_STALE)
    held = _read_until(driver, READ_STRIP, lambda s: _num(s, "answered") >= 1 and not s.get("busy"))
    if not held:
        return [f"{tab.name}: the strip's read never came back"]
    problems: list[str] = []
    if _num(held, "swaps"):
        problems.append(f"{tab.name}: the strip was redrawn under a finger")
    driver.script(FINGER, "touchend")
    if not _read_until(driver, READ_STRIP, lambda s: _num(s, "swaps") >= 1):
        problems.append(f"{tab.name}: the strip's redraw never landed after the finger lifted")
    return problems


# Scroll up from the end, and before that scroll is heard, land a swap that does not move the page.
UNHEARD = r"""
const [by] = arguments;
window.__hpHeard = 0;
window.addEventListener("scroll", () => { window.__hpHeard += 1; }, { passive: true });
window.__hpSwaps = 0;
document.addEventListener("htmx:afterSettle", () => { window.__hpSwaps += 1; });
window.scrollBy({ top: -by, behavior: "instant" });
const target = document.createElement("div");
const detail = { target: target, elt: target, requestConfig: { verb: "post" }, shouldSwap: true };
document.dispatchEvent(new CustomEvent("htmx:beforeSwap", { detail: detail }));
document.dispatchEvent(new CustomEvent("htmx:afterSwap", { detail: detail }));
return window.scrollY;
"""
HEARD = r"""
const busy = document.querySelector(".htmx-request, .htmx-swapping, .htmx-settling");
return { heard: window.__hpHeard, swaps: window.__hpSwaps, scrollY: window.scrollY, busy: !!busy };
"""
UNHEARD_PX = 400


def unheard_scroll(driver: Marionette, base: str, tab: Tab, rig: Rig, home: Path) -> list[str]:
    """A write's swap that lands before the reader's scroll is heard does not keep them pinned."""
    driver.send("WebDriver:Navigate", {"url": base + tab.state.path})
    seen = _wait_for(driver, tab.newest)
    if not seen or not seen.get("scrollable"):
        return [f"{tab.name}: never drew a page tall enough to scroll"]
    up = driver.script(UNHEARD, UNHEARD_PX)
    if not _read_until(driver, HEARD, lambda s: _num(s, "heard") >= 1):
        return [f"{tab.name}: the reader's scroll was never heard"]
    tab.grow(rig, home, 1)
    driver.script(POKE)
    after = _read_until(driver, HEARD, lambda s: _num(s, "swaps") >= 1 and not s.get("busy"))
    if not after or not isinstance(up, int | float):
        return [f"{tab.name}: the poll never redrew the chat"]
    if abs(_num(after, "scrollY") - up) > DRIFT:
        moved = _num(after, "scrollY")
        return [
            f"{tab.name}: scrolled up to {up:.0f}px as a write landed, a poll moved it to {moved:.0f}px"
        ]
    return []


# Before the view's own redraw is swapped in, scroll up from the end: the redraw lands between
# the reader's scroll and its event, as one does when the page's next frame is late.
UNHEARD_READ = r"""
const [by] = arguments;
const gap = () => document.documentElement.scrollHeight - innerHeight - scrollY;
const seen = { up: null, heard: 0, atEnd: 0, swaps: 0 };
window.__hpUnheard = seen;
// On the window, capturing: ahead of app.js's own listener on the document.
window.addEventListener("htmx:beforeSwap", (event) => {
  const target = event.detail.target;
  if (seen.up !== null || !target || target.id !== "agent-view") return;
  window.scrollBy({ top: -by, behavior: "instant" });
  seen.up = window.scrollY;
}, { capture: true });
document.addEventListener("htmx:afterSettle", (event) => {
  if (seen.up !== null && event.detail.target && event.detail.target.id === "agent-view") {
    seen.swaps += 1;
  }
});
addEventListener("scroll", () => {
  if (seen.up === null) return;
  seen.heard += 1;
  if (gap() <= 2) seen.atEnd += 1;
}, { passive: true });
htmx.trigger(document.body, "hp:poke");
return true;
"""
READ_UNHEARD = r"""
const seen = window.__hpUnheard || {};
const busy = document.querySelector(".htmx-request, .htmx-swapping, .htmx-settling");
const gap = document.documentElement.scrollHeight - innerHeight - scrollY;
return { up: seen.up, heard: seen.heard || 0, atEnd: seen.atEnd || 0, swaps: seen.swaps || 0,
  gap: gap, busy: !!busy };
"""


def unheard_read(driver: Marionette, base: str, tab: Tab, rig: Rig, home: Path) -> list[str]:
    """The view's redraw that lands before the reader's scroll is heard does not pull them back."""
    driver.send("WebDriver:Navigate", {"url": base + tab.state.path})
    seen = _wait_for(driver, tab.newest)
    if not seen or not seen.get("scrollable"):
        return [f"{tab.name}: never drew a page tall enough to scroll"]
    tab.grow(rig, home, 1)
    driver.script(UNHEARD_READ, UNHEARD_PX)

    def landed() -> dict[str, object]:
        found = driver.script(READ_UNHEARD)
        return found if isinstance(found, dict) else {}

    if not _until(lambda: _num(landed(), "heard") >= 1 and _num(landed(), "swaps") >= 1):
        return [f"{tab.name}: the view's redraw never landed after the reader's scroll"]
    if not _until(lambda: not landed().get("busy")):
        return [f"{tab.name}: the view never settled"]
    after = landed()
    problems: list[str] = []
    if _num(after, "atEnd"):
        problems.append(f"{tab.name}: a redraw pulled the reader back to the end")
    if _num(after, "gap") < UNHEARD_PX / 2:
        problems.append(
            f"{tab.name}: scrolled {UNHEARD_PX}px up as the view redrew, "
            f"the reader ended {_num(after, 'gap'):.0f}px from the end"
        )
    return problems


# Paging and the event stream.

PAGE_MESSAGES = 20
# The rig's stream: a quick watcher, and a fallback poll short enough to see inside a walk.
LIVE_SETTINGS: dict[str, object] = {"events_interval": 0.2, "events_fallback": 2}
# Half as long again as one fallback poll, timed in the page: a page that polls while its
# stream is up shows here, because the poll's own timer is ordered before the window's end.
QUIET_MS = 3000
# Watcher looks after a change before a second refresh for it would have been published.
LOOKS_AFTER = 2
# Two polls with the stream down, so the page is seen polling on its interval, not once.
MIN_FALLBACK_POLLS = 2
RECONNECT_SECONDS = 25.0

COUNT_CHAT = r"""
window.__hpChat = 0;
if (!window.__hpCounting) {
  window.__hpCounting = true;
  document.addEventListener("htmx:afterRequest", (event) => {
    const path = (event.detail.pathInfo && event.detail.pathInfo.requestPath) || "";
    if (path.indexOf("/chat") !== -1) window.__hpChat += 1;
  });
}
return true;
"""
READ_COUNT = "return window.__hpChat || 0;"
# How many chat fetches the page made over ``ms`` of its own time from now.
COUNT_OVER = r"""
const [ms] = arguments;
const done = arguments[arguments.length - 1];
window.__hpChat = 0;
setTimeout(() => done(window.__hpChat), ms);
"""
# Whether the page's event stream is open now, read off its own request: app.js keeps no flag
# a script can see, so every request to /events made from here on is watched.
WATCH_STREAM = r"""
if (window.__hpWatching) return true;
window.__hpWatching = true;
window.__hpStream = false;
const open = XMLHttpRequest.prototype.open;
const send = XMLHttpRequest.prototype.send;
XMLHttpRequest.prototype.open = function (method, url) {
  this.__hpEvents = String(url).indexOf("/events") === 0;
  return open.apply(this, arguments);
};
XMLHttpRequest.prototype.send = function () {
  if (this.__hpEvents) {
    // Added after app.js's own handler, so it runs once that handler has opened the stream.
    this.addEventListener("readystatechange", () => {
      if (this.readyState >= 2 && this.status === 200) window.__hpStream = true;
    });
    this.addEventListener("loadend", () => { window.__hpStream = false; });
  }
  return send.apply(this, arguments);
};
return true;
"""
STREAM_OPEN = "return window.__hpStream === true;"
# The first message wholly in view under the top bar, how many there are, and whether the
# reader is at the end as well as at the top.
EARLIER = r"""
const busy = document.querySelector(".htmx-request, .htmx-swapping, .htmx-settling");
if (busy || document.readyState !== "complete") return null;
const under = document.querySelector(".topbar").getBoundingClientRect().bottom;
const all = [...document.querySelectorAll("#agent-view article.turn[data-hp-key]")];
const first = all.find((a) => a.getBoundingClientRect().top >= under);
const [key] = arguments;
const held = key ? all.find((a) => a.getAttribute("data-hp-key") === key) : first;
const units = document.querySelectorAll("#agent-view .turn");
const last = units[units.length - 1];
const dock = document.getElementById("composer").getBoundingClientRect().top;
return {
  count: all.length,
  key: held ? held.getAttribute("data-hp-key") : null,
  top: held ? held.getBoundingClientRect().top : null,
  atEnd: !!last && last.getBoundingClientRect().bottom <= dock + 120,
  more: !!document.querySelector(".chat__earlier-button"),
};
"""


def _hub(rig: Rig) -> Hub:
    hub = getattr(rig.http.app, "state").events  # noqa: B009
    if not isinstance(hub, Hub):
        raise TypeError("the app carries no event hub")
    return hub


def _until(done: Callable[[], bool], ceiling: float = WAIT_SECONDS, step: float = POLL) -> bool:
    return eventually(done, ceiling, step=step)


def _read_until(
    driver: Marionette, script: str, done: Callable[[dict[str, object]], bool]
) -> dict[str, object]:
    """What ``script`` reads once ``done`` holds of it; empty if it never does."""
    seen: dict[str, object] = {}

    def look() -> bool:
        nonlocal seen
        found = driver.script(script)
        seen = found if isinstance(found, dict) else {}
        return done(seen)

    return seen if eventually(look, WAIT_SECONDS) else {}


def _append(home: Path, text: str, uuid: str) -> str:
    """Append one agent reply to the transcript, the way the agent writes it."""
    log = next((home / "transcripts").rglob(f"{WORKING_SESSION}.jsonl"))
    with log.open("a") as handle:
        handle.write(json.dumps(assistant_says(text, uuid=uuid)) + "\n")
    return text


def _count(driver: Marionette) -> int:
    found = driver.script(READ_COUNT)
    return int(found) if isinstance(found, int | float) else -1


def _quiet_count(driver: Marionette) -> int:
    found = driver.script_async(COUNT_OVER, QUIET_MS)
    return int(found) if isinstance(found, int | float) else -1


def _refreshes(driver: Marionette, hub: Hub, newest: str) -> int:
    """How many chat fetches one change made, once a second one would have been told."""
    if not _wait_for(driver, newest):
        return -1
    looked = hub.looks
    _until(lambda: hub.looks >= looked + LOOKS_AFTER)
    settle(driver)
    return _count(driver)


def _open_live(driver: Marionette, base: str, tab: Tab, rig: Rig) -> list[str]:
    driver.send("WebDriver:Navigate", {"url": base + tab.state.path})
    if not _wait_for(driver, tab.newest):
        return [f"{tab.name}: never drew its newest turn"]
    if not _until(lambda: _hub(rig).listeners >= 1):
        return [f"{tab.name}: the page never opened its event stream"]
    settle(driver)
    driver.script(COUNT_CHAT)
    return []


def twenty(driver: Marionette, base: str, tab: Tab, _rig: Rig, _home: Path) -> list[str]:
    """A tool-heavy transcript opens on twenty messages, with a way to the page above."""
    driver.send("WebDriver:Navigate", {"url": base + tab.state.path})
    if not _wait_for(driver, tab.newest):
        return [f"{tab.name}: never drew its newest turn"]
    seen = driver.script(EARLIER, "")
    if not isinstance(seen, dict):
        return [f"{tab.name}: never settled"]
    problems: list[str] = []
    if seen.get("count") != PAGE_MESSAGES:
        problems.append(f"{tab.name}: opened on {seen.get('count')} messages, not {PAGE_MESSAGES}")
    if not seen.get("more"):
        problems.append(f"{tab.name}: offers no way to the earlier messages")
    return problems


def earlier(driver: Marionette, base: str, tab: Tab, _rig: Rig, _home: Path) -> list[str]:
    """Load earlier lands a page above the reader, who stays on the message they were reading,
    even when the page was short enough that the reader was also at the end."""
    driver.send("WebDriver:Navigate", {"url": base + tab.state.path})
    if not _wait_for(driver, tab.newest):
        return [f"{tab.name}: never drew its newest turn"]
    driver.script("window.scrollTo(0, 0);")
    settle(driver, READER_STILL_MS)
    before = driver.script(EARLIER, "")
    if not isinstance(before, dict) or not before.get("key") or not before.get("more"):
        return [f"{tab.name}: no message in view under a Load earlier button"]
    if not before.get("atEnd"):
        return [f"{tab.name}: the first page is not at its end, so following it proves nothing"]
    driver.script(_click_earlier())
    key = str(before.get("key"))
    grown: dict[str, object] = {}

    def landed() -> bool:
        nonlocal grown
        found = driver.script(EARLIER, key)
        grown = found if isinstance(found, dict) else {}
        return int(str(grown.get("count") or 0)) > int(str(before.get("count") or 0))

    if not _until(landed):
        return [f"{tab.name}: Load earlier never landed a page"]
    settle(driver)
    after = driver.script(EARLIER, key)
    after = after if isinstance(after, dict) else {}
    problems: list[str] = []
    want = 2 * int(str(before.get("count")))
    if after.get("count") != want:
        problems.append(
            f"{tab.name}: Load earlier showed {after.get('count')} messages, not {want}"
        )
    start, end = _num(before, "top"), _num(after, "top")
    if not abs(end - start) <= DRIFT:
        problems.append(
            f"{tab.name}: Load earlier moved the message being read from {start:.0f}px to {end:.0f}px"
        )
    return problems


def _click_earlier() -> str:
    return 'document.querySelector(".chat__earlier-button").click(); return true;'


def refreshed_once(driver: Marionette, base: str, tab: Tab, rig: Rig, home: Path) -> list[str]:
    """A live page does not poll, and one change to the transcript is exactly one refresh."""
    problems = _open_live(driver, base, tab, rig)
    if problems:
        return problems
    if quiet := _quiet_count(driver):
        problems.append(f"{tab.name}: a live page fetched its chat {quiet}x with no change")
    driver.script(COUNT_CHAT)
    made = _refreshes(driver, _hub(rig), _append(home, "Event reply one.", "ev1"))
    if made < 0:
        return [*problems, f"{tab.name}: the change never reached the page"]
    if made != 1:
        problems.append(f"{tab.name}: one change made {made} chat refreshes, not 1")
    return problems


def _refusing(app: ASGIApp) -> ASGIApp:
    """``app``, with its event stream down."""

    async def down(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"] == "/events":
            await send({"type": "http.response.start", "status": 503, "headers": []})
            await send({"type": "http.response.body", "body": b""})
            return
        await app(scope, receive, send)

    return down


def falls_back(
    mount: Callable[[ASGIApp], None],
) -> Callable[[Marionette, str, Tab, Rig, Path], list[str]]:
    """The stream-loss walk, able to take the stream down by remounting the app."""

    def walk(driver: Marionette, base: str, tab: Tab, rig: Rig, home: Path) -> list[str]:
        """With the stream gone the page polls; once it is back the polling stops."""
        problems = _open_live(driver, base, tab, rig)
        if problems:
            return problems
        hub = _hub(rig)
        driver.script(WATCH_STREAM)
        mount(_refusing(rig.http.app))
        hub.drop_all()
        if not _until(lambda: _count(driver) >= MIN_FALLBACK_POLLS, RECONNECT_SECONDS):
            problems.append(f"{tab.name}: with the stream down the page polled {_count(driver)}x")
        newest = _append(home, "Polled reply one.", "pl1")
        if not _wait_for(driver, newest):
            problems.append(f"{tab.name}: a change never arrived while the stream was down")
        mount(rig.http.app)
        if not _until(lambda: hub.listeners >= 1, RECONNECT_SECONDS):
            return [*problems, f"{tab.name}: the stream never came back"]
        # Open in the page too: its one refresh for what it missed has then been asked for.
        if not _until(lambda: driver.script(STREAM_OPEN) is True):
            return [*problems, f"{tab.name}: the page never saw its stream come back"]
        settle(driver)
        if quiet := _quiet_count(driver):
            problems.append(f"{tab.name}: still polling ({quiet}x) after the stream came back")
        driver.script(COUNT_CHAT)
        made = _refreshes(driver, hub, _append(home, "Recovered reply one.", "rc1"))
        if made < 0:
            return [*problems, f"{tab.name}: the recovered stream never brought a change"]
        if made != 1:
            problems.append(f"{tab.name}: after recovery one change made {made} refreshes")
        return problems

    return walk


# A reader scrolling up from the end in small steps while the agent works and the region is
# redrawn under them. Each step is a user scroll; the walk counts every return to the end and
# measures, across each redraw, the line the reader was reading.
STEP = 10
REDRAW_SECONDS = 0.3
SCROLL_UP = r"""
const [step, every, count] = arguments;
const walk = { steps: 0, swaps: [], atEnd: 0, done: false };
window.__hpWalk = walk;
const units = () => [...document.querySelectorAll(
  "#agent-view .turn, #agent-view > .pane > .pane__line")];
const under = () => document.querySelector(".topbar").getBoundingClientRect().bottom;
const gap = () => document.documentElement.scrollHeight - innerHeight - scrollY;
let held = null;
document.addEventListener("htmx:beforeSwap", (event) => {
  held = null;
  if (!walk.steps || !event.detail.target || event.detail.target.id !== "agent-view") return;
  const first = units().find((u) => u.getBoundingClientRect().top >= under());
  held = first ? { text: first.textContent, top: first.getBoundingClientRect().top } : null;
});
document.addEventListener("htmx:afterSwap", () => {
  if (!held) return;
  const same = units().find((u) => u.textContent === held.text);
  walk.swaps.push(same ? Math.round(same.getBoundingClientRect().top - held.top) : null);
  held = null;
});
addEventListener("scroll", () => { if (walk.steps && gap() <= 2) walk.atEnd += 1; });
const timer = setInterval(() => {
  scrollBy(0, -step);
  walk.steps += 1;
  if (walk.steps >= count) { clearInterval(timer); walk.done = true; }
}, every);
return true;
"""
READ_WALK = r"""
const walk = window.__hpWalk || {};
const gap = document.documentElement.scrollHeight - innerHeight - scrollY;
return { done: !!walk.done, steps: walk.steps || 0, swaps: walk.swaps || [],
  atEnd: walk.atEnd || 0, gap: gap };
"""


def _walked(driver: Marionette) -> dict[str, object]:
    found = driver.script(READ_WALK)
    return found if isinstance(found, dict) else {}


def _redraw_while(driver: Marionette, tab: Tab, rig: Rig, home: Path) -> int:
    """Redraw the region on a cadence until the reader's walk is done; the last count drawn."""
    n = 1

    def walked() -> bool:
        nonlocal n
        if _walked(driver).get("done"):
            return True
        tab.grow(rig, home, n)
        driver.script(POKE)
        n += 1
        return False

    _ = eventually(walked, WAIT_SECONDS, step=REDRAW_SECONDS)
    return n


def _poked(driver: Marionette, newest: str) -> bool:
    if newest in str(_probe(driver).get("text", "")):
        return True
    driver.script(POKE)
    return False


def _reply(_rig: Rig, home: Path, n: int) -> str:
    """The agent appends one more reply, the way it writes; return its text."""
    return _append(home, f"Appended reply number {n}.", f"app{n}")


def _walk_problems(name: str, walk: dict[str, object], travelled: int) -> list[str]:
    problems: list[str] = []
    if walk.get("atEnd"):
        problems.append(f"{name}: a redraw pulled the reader back to the end {walk['atEnd']}x")
    swaps = walk.get("swaps")
    moved = [s for s in swaps if s is None or abs(s) > DRIFT] if isinstance(swaps, list) else []
    if moved:
        problems.append(f"{name}: a redraw moved the line being read by {moved}px")
    if _num(walk, "gap") < travelled / 2:
        problems.append(
            f"{name}: the reader ended {_num(walk, 'gap'):.0f}px from the end "
            f"after scrolling {travelled}px up"
        )
    return problems


def scrolling_up(
    every: float, count: int
) -> Callable[[Marionette, str, Tab, Rig, Path], list[str]]:
    """The walk for one cadence: ``count`` steps up, one every ``every`` seconds."""

    def walk(driver: Marionette, base: str, tab: Tab, rig: Rig, home: Path) -> list[str]:
        """Scroll up from the end in small steps while the region is redrawn: never pulled
        back, and the line being read stays where it was across every redraw."""
        driver.send("WebDriver:Navigate", {"url": base + tab.state.path})
        seen = _wait_for(driver, tab.newest)
        if not seen or not seen.get("scrollable"):
            return [f"{tab.name}: never drew a page taller than the screen"]
        driver.script(SCROLL_UP, STEP, round(every * 1000), count)
        n = _redraw_while(driver, tab, rig, home)
        if not _walked(driver).get("done"):
            return [f"{tab.name}: the reader's walk never finished"]
        newest = tab.grow(rig, home, n)
        # A poke while a redraw is in flight is dropped (htmx's default sync), so poke until
        # the newest line is drawn.
        poked = _until(lambda: _poked(driver, newest), step=REDRAW_SECONDS)
        if not poked or not _wait_for(driver, newest):
            return [f"{tab.name}: the redraw after the reader stopped never landed"]
        walk = _walked(driver)
        if not walk.get("swaps"):
            return [f"{tab.name}: no redraw landed while the reader was scrolled up"]
        return _walk_problems(tab.name, walk, STEP * count)

    return walk


# The keyboard as an iPhone reports it in a device report with the keyboard up: innerHeight
# and scrollY are the visible band's, which sits inside a taller layout viewport whose bottom the
# composer is fixed to. Here the window is the band, so innerHeight and scrollY are real; only
# the layout viewport's height and the band's offset in it are told. Not an iPhone: it checks
# that the dock is measured against the numbers WebKit gives, not how iOS moves anything.
KEYBOARD_PX = 413
KEYBOARD_STEPS = 40
KEYBOARD = r"""
const [layout] = arguments;
const root = document.documentElement;
const view = window.visualViewport;
const band = window.innerHeight;
const kb = { top: layout - band, wrong: [], shut: [], pans: 0 };
window.__hpKb = kb;
document.querySelector("#composer textarea").focus({ preventScroll: true });
Object.defineProperty(root, "clientHeight", { configurable: true, get: () => layout });
Object.defineProperty(view, "offsetTop", { configurable: true, get: () => kb.top });
// The band pans inside the layout viewport, which moves only once the band reaches its edge.
let layoutTop = Math.max(0, root.scrollHeight - layout);
const pan = () => {
  layoutTop = Math.min(Math.max(layoutTop, scrollY + band - layout), scrollY);
  layoutTop = Math.min(Math.max(layoutTop, 0), Math.max(0, root.scrollHeight - layout));
  kb.top = scrollY - layoutTop;
  view.dispatchEvent(new Event("scroll"));
  const at = kb.top;
  kb.pans += 1;
  frames(2, () => look(at, 0));
};
const frames = (n, then) => (n ? requestAnimationFrame(() => frames(n - 1, then)) : then());
// Judged once the dock has had its frame, and again later: a newer pan, or a busy machine's late
// frame, is not a wrong lift, but one still wrong ten frames on is.
const look = (at, tries) => {
  if (kb.top !== at) return;
  const lift = parseFloat(root.style.getPropertyValue("--kb")) || 0;
  const want = layout - band - at;
  const open = root.classList.contains("is-keyboard");
  if (Math.abs(lift - want) <= 1 && open) return;
  if (!tries) {
    frames(10, () => look(at, 1));
  } else {
    if (Math.abs(lift - want) > 1) kb.wrong.push([Math.round(at), lift, Math.round(want)]);
    if (!open) kb.shut.push(Math.round(at));
  }
};
addEventListener("scroll", pan, { passive: true });
view.dispatchEvent(new Event("resize"));
pan();
return true;
"""
READ_KEYBOARD = "return window.__hpKb || {};"
# A pan of the band alone, its height and the page's scroll unchanged, as a browser whose scrollY
# is the layout viewport's reports one: the keyboard did not move, so no script may move the page.
PAN_PINNED = r"""
const [tops] = arguments;
const done = arguments[arguments.length - 1];
const kb = window.__hpKb;
const rest = kb.top;
let moves = 0;
const count = (real) => function () { moves += 1; return real.apply(window, arguments); };
const realTo = window.scrollTo;
const realBy = window.scrollBy;
window.scrollTo = count(realTo);
window.scrollBy = count(realBy);
const frames = (n, then) => (n ? requestAnimationFrame(() => frames(n - 1, then)) : then());
const step = (i) => {
  if (i === tops.length) {
    kb.top = rest;
    window.scrollTo = realTo;
    window.scrollBy = realBy;
    done(moves);
    return;
  }
  kb.top = tops[i];
  window.visualViewport.dispatchEvent(new Event("scroll"));
  frames(2, () => step(i + 1));
};
step(0);
"""
PAN_TOPS = [400, 300, 200, 100, 0]
# iOS swaps the band's offset between 0 and the keyboard mid-fling (device-report.jsonl), and the
# composer drawn in that frame carries whatever lift the handler left: it must be right as soon as
# the event returns, not a frame later. A repeat of the same offset writes nothing, because --kb
# sits on <html> and every write restyles the whole chat.
SWAP = r"""
const [tops] = arguments;
const done = arguments[arguments.length - 1];
const kb = window.__hpKb;
const root = document.documentElement;
const view = window.visualViewport;
const rest = kb.top;
const late = [];
const frames = (n, then) => (n ? requestAnimationFrame(() => frames(n - 1, then)) : then());
for (const top of tops) {
  kb.top = top;
  view.dispatchEvent(new Event("scroll"));
  const lift = parseFloat(root.style.getPropertyValue("--kb")) || 0;
  const want = Math.round(root.clientHeight - window.innerHeight - top);
  if (Math.abs(lift - want) > 1) late.push([top, lift, want]);
}
frames(2, () => {
  let writes = 0;
  const style = root.style;
  const real = style.setProperty;
  style.setProperty = function (name) {
    if (name === "--kb") writes += 1;
    return real.apply(style, arguments);
  };
  view.dispatchEvent(new Event("scroll"));
  frames(2, () => {
    delete style.setProperty;
    kb.top = rest;
    view.dispatchEvent(new Event("scroll"));
    done({ late: late, writes: writes });
  });
});
"""
SWAP_TOPS = [0, 241.7, 413]
# After a downward scroll iOS leaves the band at the layout viewport's bottom (offsetTop 413 in
# device-report.jsonl), so the keyboard's pill sits below the layout viewport, where a fixed box's
# overflow is not drawn: the page under it must carry the composer's colour. A fling moves the page
# for many frames before a script hears of it, so this flings without visualViewport events and
# looks, then lets one event through and looks again: nothing of the composer's colour may be
# missing under the pill or lie over the chat above the composer, and the page may not grow. Past
# the page's end iOS paints its under-page colour, which WebKit takes from <html>'s background with
# <body>'s blended over it, so <body> must not paint an opaque colour of its own there.
PILL_PX = 44
FLING_STEPS = 5
FLING_PX = 80
FLING = r"""
const [pill, steps, px] = arguments;
const done = arguments[arguments.length - 1];
const kb = window.__hpKb;
const root = document.documentElement;
const view = window.visualViewport;
const bar = document.getElementById("composer");
const colour = getComputedStyle(bar).backgroundColor;
const frames = (n, then) => (n ? requestAnimationFrame(() => frames(n - 1, then)) : then());
const painted = () => [...document.querySelectorAll("body *")].filter((el) => {
  if (el.closest("#composer, .topbar")) return false;
  const style = getComputedStyle(el);
  if (style.backgroundColor !== colour || style.display === "none") return false;
  const box = el.getBoundingClientRect();
  return box.height > 0 && box.left <= 1 && box.right >= innerWidth - 1;
}).map((el) => el.getBoundingClientRect());
const look = (when) => {
  const band = window.innerHeight;
  const boxes = painted();
  let reach = band;
  for (const box of boxes.slice().sort((a, b) => a.top - b.top)) {
    if (box.top <= reach + 0.5 && box.bottom > reach) reach = box.bottom;
  }
  const floor = band - bar.offsetHeight - 1;
  const over = boxes.filter((box) => box.top < floor).map((box) => Math.round(box.top));
  return { when, band, reach: Math.round(reach), over };
};
window.scrollBy(0, -(steps * px + 2 * innerHeight));
frames(3, () => {
  const before = root.scrollHeight;
  kb.top = root.clientHeight - window.innerHeight;
  view.dispatchEvent(new Event("scroll"));
  const seen = [look("at rest")];
  for (let i = 0; i < steps; i++) window.scrollBy(0, px);
  seen.push(look("mid-fling, no event yet"));
  view.dispatchEvent(new Event("scroll"));
  seen.push(look("after the event"));
  done({
    seen: seen,
    pill: pill,
    grew: root.scrollHeight - before,
    under: getComputedStyle(document.body).backgroundColor,
    canvas: getComputedStyle(root).backgroundColor,
    colour: colour,
  });
});
"""


def _pill_problems(name: str, found: object) -> list[str]:
    fling = found if isinstance(found, dict) else {}
    problems: list[str] = []
    colour = fling.get("colour")
    if fling.get("canvas") != colour:
        problems.append(f"{name}: <html> is {fling.get('canvas')} under the pill, not {colour}")
    if fling.get("under") not in (colour, "rgba(0, 0, 0, 0)"):
        problems.append(
            f"{name}: <body> paints {fling.get('under')}, which iOS blends over <html> past the "
            "page's end"
        )
    if fling.get("grew") != 0:
        problems.append(f"{name}: covering the pill grew the page by {fling.get('grew')}px")
    seen = fling.get("seen")
    for look in seen if isinstance(seen, list) else []:
        at = look if isinstance(look, dict) else {}
        if _num(at, "reach") - _num(at, "band") < _num(fling, "pill"):
            problems.append(f"{name}: {at.get('when')}, chat shows under the pill: {at}")
        if at.get("over"):
            problems.append(f"{name}: {at.get('when')}, the backdrop lies over the chat: {at}")
    return problems


def _swap_problems(name: str, found: object) -> list[str]:
    swap = found if isinstance(found, dict) else {}
    problems: list[str] = []
    if swap.get("late"):
        problems.append(
            f"{name}: the lift trailed an offset swap past the event that told it "
            f"([band offset, lift, wanted]): {swap['late']}"
        )
    if swap.get("writes") != 0:
        problems.append(f"{name}: an unchanged offset rewrote --kb {swap.get('writes')}x")
    return problems


def keyboard_up(driver: Marionette, base: str, tab: Tab, rig: Rig, home: Path) -> list[str]:
    """With the keyboard up, scroll up through the chat while it is redrawn: the composer rides
    the band's bottom edge at every step, the keyboard reads as open throughout, and the reader
    is neither pulled back nor moved."""
    driver.send("WebDriver:Navigate", {"url": base + tab.state.path})
    seen = _wait_for(driver, tab.newest)
    if not seen or not seen.get("scrollable"):
        return [f"{tab.name}: never drew a page taller than the band"]
    driver.script(KEYBOARD, HEIGHT)
    settle(driver)
    moves = driver.script_async(PAN_PINNED, PAN_TOPS)
    pans = [f"{tab.name}: a pan of the band moved the pinned page {moves}x"] if moves else []
    pans += _swap_problems(tab.name, driver.script_async(SWAP, SWAP_TOPS))
    driver.script(SCROLL_UP, STEP, 50, KEYBOARD_STEPS)
    n = _redraw_while(driver, tab, rig, home)
    newest = tab.grow(rig, home, n)
    poked = _until(lambda: _poked(driver, newest), step=REDRAW_SECONDS)
    if not poked or not _wait_for(driver, newest):
        return [f"{tab.name}: the redraw after the reader stopped never landed"]
    found = driver.script(READ_KEYBOARD)
    kb = found if isinstance(found, dict) else {}
    problems = pans + _walk_problems(tab.name, _walked(driver), STEP * KEYBOARD_STEPS)
    if _num(kb, "pans") <= 1:
        problems.append(f"{tab.name}: the band never moved")
    if kb.get("wrong"):
        problems.append(
            f"{tab.name}: the composer was not lifted to the band's bottom edge "
            f"([band offset, lift, wanted]): {kb['wrong'][:4]}"
        )
    if kb.get("shut"):
        problems.append(f"{tab.name}: the keyboard read as closed at band offsets {kb['shut'][:4]}")
    fling = driver.script_async(FLING, PILL_PX, FLING_STEPS, FLING_PX)
    return problems + _pill_problems(tab.name, fling)


def _scroll_pane(rig: Rig, _home: Path, n: int) -> str:
    """The terminal scrolls: its oldest line goes and a newer one arrives; return that one."""
    text = "".join(
        f"\x1b[32m$\x1b[0m step {k:03d}: applied to the cluster\n" for k in range(n, n + LINES)
    )
    rig.wire.replies[READ] = read_frame(text)
    return f"step {n + LINES - 1:03d}"


# The turn the reader is on (the first whose top is below the bar), and how many are drawn; and,
# from the moment it is armed, whether the log was ever left with no turns at all.
PLACE = r"""
const under = document.querySelector(".topbar").getBoundingClientRect().bottom;
const turns = [...document.querySelectorAll("#agent-view .turn")];
const on = turns.find((t) => t.getBoundingClientRect().top >= under);
const note = document.getElementById("chat-note");
const busy = document.querySelector(".htmx-request, .htmx-swapping, .htmx-settling");
return { key: on ? on.getAttribute("data-hp-key") : "", top: on ? on.getBoundingClientRect().top : null,
  turns: turns.length, note: note ? note.textContent : "", blank: !!window.__hpBlank, busy: !!busy };
"""
ARM_BLANK = r"""
window.__hpBlank = false;
new MutationObserver(() => {
  if (!document.querySelector("#agent-view .turn")) window.__hpBlank = true;
}).observe(document.body, { childList: true, subtree: true });
"""
HELD_NOTE = "could not be read just now"


def _place(driver: Marionette) -> dict[str, object]:
    found = driver.script(PLACE)
    return found if isinstance(found, dict) else {}


def _place_when(driver: Marionette, done: Callable[[dict[str, object]], bool]) -> dict[str, object]:
    if not _until(lambda: done(_place(driver))):
        return {}
    settle(driver)
    return _place(driver)


def _kept(name: str, was: dict[str, object], now: dict[str, object]) -> list[str]:
    problems = [f"{name}: the log was left with no turns"] if now.get("blank") else []
    if now.get("turns") != was.get("turns"):
        problems.append(f"{name}: {was.get('turns')} turns drawn, then {now.get('turns')}")
    if now.get("key") != was.get("key") or abs(_num(now, "top") - _num(was, "top")) > DRIFT:
        problems.append(
            f"{name}: the reader was on {was.get('key')} at {_num(was, 'top'):.0f}px, "
            f"then {now.get('key')} at {_num(now, 'top'):.0f}px"
        )
    return problems


def locked(driver: Marionette, base: str, tab: Tab, _rig: Rig, home: Path) -> list[str]:
    """A refresh that cannot read the log (its harness holds the store locked) keeps the turns
    drawn and the reader on the one they were reading, says so under them, and heals."""
    driver.send("WebDriver:Navigate", {"url": base + tab.state.path})
    if not _wait_for(driver, tab.newest):
        return [f"{tab.name}: never drew its newest turn"]
    driver.script("window.scrollTo(0, 400);")
    settle(driver, READER_STILL_MS)
    was = _place(driver)
    if not was.get("key"):
        return [f"{tab.name}: no turn under the bar to read"]
    log = next((home / "transcripts").rglob(f"{WORKING_SESSION}.jsonl"))
    driver.script(ARM_BLANK)
    log.chmod(0)
    try:
        driver.script(POKE)
        held = _place_when(
            driver, lambda p: HELD_NOTE in str(p.get("note", "")) or bool(p.get("blank"))
        )
    finally:
        log.chmod(0o600)
    if not held:
        return [f"{tab.name}: a locked refresh never said the log could not be read"]
    problems = _kept(f"{tab.name}, locked", was, held)
    driver.script(POKE)
    healed = _place_when(
        driver, lambda p: HELD_NOTE not in str(p.get("note", "")) and not p.get("busy")
    )
    if not healed:
        return [*problems, f"{tab.name}: the note stayed once the log could be read again"]
    return problems + _kept(f"{tab.name}, healed", was, healed)


def _tabs() -> tuple[tuple[Tab, Callable[[Marionette, str, Tab, Rig, Path], list[str]]], ...]:
    blocked = runner(lambda: herd(**BLOCKED), long_pane(LINES) + capture(MENU))
    chat = State("chat", f"{AGENT}?tab=chat", "", runner(snapshot_frame), long_chat(TURNS))
    pane = State("pane", f"{AGENT}?tab=pane", "", runner(snapshot_frame, long_pane(LINES)))
    asking_chat = State("asking chat", chat.path, "", blocked, long_chat(TURNS))
    asking_pane = State("asking pane", pane.path, "", blocked)
    last_reply = f"Reply {TURNS - 1}:"
    [plan] = pick(["agent-answer-plan"])
    [ticks] = pick(["agent-answer-multiselect"])
    [strip] = pick(["usage-chat"])
    last_line = f"step {LINES - 1:03d}"
    tool = State("tool result", chat.path, "", runner(snapshot_frame), _tool_chat(TURNS))
    folds = State("tool burst", chat.path, "", runner(snapshot_frame), _burst_chat(BURST))
    heavy = State("tool heavy", chat.path, "", runner(snapshot_frame), tool_heavy_chat())
    short = State(
        "short page",
        chat.path,
        "",
        runner(snapshot_frame),
        tool_heavy_chat(),
        settings={"chat_messages": 3},
    )
    live = State("live chat", chat.path, "", runner(snapshot_frame), long_chat(TURNS))
    live = State(live.name, live.path, "", live.replies, live.transcript, settings=LIVE_SETTINGS)
    return (
        (Tab("twenty messages", heavy, "tool calls", _say), twenty),
        (Tab("load earlier", short, "tool calls", _say), earlier),
        (Tab("one refresh", live, last_reply, _say), refreshed_once),
        (Tab("tool burst", folds, "Reply after the burst", _say), burst),
        (Tab("tool result", tool, "Reply after the tests", _say), kept_open),
        (Tab("chat end", chat, last_reply, _say), follow),
        (Tab("unheard scroll", chat, last_reply, _say), unheard_scroll),
        (Tab("unheard read", chat, last_reply, _say), unheard_read),
        (Tab("locked log", chat, last_reply, _say), locked),
        (Tab("pane unheard read", pane, last_line, _scroll_pane), unheard_read),
        (Tab("strip under a finger", strip, "", _say), strip_under_finger),
        (Tab("pane end", pane, last_line, _print), follow),
        (Tab("pane cold", pane, last_line, _print), cold),
        (Tab("chat cold", chat, last_reply, _say), cold),
        (Tab("pane nudged up", pane, last_line, _scroll_pane), scrolling_up(0.4, 10)),
        (Tab("pane scrolled up", pane, last_line, _scroll_pane), scrolling_up(0.05, 40)),
        (Tab("chat nudged up", chat, last_reply, _reply), scrolling_up(0.4, 10)),
        (Tab("chat scrolled up", chat, last_reply, _reply), scrolling_up(0.05, 40)),
        (
            Tab("chat keyboard up", chat, last_reply, _reply, height=HEIGHT - KEYBOARD_PX),
            keyboard_up,
        ),
        (Tab("chat question", asking_chat, last_reply, _say), asked),
        (Tab("pane question", asking_pane, "", _print), asked),
        (Tab("chat question waiting", asking_chat, last_reply, _say), waiting_pill),
        (Tab("pane question waiting", asking_pane, "", _print), waiting_pill),
        (Tab("tall plan", plan, "", _print, width=320, height=568), tall_question),
        (
            Tab("tall multiselect", ticks, "", _print, width=430, height=932, scale=2.0),
            tall_question,
        ),
    )


def _walks(
    mount: Callable[[ASGIApp], None],
) -> list[tuple[Tab, Callable[[Marionette, str, Tab, Rig, Path], list[str]]]]:
    chat = State("stream loss", f"{AGENT}?tab=chat", "", runner(snapshot_frame), long_chat(TURNS))
    lost = State(chat.name, chat.path, "", chat.replies, chat.transcript, settings=LIVE_SETTINGS)
    return [*_tabs(), (Tab("stream loss", lost, f"Reply {TURNS - 1}:", _say), falls_back(mount))]


def names() -> list[str]:
    """Every walk's name, in the order ``run`` walks them."""
    return [tab.name for tab, _walk in _walks(lambda _app: None)]


def run(
    driver: Marionette,
    base: str,
    mount: Callable[[ASGIApp], None],
    scratch: Path,
    only: frozenset[str] | None = None,
) -> list[str]:
    """Every walk, or those ``only`` names, on a fresh app in its own phone window; empty when
    each tab keeps its end."""
    problems: list[str] = []
    for tab, walk in _walks(mount):
        if only is not None and tab.name not in only:
            continue
        home = scratch / f"end-{tab.name.replace(' ', '-')}"
        home.mkdir(parents=True)
        rig = build(tab.state, home)
        mount(rig.http.app)
        first = driver.handles()[0]
        phone_window(driver, tab.width, tab.height)
        try:
            problems += walk(driver, base, tab, rig, home)
        finally:
            driver.send("WebDriver:CloseWindow", {})
            driver.send("WebDriver:SwitchToWindow", {"handle": first})
        print(f"  walked {tab.name}", flush=True)
    return problems
