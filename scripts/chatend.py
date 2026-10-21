"""Drive the Chat and Pane tabs in headless Firefox: each opens on its newest line, follows new
lines while the reader is at the end, and leaves a reader who scrolled up where they were. An
agent that is asking lands at the end too, its question after the newest line and on the first
screen, even under a view with no line to land on, and a reader who scrolled up from it is
offered the way back; a link to the Pane opens
it the same way, from either tab. A tool's
result the reader opened stays open across a poll, and the page does not move under them. A
burst of tool calls lands folded, its failure in plain sight, and a fold the reader opened
stays open while calls are added to it.

The chat is paged by messages and live over an event stream: a tool-heavy transcript opens on
twenty messages, "Load earlier" lands the page above without moving the reader, one change is
one refresh, and a lost stream falls back to polling and then recovers.

A reader who scrolls up from the end in small steps while the agent works is never pulled back
to it, and the line they are reading stays where it was across every redraw, even one that lands
before their scroll is heard. On a slow link, where a frame runs before the page's last script
arrives, the chat still opens on its newest line, and a question still lands at the end when the
stylesheet applies after the page's scripts have run."""

import asyncio
import contextlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Final

from firefox import Bench, Marionette, phone_window
from pydantic import JsonValue
from settle import READER_STILL_MS, settle
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from herdr_pwa.web.events import Hub
from tests.catalogue import (
    AGENT,
    BLOCKED,
    MENU,
    PLAN,
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
    SNAPSHOT,
    WORKING_SESSION,
    Rig,
    assistant_says,
    read_frame,
    snapshot_frame,
    write_transcript,
)

WIDTH: Final = 390
HEIGHT: Final = 844
WAIT_SECONDS: Final = 15.0
# A reader's place may move by a rounding error, never by a line.
DRIFT: Final = 3
TURNS: Final = 24
LINES: Final = 120
# The newest line of the pane walks' terminal.
LAST_LINE: Final = f"step {LINES - 1:03d}"

# Where the newest turn or terminal line, and the question if any, sit against the dock.
PROBE: Final = r"""
const units = document.querySelectorAll(
  "#agent-view .turn, #agent-view > .pane > .pane__line");
const last = units[units.length - 1];
// The dock's top is the status row's, which sits fixed on the composer.
const bar = document.getElementById("agent-tray") || document.getElementById("composer");
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
POKE: Final = "htmx.trigger(document.body, 'hp:poke');"
# Lines in the tool's result: open, it is taller than the screen.
RESULT_LINES: Final = 120
# How far under the dock's top edge the newest turn may end and still count as the end (app.js).
END_SLACK: Final = 120

# Open the last tool result and bring the middle of its output to the middle of the screen.
OPEN_RESULT: Final = r"""
const details = [...document.querySelectorAll("#agent-view details.tool")].pop();
if (!details) return false;
details.querySelector("summary").click();
const out = details.querySelector(".tool__result").getBoundingClientRect();
window.scrollBy(0, out.top + out.height / 2 - innerHeight / 2);
window.__hpSwaps = 0;
document.addEventListener("htmx:afterSettle", () => { window.__hpSwaps += 1; });
return true;
"""
READ_RESULT: Final = r"""
const details = [...document.querySelectorAll("#agent-view details.tool")].pop();
const busy = document.querySelector(".htmx-request, .htmx-swapping, .htmx-settling");
return { open: !!(details && details.open), scrollY: window.scrollY,
  swaps: window.__hpSwaps || 0, busy: !!busy };
"""


# The burst: calls in a row, the third of them failing, then the agent's reply.
BURST: Final = 6
FAILS_AT: Final = 2
# Open the burst's first fold and its first call, and bring the fold to the middle of the screen.
OPEN_FOLD: Final = r"""
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
READ_FOLD: Final = r"""
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
CLOSE_FOLD: Final = r"""
const fold = document.querySelector("#agent-view details.tools");
if (!fold) return null;
fold.querySelector("summary").click();
return { open: fold.open, inner: !!fold.querySelector("details.tool[open]") };
"""
# Redraw the named regions: the answer region alone, or every region on the page.
REDRAW: Final = r"""
const ids = arguments[0];
for (const id of ids) document.getElementById(id).__hpOld = true;
if (ids.length === 1) htmx.trigger(document.getElementById(ids[0]), "hp:stale");
else htmx.trigger(document.body, "hp:poke");
"""
REDRAWN: Final = r"""
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


@dataclass(frozen=True)
class WalkCtx:
    """What one walk is handed: the browser, the app's address, the tab, its rig and its home."""

    driver: Marionette
    base: str
    tab: Tab
    rig: Rig
    home: Path

    def open(self) -> None:
        """Load the tab's page from its address."""
        self.driver.send("WebDriver:Navigate", {"url": self.base + self.tab.state.path})

    def grow(self, n: int) -> str:
        """Grow the tab by its ``n``th newest line; return that line's text."""
        return self.tab.grow(self.rig, self.home, n)


type Walk = Callable[[WalkCtx], list[str]]


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


def _pane() -> State:
    return State("pane", f"{AGENT}?tab=pane", "", runner(snapshot_frame, long_pane(LINES)))


def pane_tab(name: str) -> Tab:
    """The Pane tab over a terminal taller than the screen, which prints a newest line per grow."""
    return Tab(name, _pane(), LAST_LINE, _print)


def asking_pane_tab(name: str) -> Tab:
    """The Pane tab of an agent asking a question under a terminal taller than the screen."""
    blocked = runner(lambda: herd(**BLOCKED), long_pane(LINES) + capture(MENU))
    return Tab(name, State("asking pane", _pane().path, "", blocked), "", _print)


def plan_coming_tab(name: str) -> Tab:
    """The Pane tab at 320px with the plan on its screen while herdr still reports it working."""
    working = runner(snapshot_frame, long_pane(LINES) + capture(PLAN))
    return Tab(name, State("plan coming", _pane().path, "", working), "", _print, 320, 568)


def _plan_printed(rig: Rig, _home: Path, _n: int) -> str:
    """Make the terminal print the plan under its scrollback; return the plan's last line."""
    plan = capture(PLAN)
    rig.wire.replies[READ] = read_frame(long_pane(LINES) + plan)
    return plan.rstrip().rsplit("\n", 1)[-1].strip()


def plan_printing_tab(name: str, width: int = 320, height: int = 568) -> Tab:
    """The Pane tab of a working agent whose terminal prints the plan as it comes to ask it."""
    return Tab(name, _pane(), LAST_LINE, _plan_printed, width, height)


def _scrolled_up_stays(ctx: WalkCtx) -> list[str]:
    driver, tab = ctx.driver, ctx.tab
    driver.script("window.scrollTo(0, 200);")
    settle(driver, READER_STILL_MS)
    before = _num(_idle(driver), "scrollY")
    newest = ctx.grow(2)
    driver.script(POKE)
    after = _num(_wait_for(driver, newest), "scrollY")
    if abs(after - before) <= DRIFT:
        return []
    return [
        f"{tab.name}: a poll moved a reader who had scrolled up from {before:.0f}px to {after:.0f}px"
    ]


def follow(ctx: WalkCtx) -> list[str]:
    """Open on the end, follow a new line there, then scroll up and stay put."""
    driver, tab = ctx.driver, ctx.tab
    ctx.open()
    seen = _wait_for(driver, tab.newest)
    if not seen:
        return [f"{tab.name}: never drew its newest line"]
    if not seen.get("scrollable"):
        return [f"{tab.name}: fits on one screen, so it proves nothing"]
    problems = _shown(seen, f"{tab.name}, opened")
    newest = ctx.grow(1)
    driver.script(POKE)
    problems += _shown(_wait_for(driver, newest), f"{tab.name}, after a poll at the end")
    return problems + _scrolled_up_stays(ctx)


# How often the page fetched its own view: a swap would land it by the swap's pin, not the first.
VIEW_READS: Final = r"""
const view = document.getElementById("agent-view");
if (!view) return -1;
const own = new URL(view.getAttribute("data-hp-poll"), location.href).href;
return performance.getEntriesByType("resource").filter((e) => e.name === own).length;
"""


def cold(ctx: WalkCtx) -> list[str]:
    """A tab loaded straight from its address, as a link, a restored tab or a bookmark opens it,
    lands on its newest line with no swap to land it."""
    driver, tab = ctx.driver, ctx.tab
    ctx.open()
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


# A slow link, as a phone's often is, lets a frame run between two of the page's scripts. The page
# is made to measure itself after one script (a resize, as iOS sends while its bars settle), and a
# later one is held back until that frame has run.
FRAME_JS: Final = "/walk/frame.js"
FRAMED: Final = "/walk/framed"
FRAME: Final = (
    f'dispatchEvent(new Event("resize")); requestAnimationFrame(() => fetch("{FRAMED}"));'
)
# The frame lands the page itself, as the dock's first measure does when it falls in that frame.
LANDING_FRAME: Final = f'hp.chatend.toEnd(); requestAnimationFrame(() => fetch("{FRAMED}"));'


@dataclass(frozen=True, slots=True)
class Held:
    """Which script the slow link holds, the script after which the frame runs, and the frame."""

    script: str = "chatend.js"
    after: str = "geometry.js"
    frame: str = FRAME


def _tag(name: str) -> re.Pattern[bytes]:
    return re.compile(
        rb'<script src="/static/' + re.escape(name.encode()) + rb'\?v=[0-9a-f]+" defer></script>'
    )


def _digest(name: str) -> re.Pattern[bytes]:
    return re.compile(rb"(/static/" + re.escape(name.encode()) + rb"\?v=)([0-9a-f]+)")


async def _serve(send: Send, body: bytes, kind: bytes) -> None:
    headers = [(b"content-type", kind), (b"content-length", str(len(body)).encode())]
    await send({"type": "http.response.start", "status": 200, "headers": headers})
    await send({"type": "http.response.body", "body": body})


def _late_page(held: str, after: str) -> Callable[[bytes], bytes]:
    """A page measuring itself after ``after``, with ``held`` fetched afresh."""

    def rewrite(page: bytes) -> bytes:
        # Under a digest the server does not know, the held script is never cached.
        page = _digest(held).sub(lambda m: m[1] + b"0" * len(m[2]), page)
        tag = f'<script src="{FRAME_JS}" defer></script>'.encode()
        return _tag(after).sub(lambda m: m[0] + tag, page)

    return rewrite


def _rewriting(send: Send, rewrite: Callable[[bytes], bytes]) -> Send:
    async def sending(message: Message) -> None:
        if message["type"] == "http.response.start":
            # The page grows by a tag; the rig's own collector sets the length again.
            kept = [(k, v) for k, v in message["headers"] if k.lower() != b"content-length"]
            message = {**message, "headers": kept}
        elif message["type"] == "http.response.body":
            message = {**message, "body": rewrite(bytes(message.get("body", b"")))}
        await send(message)

    return sending


def _unzipped(scope: Scope) -> Scope:
    """``scope`` asking for its answer unzipped, so the page's tags can be rewritten."""
    headers = [(k, v) for k, v in scope["headers"] if k.lower() != b"accept-encoding"]
    return {**scope, "headers": headers}


class SlowLink:
    """``app`` with one script held back until the page has drawn a frame after an earlier one."""

    def __init__(self, app: ASGIApp, held: Held | None = None) -> None:
        self.app = app
        self.scripts = held or Held()
        self.framed = asyncio.Event()
        self.held = False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Serve the frame's script and its signal, hold chatend.js for it, pass the rest."""
        path = str(scope["path"]) if scope["type"] == "http" else ""
        if path == FRAME_JS:
            await _serve(send, self.scripts.frame.encode(), b"text/javascript")
            return
        if path == FRAMED:
            self.framed.set()
            await _serve(send, b"", b"text/plain")
            return
        if path == f"/static/{self.scripts.script}":
            self.held = True
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.framed.wait(), WAIT_SECONDS)
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        await self.app(
            _unzipped(scope),
            receive,
            _rewriting(send, _late_page(self.scripts.script, self.scripts.after)),
        )


def slow_link(
    mount: Callable[[ASGIApp], None],
) -> Walk:
    """The cold walk on a slow link, able to serve the app as one by remounting it."""

    def walk(ctx: WalkCtx) -> list[str]:
        """A frame that ran before chatend.js did leaves the page to land on its newest line."""
        tab, rig = ctx.tab, ctx.rig
        link = SlowLink(rig.http.app)
        mount(link)
        problems = cold(ctx)
        if not (link.held and link.framed.is_set()):
            return [*problems, f"{tab.name}: no frame ran before chatend.js, so it proves nothing"]
        return problems

    return walk


def columns_late(
    mount: Callable[[ASGIApp], None],
) -> Walk:
    """The tall question loaded on a slow link that holds app.js past a frame after chatend.js."""

    def walk(ctx: WalkCtx) -> list[str]:
        """That frame lands the page on the question before app.js shows Keep the columns above
        it; the dock's measure on load then lands it again, with the question's top under the bar."""
        driver, tab = ctx.driver, ctx.tab
        link = SlowLink(ctx.rig.http.app, Held("app.js", "chatend.js", LANDING_FRAME))
        mount(link)
        ctx.open()
        found: dict[str, object] = {}
        if not _until(lambda: bool(found.update(_tall(driver)) or found)):
            return [f"{tab.name}: never drew its question"]
        settle(driver)
        found.update(_tall(driver))
        if not (link.held and link.framed.is_set()):
            return [f"{tab.name}: no frame ran before app.js, so it proves nothing"]
        if not found.get("tall"):
            return [
                f"{tab.name}: the answer fits between the bar and the dock, so it proves nothing"
            ]
        top, under = _num(found, "top"), _num(found, "under")
        if abs(top - under) <= DRIFT:
            return []
        return [
            f"{tab.name}: the question's first line sits at {top:.0f}px, not under the bar ({under:.0f}px)"
        ]

    return walk


# WebKit runs a page's deferred scripts before a stylesheet still loading has applied, and Firefox
# does not, so the walk links the sheet by script for print, holds it past the first frame and
# applies it on arrival. WebKit moves a page landed unstyled as the sheet applies, and that move
# reads as the reader's scroll, so the page must not have moved before then.
STYLE_JS: Final = "/walk/style.js"
STYLE_GO: Final = "/walk/style-go"
APP_CSS: Final = re.compile(rb'<link rel="stylesheet" href="/static/app\.css\?v=[0-9a-f]+">')
# Under a digest the server does not know, app.css is never cached, so every load fetches it.
LATE_CSS: Final = "/static/app.css?v=0"
STYLE: Final = (
    'const link = document.createElement("link"); link.rel = "stylesheet"; link.media = "print"; '
    'link.addEventListener("load", () => { window.hpStyledAt = scrollY; link.media = "all"; }); '
    f'link.href = "{LATE_CSS}"; document.head.append(link); '
    'document.addEventListener("DOMContentLoaded", '
    f'() => requestAnimationFrame(() => fetch("{STYLE_GO}")));'
)


def _late_style(page: bytes) -> bytes:
    return APP_CSS.sub(f'<script src="{STYLE_JS}"></script>'.encode(), page)


class LateStyle:
    """``app`` with its stylesheet linked by a script and held past the page's first frame."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.landed = asyncio.Event()
        self.held = False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Serve the linking script and its signal, hold the stylesheet for it, pass the rest."""
        path = str(scope["path"]) if scope["type"] == "http" else ""
        if path == STYLE_JS:
            await _serve(send, STYLE.encode(), b"text/javascript")
            return
        if path == STYLE_GO:
            self.landed.set()
            await _serve(send, b"", b"text/plain")
            return
        if path == "/static/app.css":
            self.held = True
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.landed.wait(), WAIT_SECONDS)
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        await self.app(_unzipped(scope), receive, _rewriting(send, _late_style))


def late_style(
    mount: Callable[[ASGIApp], None],
) -> Walk:
    """The question walk with the stylesheet arriving after the page's scripts ran."""

    def walk(ctx: WalkCtx) -> list[str]:
        """A page whose style applies after its scripts lands at its question once styled."""
        tab = ctx.tab
        link = LateStyle(ctx.rig.http.app)
        mount(link)
        problems = asked(ctx)
        styled_at = ctx.driver.script("return window.hpStyledAt;")
        if styled_at != 0:
            problems.append(f"{tab.name}: stood at {styled_at}px before its stylesheet applied")
        if _jump(ctx.driver).get("shown"):
            problems.append(f"{tab.name}: offers the way to the question it should have landed on")
        if not (link.held and link.landed.is_set()):
            return [*problems, f"{tab.name}: the stylesheet was not held past the landing"]
        return problems

    return walk


def asked(ctx: WalkCtx) -> list[str]:
    """An agent with a question open lands at the end like any other page, with the question
    after the newest line and it and every option on the first screen, above the dock."""
    driver, tab = ctx.driver, ctx.tab
    ctx.open()
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


GROW_TEXT: Final = r"""
const root = document.documentElement;
root.style.fontSize = parseFloat(getComputedStyle(root).fontSize) * arguments[0] + "px";
return true;
"""
# Where the question's first line sits, and whether the region is taller than the room for it.
TALL: Final = r"""
const busy = document.querySelector(".htmx-request, .htmx-swapping, .htmx-settling");
const question = document.querySelector("#answer .answer__question");
const region = document.getElementById("answer");
if (busy || !question || !region) return null;
const under = document.querySelector(".topbar").getBoundingClientRect().bottom;
const dock = (document.getElementById("agent-tray") || document.getElementById("composer"))
  .getBoundingClientRect().top;
const line = parseFloat(getComputedStyle(question).lineHeight) || 24;
return { top: question.getBoundingClientRect().top, line: line, under: under, dock: dock,
  tall: region.getBoundingClientRect().height > dock - under,
  gap: document.documentElement.scrollHeight - innerHeight - scrollY };
"""


def _tall(driver: Marionette) -> dict[str, object]:
    found = driver.script(TALL)
    return found if isinstance(found, dict) else {}


def tall_question(ctx: WalkCtx) -> list[str]:
    """A question taller than the room between the bar and the dock lands with its first line
    just under the bar, not at the very end with only Interrupt showing."""
    driver, tab = ctx.driver, ctx.tab
    ctx.open()
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


def _landed_in_view(name: str, found: dict[str, object], *, printed: bool) -> list[str]:
    top, under, dock = _num(found, "top"), _num(found, "under"), _num(found, "dock")
    if not found.get("tall"):
        if not printed:
            return [f"{name}: the answer fits between the bar and the dock, so it proves nothing"]
        if _num(found, "gap") <= DRIFT and top >= under - DRIFT:
            return []
        return [f"{name}: the question that fits sits at {top:.0f}px, not at the end"]
    if top >= under - DRIFT and top + _num(found, "line") <= dock:
        return []
    where = f"between the bar ({under:.0f}px) and the dock ({dock:.0f}px)"
    return [f"{name}: the question that arrived sits at {top:.0f}px, not {where}"]


STALE: Final = 'htmx.trigger(document.getElementById(arguments[0]), "hp:stale");'


def _question_landed(ctx: WalkCtx, *, printed: bool, answer_first: bool = False) -> list[str]:
    """Turn the agent blocked, the terminal printing the plan first if ``printed``, poke the
    page (the answer region alone first if ``answer_first``, then the pane), and check the
    question lands in view: a tall one with its first line between the bar and the dock, and,
    when the terminal printed it, one that fits at the end."""
    driver, tab = ctx.driver, ctx.tab
    ctx.open()
    seen = _wait_for(driver, tab.newest)
    if not seen or _num(seen, "gap") > DRIFT:
        return [f"{tab.name}: never landed at the end before the question came"]
    if printed:
        _ = ctx.grow(1)
    ctx.rig.wire.replies[SNAPSHOT] = herd(**BLOCKED)
    # The app holds a snapshot for a while; the rig's clock moves past that.
    ctx.rig.clock.sleep(60.0)
    if answer_first:
        driver.script(STALE, "answer")
    else:
        driver.script(POKE)
    if not _until(lambda: bool(_tall(driver))):
        return [f"{tab.name}: the question never arrived"]
    settle(driver)
    if answer_first:
        driver.script(STALE, "agent-view")
        settle(driver)
    found: dict[str, object] = {}
    if not _until(lambda: bool(found.update(_tall(driver)) or found)):
        return [f"{tab.name}: never settled"]
    return _landed_in_view(tab.name, found, printed=printed)


def question_arrives(ctx: WalkCtx) -> list[str]:
    """A question taller than the room that arrives while the reader is at the end lands as one
    loaded with the page does: its first line just under the bar. The screen already shows it, so
    only the answer region redraws."""
    return _question_landed(ctx, printed=False)


def question_printed(ctx: WalkCtx) -> list[str]:
    """The same when the terminal prints the question in the poll that brings it: the pane and
    the answer region both redraw, in either order, and the question still lands in view, at the
    end if it fits."""
    return _question_landed(ctx, printed=True)


def question_printed_after(ctx: WalkCtx) -> list[str]:
    """The order that lost it: the answer region lands on the tall question's top, then the pane
    draws its new lines in above it, and the question must still be in view."""
    return _question_landed(ctx, printed=True, answer_first=True)


JUMP: Final = r"""
const jump = document.getElementById("hp-jump");
const busy = document.querySelector(".htmx-request, .htmx-swapping, .htmx-settling");
const gap = document.documentElement.scrollHeight - innerHeight - scrollY;
return { exists: !!jump, shown: !!jump && !jump.hidden && jump.getBoundingClientRect().height > 0,
  busy: !!busy, gap: gap };
"""


def _jump(driver: Marionette) -> dict[str, object]:
    found = driver.script(JUMP)
    return found if isinstance(found, dict) else {}


def waiting_pill(ctx: WalkCtx) -> list[str]:
    """Scrolled up from a waiting question, the reader is offered a way back, which works."""
    driver, tab = ctx.driver, ctx.tab
    ctx.open()
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


# Marks this document, so a new one shows the tap opened a page rather than jumped in this one.
DOOR_TAP: Final = r"""
window.__hpDoorLeft = true;
const door = document.querySelector("#answer .answer__go");
if (door) setTimeout(() => door.click(), 0);
return { door: !!door, length: history.length };
"""
DOOR_DRAWN: Final = 'return document.querySelector("#answer .answer__go") !== null;'
DOOR_LANDED: Final = r"""
const jump = document.getElementById("hp-jump");
const busy = document.querySelector(".htmx-request, .htmx-swapping, .htmx-settling");
const arrived = location.search === "?tab=pane" && (!window.__hpDoorLeft || location.hash !== "");
return { arrived: arrived && document.readyState === "complete" && !busy
    && !!document.querySelector("#agent-view .pane__line") && !!document.getElementById("answer"),
  gap: document.documentElement.scrollHeight - innerHeight - scrollY,
  pinned: !!(window.hp && hp.chatend && hp.chatend.pinned()),
  pill: !!jump && !jump.hidden, length: history.length };
"""


def pane_door(ctx: WalkCtx) -> list[str]:
    """The answer note's link to the Pane opens it as any opening does: at the end, following
    it, with no way back offered, and in place of the page it was tapped on."""
    driver, tab = ctx.driver, ctx.tab
    ctx.open()
    if not _until(lambda: bool(driver.script(DOOR_DRAWN))):
        return [f"{tab.name}: never drew the link to the Pane"]
    settle(driver)
    tapped = driver.script(DOOR_TAP)
    was = tapped if isinstance(tapped, dict) else {}
    if not _read_until(driver, DOOR_LANDED, lambda s: bool(s.get("arrived"))):
        return [f"{tab.name}: the link never opened the Pane"]
    settle(driver)
    seen = driver.script(DOOR_LANDED)
    seen = seen if isinstance(seen, dict) else {}
    problems: list[str] = []
    if _num(seen, "gap") > DRIFT or not seen.get("pinned") or seen.get("pill"):
        problems.append(
            f"{tab.name}: the Pane opened {_num(seen, 'gap'):.0f}px above its end, "
            f"pinned={seen.get('pinned')}, the way back {'shown' if seen.get('pill') else 'hidden'}"
        )
    if seen.get("length") != was.get("length"):
        problems.append(f"{tab.name}: the link pushed a history entry, so back is not the feed")
    return problems


# The answer region once the view's own read has drawn, whatever it drew.
UNREAD: Final = r"""
const busy = document.querySelector(".htmx-request, .htmx-swapping, .htmx-settling");
const region = document.getElementById("answer");
const view = document.getElementById("agent-view");
if (busy || document.readyState !== "complete" || !view || view.querySelector(".boot")
    || !region || !region.querySelector(".banner--held")) return null;
const jump = document.getElementById("hp-jump");
const box = region.getBoundingClientRect();
return { gap: document.documentElement.scrollHeight - innerHeight - scrollY,
  scrollable: document.documentElement.scrollHeight > innerHeight,
  pinned: !!(window.hp && hp.chatend && hp.chatend.pinned()), pill: !!jump && !jump.hidden,
  top: box.top, bottom: box.bottom, under: document.querySelector(".topbar").getBoundingClientRect().bottom,
  dock: (document.getElementById("agent-tray") || document.getElementById("composer"))
    .getBoundingClientRect().top };
"""


def unread_question(ctx: WalkCtx) -> list[str]:
    """A question the app could not read, under a view with no line to land on (the pane's read
    failed, or the log is empty), still waits at the end, in view above the dock."""
    driver, tab = ctx.driver, ctx.tab
    ctx.open()
    if not _read_until(driver, UNREAD, bool):
        return [f"{tab.name}: never drew its view and the banner for its question"]
    settle(driver)
    seen = driver.script(UNREAD)
    seen = seen if isinstance(seen, dict) else {}
    if not seen.get("scrollable"):
        return [f"{tab.name}: fits on one screen, so it proves nothing"]
    top, low = _num(seen, "top"), _num(seen, "bottom")
    if _num(seen, "gap") > DRIFT or not seen.get("pinned") or seen.get("pill"):
        way = "shown" if seen.get("pill") else "hidden"
        gap = f"{_num(seen, 'gap'):.0f}px"
        return [
            f"{tab.name}: landed {gap} above the end, pinned={seen.get('pinned')}, way back {way}"
        ]
    if top < _num(seen, "under") or low > _num(seen, "dock") + 0.5:
        return [f"{tab.name}: the question sits at {top:.0f}..{low:.0f}px, not above the dock"]
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


def kept_open(ctx: WalkCtx) -> list[str]:
    """Open a tool's result far from the end; a poll leaves it open and the page where it was."""
    driver, tab = ctx.driver, ctx.tab
    ctx.open()
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


def burst(ctx: WalkCtx) -> list[str]:
    """Land folded; open a fold and a call in it; a poll that adds a call leaves both open."""
    driver, tab, home = ctx.driver, ctx.tab, ctx.home
    ctx.open()
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
FINGER: Final = r"""
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
READ_STRIP: Final = r"""
const busy = document.querySelector(".htmx-request, .htmx-swapping, .htmx-settling");
return { ...window.__hpStrip, busy: !!busy, there: !!document.getElementById("agent-stats") };
"""
STRIP_STALE: Final = "htmx.trigger(document.getElementById('agent-stats'), 'hp:stale');"


def strip_under_finger(ctx: WalkCtx) -> list[str]:
    """The stats strip's redraw waits while a finger is down, and lands once it lifts."""
    driver, tab = ctx.driver, ctx.tab
    ctx.open()
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
UNHEARD: Final = r"""
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
HEARD: Final = r"""
const busy = document.querySelector(".htmx-request, .htmx-swapping, .htmx-settling");
return { heard: window.__hpHeard, swaps: window.__hpSwaps, scrollY: window.scrollY, busy: !!busy };
"""
UNHEARD_PX: Final = 400


def unheard_scroll(ctx: WalkCtx) -> list[str]:
    """A write's swap that lands before the reader's scroll is heard does not keep them pinned."""
    driver, tab = ctx.driver, ctx.tab
    ctx.open()
    seen = _wait_for(driver, tab.newest)
    if not seen or not seen.get("scrollable"):
        return [f"{tab.name}: never drew a page tall enough to scroll"]
    up = driver.script(UNHEARD, UNHEARD_PX)
    if not _read_until(driver, HEARD, lambda s: _num(s, "heard") >= 1):
        return [f"{tab.name}: the reader's scroll was never heard"]
    ctx.grow(1)
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
UNHEARD_READ: Final = r"""
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
READ_UNHEARD: Final = r"""
const seen = window.__hpUnheard || {};
const busy = document.querySelector(".htmx-request, .htmx-swapping, .htmx-settling");
const gap = document.documentElement.scrollHeight - innerHeight - scrollY;
return { up: seen.up, heard: seen.heard || 0, atEnd: seen.atEnd || 0, swaps: seen.swaps || 0,
  gap: gap, busy: !!busy };
"""


def unheard_read(ctx: WalkCtx) -> list[str]:
    """The view's redraw that lands before the reader's scroll is heard does not pull them back."""
    driver, tab = ctx.driver, ctx.tab
    ctx.open()
    seen = _wait_for(driver, tab.newest)
    if not seen or not seen.get("scrollable"):
        return [f"{tab.name}: never drew a page tall enough to scroll"]
    ctx.grow(1)
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

PAGE_MESSAGES: Final = 20
# The rig's stream: a quick watcher, and a fallback poll short enough to see inside a walk.
LIVE_SETTINGS: Final[MappingProxyType[str, object]] = MappingProxyType(
    {"events_interval": 0.2, "events_fallback": 2}
)
# Half as long again as one fallback poll, timed in the page: a page that polls while its
# stream is up shows here, because the poll's own timer is ordered before the window's end.
QUIET_MS: Final = 3000
# Watcher looks after a change before a second refresh for it would have been published.
LOOKS_AFTER: Final = 2
# Two polls with the stream down, so the page is seen polling on its interval, not once.
MIN_FALLBACK_POLLS: Final = 2
RECONNECT_SECONDS: Final = 25.0

COUNT_CHAT: Final = r"""
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
READ_COUNT: Final = "return window.__hpChat || 0;"
# How many chat fetches the page made over ``ms`` of its own time from now.
COUNT_OVER: Final = r"""
const [ms] = arguments;
const done = arguments[arguments.length - 1];
window.__hpChat = 0;
setTimeout(() => done(window.__hpChat), ms);
"""
# Whether the page's event stream is open now, read off its own request: app.js keeps no flag
# a script can see, so every request to /events made from here on is watched.
WATCH_STREAM: Final = r"""
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
STREAM_OPEN: Final = "return window.__hpStream === true;"
# The first message wholly in view under the top bar, how many there are, and whether the
# reader is at the end as well as at the top.
EARLIER: Final = r"""
const busy = document.querySelector(".htmx-request, .htmx-swapping, .htmx-settling");
if (busy || document.readyState !== "complete") return null;
const under = document.querySelector(".topbar").getBoundingClientRect().bottom;
const all = [...document.querySelectorAll("#agent-view article.turn[data-hp-key]")];
const first = all.find((a) => a.getBoundingClientRect().top >= under);
const [key] = arguments;
const held = key ? all.find((a) => a.getAttribute("data-hp-key") === key) : first;
const units = document.querySelectorAll("#agent-view .turn");
const last = units[units.length - 1];
const dock = (document.getElementById("agent-tray") || document.getElementById("composer"))
  .getBoundingClientRect().top;
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


def _open_live(ctx: WalkCtx) -> list[str]:
    driver, tab, rig = ctx.driver, ctx.tab, ctx.rig
    ctx.open()
    if not _wait_for(driver, tab.newest):
        return [f"{tab.name}: never drew its newest turn"]
    if not _until(lambda: _hub(rig).listeners >= 1):
        return [f"{tab.name}: the page never opened its event stream"]
    settle(driver)
    driver.script(COUNT_CHAT)
    return []


def twenty(ctx: WalkCtx) -> list[str]:
    """A tool-heavy transcript opens on twenty messages, with a way to the page above."""
    driver, tab = ctx.driver, ctx.tab
    ctx.open()
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


def earlier(ctx: WalkCtx) -> list[str]:  # noqa: PLR0915 - a browser walk reads as one story, step by step
    """Load earlier lands a page above the reader, who stays on the message they were reading,
    even when the page was short enough that the reader was also at the end."""
    driver, tab = ctx.driver, ctx.tab
    ctx.open()
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


def refreshed_once(ctx: WalkCtx) -> list[str]:
    """A live page does not poll, and one change to the transcript is exactly one refresh."""
    driver, tab, rig, home = ctx.driver, ctx.tab, ctx.rig, ctx.home
    problems = _open_live(ctx)
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


def falls_back(  # noqa: PLR0915 - a browser walk reads as one story, step by step
    mount: Callable[[ASGIApp], None],
) -> Walk:
    """The stream-loss walk, able to take the stream down by remounting the app."""

    def walk(ctx: WalkCtx) -> list[str]:
        """With the stream gone the page polls; once it is back the polling stops."""
        driver, tab, rig, home = ctx.driver, ctx.tab, ctx.rig, ctx.home
        problems = _open_live(ctx)
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
STEP: Final = 10
REDRAW_SECONDS: Final = 0.3
SCROLL_UP: Final = r"""
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
READ_WALK: Final = r"""
const walk = window.__hpWalk || {};
const gap = document.documentElement.scrollHeight - innerHeight - scrollY;
return { done: !!walk.done, steps: walk.steps || 0, swaps: walk.swaps || [],
  atEnd: walk.atEnd || 0, gap: gap };
"""


def _walked(driver: Marionette) -> dict[str, object]:
    found = driver.script(READ_WALK)
    return found if isinstance(found, dict) else {}


def _redraw_while(ctx: WalkCtx) -> int:
    """Redraw the region on a cadence until the reader's walk is done; the last count drawn."""
    driver = ctx.driver
    n = 1

    def walked() -> bool:
        nonlocal n
        if _walked(driver).get("done"):
            return True
        ctx.grow(n)
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


def scrolling_up(every: float, count: int) -> Walk:
    """The walk for one cadence: ``count`` steps up, one every ``every`` seconds."""

    def walk(ctx: WalkCtx) -> list[str]:
        """Scroll up from the end in small steps while the region is redrawn: never pulled
        back, and the line being read stays where it was across every redraw."""
        driver, tab = ctx.driver, ctx.tab
        ctx.open()
        seen = _wait_for(driver, tab.newest)
        if not seen or not seen.get("scrollable"):
            return [f"{tab.name}: never drew a page taller than the screen"]
        driver.script(SCROLL_UP, STEP, round(every * 1000), count)
        n = _redraw_while(ctx)
        if not _walked(driver).get("done"):
            return [f"{tab.name}: the reader's walk never finished"]
        newest = ctx.grow(n)
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
KEYBOARD_PX: Final = 413
KEYBOARD_STEPS: Final = 40
KEYBOARD: Final = r"""
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
READ_KEYBOARD: Final = "return window.__hpKb || {};"
# A pan of the band alone, its height and the page's scroll unchanged, as a browser whose scrollY
# is the layout viewport's reports one: the keyboard did not move, so no script may move the page.
PAN_PINNED: Final = r"""
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
PAN_TOPS: Final = (400, 300, 200, 100, 0)
# iOS swaps the band's offset between 0 and the keyboard mid-fling (device-report.jsonl), and the
# composer drawn in that frame carries whatever lift the handler left: it must be right as soon as
# the event returns, not a frame later. A repeat of the same offset writes nothing, because --kb
# sits on <html> and every write restyles the whole chat.
SWAP: Final = r"""
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
SWAP_TOPS: Final = (0, 241.7, 413)
# After a downward scroll iOS leaves the band at the layout viewport's bottom (offsetTop 413 in
# device-report.jsonl), so the keyboard's pill sits below the layout viewport, where a fixed box's
# overflow is not drawn: the page under it must carry the composer's colour. A fling moves the page
# for many frames before a script hears of it, so this flings without visualViewport events and
# looks, then lets one event through and looks again: nothing of the composer's colour may be
# missing under the pill or lie over the chat above the composer, and the page may not grow. Past
# the page's end iOS paints its under-page colour, which WebKit takes from <html>'s background with
# <body>'s blended over it, so <body> must not paint an opaque colour of its own there.
PILL_PX: Final = 44
FLING_STEPS: Final = 5
FLING_PX: Final = 80
FLING: Final = r"""
const [pill, steps, px] = arguments;
const done = arguments[arguments.length - 1];
const kb = window.__hpKb;
const root = document.documentElement;
const view = window.visualViewport;
const bar = document.getElementById("composer");
const colour = getComputedStyle(bar).backgroundColor;
const frames = (n, then) => (n ? requestAnimationFrame(() => frames(n - 1, then)) : then());
const painted = () => [...document.querySelectorAll("body *")].filter((el) => {
  if (el.closest("#composer, #agent-tray, .topbar")) return false;
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


def keyboard_up(ctx: WalkCtx) -> list[str]:
    """With the keyboard up, scroll up through the chat while it is redrawn: the composer rides
    the band's bottom edge at every step, the keyboard reads as open throughout, and the reader
    is neither pulled back nor moved."""
    driver, tab = ctx.driver, ctx.tab
    ctx.open()
    seen = _wait_for(driver, tab.newest)
    if not seen or not seen.get("scrollable"):
        return [f"{tab.name}: never drew a page taller than the band"]
    driver.script(KEYBOARD, HEIGHT)
    settle(driver)
    moves = driver.script_async(PAN_PINNED, PAN_TOPS)
    pans = [f"{tab.name}: a pan of the band moved the pinned page {moves}x"] if moves else []
    pans += _swap_problems(tab.name, driver.script_async(SWAP, SWAP_TOPS))
    driver.script(SCROLL_UP, STEP, 50, KEYBOARD_STEPS)
    n = _redraw_while(ctx)
    newest = ctx.grow(n)
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
PLACE: Final = r"""
const under = document.querySelector(".topbar").getBoundingClientRect().bottom;
const turns = [...document.querySelectorAll("#agent-view .turn")];
const on = turns.find((t) => t.getBoundingClientRect().top >= under);
const note = document.getElementById("chat-note");
const busy = document.querySelector(".htmx-request, .htmx-swapping, .htmx-settling");
return { key: on ? on.getAttribute("data-hp-key") : "", top: on ? on.getBoundingClientRect().top : null,
  turns: turns.length, note: note ? note.textContent : "", blank: !!window.__hpBlank, busy: !!busy };
"""
ARM_BLANK: Final = r"""
window.__hpBlank = false;
new MutationObserver(() => {
  if (!document.querySelector("#agent-view .turn")) window.__hpBlank = true;
}).observe(document.body, { childList: true, subtree: true });
"""
HELD_NOTE: Final = "could not be read just now"


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


def locked(ctx: WalkCtx) -> list[str]:
    """A refresh that cannot read the log (its harness holds the store locked) keeps the turns
    drawn and the reader on the one they were reading, says so under them, and heals."""
    driver, tab, home = ctx.driver, ctx.tab, ctx.home
    ctx.open()
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


# The stats sheet in the top bar: open it, and how tall it stands open over its line alone.
OPEN_SHEET: Final = r"""
const sheet = document.querySelector("#agent-stats details");
if (!sheet) return null;
const line = sheet.querySelector("summary").getBoundingClientRect().height;
if (!sheet.open) sheet.querySelector("summary").click();
return { open: sheet.open, grew: sheet.getBoundingClientRect().height - line };
"""
# Mark the strip as drawn before, then have it redraw itself as the stream would.
STATS_STALE: Final = r"""
document.getElementById("agent-stats").__hpOld = true;
htmx.trigger(document.getElementById("agent-stats"), "hp:stale");
"""
STATS_REDRAWN: Final = r"""
const strip = document.getElementById("agent-stats");
const sheet = strip && strip.querySelector("details");
const busy = document.querySelector(".htmx-request, .htmx-swapping, .htmx-settling");
return { fresh: !!strip && !strip.__hpOld, busy: !!busy, open: !!(sheet && sheet.open),
  gap: document.documentElement.scrollHeight - innerHeight - scrollY,
  pinned: window.hp.chatend.pinned() };
"""


def stats_open(ctx: WalkCtx) -> list[str]:
    """A reader at the end with the stats sheet open stays at the end, the sheet still open, when
    the strip redraws: the sheet sits in the sticky bar, so it is part of the page's height."""
    driver, tab = ctx.driver, ctx.tab
    ctx.open()
    if not _wait_for(driver, tab.newest):
        return [f"{tab.name}: never drew its newest line"]
    sheet = driver.script(OPEN_SHEET)
    if not isinstance(sheet, dict) or not sheet.get("open") or _num(sheet, "grew") <= DRIFT:
        return [f"{tab.name}: the stats sheet would not open taller than its line: {sheet}"]
    # The reader brings the end back into view, which pins it.
    driver.script("window.scrollTo(0, document.documentElement.scrollHeight);")
    settle(driver, READER_STILL_MS)
    was = driver.script(STATS_REDRAWN)
    if not isinstance(was, dict) or not was.get("pinned") or _num(was, "gap") > DRIFT:
        return [f"{tab.name}: the reader was not pinned at the end before the redraw: {was}"]
    driver.script(STATS_STALE)
    seen = _read_until(driver, STATS_REDRAWN, lambda s: bool(s.get("fresh")) and not s.get("busy"))
    if not seen:
        return [f"{tab.name}: the stats strip never redrew"]
    settle(driver)
    now = driver.script(STATS_REDRAWN)
    now = now if isinstance(now, dict) else {}
    problems = [] if now.get("open") else [f"{tab.name}: the stats sheet closed as it redrew"]
    if _num(now, "gap") > DRIFT:
        problems.append(
            f"{tab.name}: a stats redraw left the pinned reader {_num(now, 'gap'):.0f}px above "
            f"the end (the open sheet adds {_num(sheet, 'grew'):.0f}px)"
        )
    return problems


def _tabs() -> tuple[tuple[Tab, Walk], ...]:
    blocked = runner(lambda: herd(**BLOCKED), long_pane(LINES) + capture(MENU))
    chat = State("chat", f"{AGENT}?tab=chat", "", runner(snapshot_frame), long_chat(TURNS))
    pane = _pane()
    asking_chat = State("asking chat", chat.path, "", blocked, long_chat(TURNS))
    asking_pane = State("asking pane", pane.path, "", blocked)
    last_reply = f"Reply {TURNS - 1}:"
    [plan] = pick(["agent-answer-plan"])
    [ticks] = pick(["agent-answer-multiselect"])
    [unread] = pick(["agent-unreadable"])
    [strip] = pick(["usage-chat"])
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
        (Tab("pane unheard read", pane, LAST_LINE, _scroll_pane), unheard_read),
        (Tab("strip under a finger", strip, "", _say), strip_under_finger),
        (pane_tab("pane end"), follow),
        (Tab("chat cold", chat, last_reply, _say), cold),
        (Tab("chat stats open", chat, last_reply, _say), stats_open),
        (Tab("pane nudged up", pane, LAST_LINE, _scroll_pane), scrolling_up(0.4, 10)),
        (Tab("pane scrolled up", pane, LAST_LINE, _scroll_pane), scrolling_up(0.05, 40)),
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
        (Tab("pane door from chat", replace(ticks, path=chat.path), "", _print), pane_door),
        (Tab("pane door from pane", ticks, "", _print), pane_door),
        (Tab("unread question on chat", unread, "", _print, 320, 568), unread_question),
        (
            Tab("unread question on pane", replace(unread, path=pane.path), "", _print, 320, 568),
            unread_question,
        ),
        (Tab("tall plan", plan, "", _print, width=320, height=568), tall_question),
        (plan_coming_tab("tall plan arrives"), question_arrives),
        (plan_printing_tab("tall plan printed"), question_printed),
        (plan_printing_tab("tall plan printed wide", WIDTH, HEIGHT), question_printed),
        (plan_printing_tab("tall plan printed after"), question_printed_after),
        (
            Tab("tall multiselect", ticks, "", _print, width=430, height=932, scale=2.0),
            tall_question,
        ),
    )


def _walks(
    mount: Callable[[ASGIApp], None],
) -> list[tuple[Tab, Walk]]:
    chat = State("stream loss", f"{AGENT}?tab=chat", "", runner(snapshot_frame), long_chat(TURNS))
    lost = State(chat.name, chat.path, "", chat.replies, chat.transcript, settings=LIVE_SETTINGS)
    slow = State("slow link", f"{AGENT}?tab=chat", "", runner(snapshot_frame), long_chat(TURNS))
    [plan] = pick(["agent-answer-plan"])
    return [
        *_tabs(),
        (Tab("stream loss", lost, f"Reply {TURNS - 1}:", _say), falls_back(mount)),
        (Tab("chat slow link", slow, f"Reply {TURNS - 1}:", _say), slow_link(mount)),
        (Tab("tall plan columns late", plan, "", _print, 320, 568), columns_late(mount)),
    ]


def names() -> list[str]:
    """Every walk's name, in the order ``run`` walks them."""
    return [tab.name for tab, _walk in _walks(lambda _app: None)]


def run(bench: Bench, only: frozenset[str] | None = None) -> list[str]:
    """Every walk, or those ``only`` names, on a fresh app in its own phone window; empty when
    each tab keeps its end."""
    driver = bench.driver
    problems: list[str] = []
    for tab, walk in _walks(bench.mount):
        if only is not None and tab.name not in only:
            continue
        home = bench.scratch / f"end-{tab.name.replace(' ', '-')}"
        home.mkdir(parents=True)
        rig = build(tab.state, home)
        bench.mount(rig.http.app)
        first = driver.handles()[0]
        phone_window(driver, tab.width, tab.height)
        try:
            problems += walk(WalkCtx(driver, bench.base, tab, rig, home))
        finally:
            driver.send("WebDriver:CloseWindow", {})
            driver.send("WebDriver:SwitchToWindow", {"handle": first})
        print(f"  walked {tab.name}", flush=True)
    return problems
