"""Drive headless Firefox over the real app at phone widths and hold its geometry to account."""

import argparse
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from queue import Empty, Queue

import uvicorn
from pydantic import JsonValue, TypeAdapter
from starlette.types import ASGIApp, Receive, Scope, Send

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import backnav  # noqa: E402
import chatend  # noqa: E402
import feedhold  # noqa: E402
import sendhold  # noqa: E402
import voiceover  # noqa: E402
from firefox import BrowserError, Marionette, browser, free_port, phone_window  # noqa: E402

from tests.catalogue import STATES, State, build, pick  # noqa: E402
from tests.waiting import eventually  # noqa: E402

# The --only name for the back-navigation walks.
HISTORY = "history"
# The --only name for the Chat tab's scroll walk.
CHAT_END = "chat-end"
# The --only name for the feed's hold-under-the-thumb walk.
FEED_HOLD = "feed-hold"
# The --only name for the walk that taps an answer while the runner is slow.
SEND_HOLD = "send-hold"
# The --only name for the walks that listen as VoiceOver would.
VOICEOVER = "voiceover"
# 390 is the phone this app is designed on; 430 is the largest (a Pro Max size, run as a
# Home Screen app); 320 is the narrowest one still sold (iPhone SE 1st gen).
WIDTHS = (390, 430, 320)
HEIGHT = 844
# iPhone Text Size, as a multiple of the default root: 130% is a common setting, 200% is the
# largest the plain slider reaches. The two widths that bracket the phones are run again at each.
TEXT_SIZES = (1.3, 2.0)
TEXT_WIDTHS = (430, 320)
# Apple's HIG minimum and WCAG 2.5.5: 44 CSS px in both directions.
TAP_FLOOR = 44
# iOS Safari zooms the page into a focused field whose text is under 16px, and does not zoom back.
FIELD_FONT_FLOOR = 16
FIELDS = "input:not([type=hidden]), textarea, select"
# The page gutter. iOS starts its back swipe at the left edge; how wide that strip is can only
# be measured on the device, so a sideways scroller must at least start inside the gutter.
EDGE_INSET = 16
READY_SECONDS = 15.0

SCROLL_ALLOWED: dict[str, str] = {
    ".prose pre": (
        "a code block in agent prose: indentation and columns are the meaning, so the block "
        "scrolls inside its own box instead of wrapping a line of code into a different shape"
    ),
    ".is-columns .pane": (
        "the terminal mirror with Keep the columns on: the toggle asks for the terminal's own "
        "line breaks, so the pane scrolls inside its box, which starts inside the gutter"
    ),
    ".is-columns .src": (
        "a file with Keep the columns on: the toggle asks for each line at its own "
        "indentation, unwrapped, so the file scrolls inside its box, which starts inside the "
        "gutter, with its line numbers held at the left edge"
    ),
    ".prose .table-scroll": (
        "a table in agent prose wider than the turn: its cells wrap only between words, so a "
        "table of many columns scrolls inside its own box, which starts inside the turn"
    ),
    ".prose .math-block": (
        "a display formula wider than the turn: a formula cannot wrap, so it scrolls inside "
        "its own box like a code block"
    ),
    ".tool__result": (
        "a tool's output, which is terminal text with the same column meaning as code; it "
        "scrolls in its own box and the page around it does not move"
    ),
    ".chips": (
        "the feed's workspace filter: one row of pills that pans sideways so a long list of "
        "workspaces costs one line, not several; it starts inside the 16px back-swipe gutter "
        "and a fade at its right edge says more pills are there"
    ),
}

TAP_ALLOWED: dict[str, str] = {
    ".crumbs a": (
        "a folder in the path above a file: a short name like src is narrower than 44px, so "
        "its 44px hit area is drawn by its own ::after, centred on the word; padding the box "
        "to 44px instead spaced the slashes unevenly"
    ),
    "label input[type=checkbox]": (
        "a checkbox inside its own label: tapping anywhere on the label toggles it, and the "
        "label row is held to 44px, so the drawn box can stay the size iOS draws one"
    ),
    ".prose a": (
        "a link inside a sentence the agent wrote; padding it to 44px would tear the line it "
        "sits in apart, and WCAG 2.5.5 exempts targets inline in a block of text"
    ),
}

TAPPABLE = "a[href], button, summary, input, select, textarea, [role=button], [role=link]"

# An agent's bar: three 44px segments, on one row with the back link, the strip under them.
BAR_TABS = ("Chat", "Pane", "Files")
# "One quiet line" at the default Text Size: about 28px, with a little room for the font.
STRIP_MAX = 36
# The back link keeps its word only on a bar wide enough for it: 390px and up at default text.
WORD_WIDTHS = (390, 430)

# True once htmx has wired every hx element it can see and nothing is in flight or settling.
# A fragment swapped in by hx-trigger="load" is attached a settle delay before htmx processes
# it, and a tap in that gap posts the bare form natively and lands on the no-JS fallback.
HTMX_SETTLED = r"""() => {
  if (document.readyState !== "complete") return false;
  const busy = ".htmx-request, .htmx-swapping, .htmx-settling, .htmx-added";
  if (document.querySelector(busy)) return false;
  if (typeof window.htmx !== "object") return true;
  const wired = "[hx-get], [hx-post], [hx-put], [hx-patch], [hx-delete], [hx-trigger]";
  for (const el of document.querySelectorAll(wired)) {
    const data = el["htmx-internal-data"];
    if (!data || !data.initHash) return false;
  }
  return true;
}"""

PROBE_LOADED = r"""
const ready = arguments[0];
const base = arguments[1];
const statics = performance.getEntriesByType("resource")
  .map((e) => new URL(e.name).pathname)
  .filter((p) => p.startsWith("/static/"));
const build = document.querySelector('meta[name="hp-build"]');
return {
  url: location.href,
  errorPage: !location.href.startsWith(base) || document.documentURI.startsWith("about:"),
  build: build ? build.content : "",
  ground: getComputedStyle(document.documentElement).getPropertyValue("--ground").trim(),
  statics: statics,
  htmx: typeof window.htmx,
  hidden: document.hidden,
  ready: document.querySelector(ready) !== null,
  settled: (HTMX_SETTLED)(),
};
""".replace("HTMX_SETTLED", HTMX_SETTLED)

# Text Size moves the root font, and every rem token with it; the browser's default stands in
# for the system body size here, because only iOS answers -apple-system-body. Then, frame by
# frame, wait for the room the page keeps for the docked composer to be its height again.
# Every htmx request is refused meanwhile, so no poll's redraw can re-measure the dock for it:
# the dock must follow the text itself. How many frames that took is not judged, only whether
# it happened before the ceiling, so a loaded machine is slower but never red.
GROW_TEXT = r"""
const [scale, ceiling] = arguments;
const done = arguments[arguments.length - 1];
const refuse = (event) => event.preventDefault();
document.addEventListener("htmx:beforeRequest", refuse, true);
const root = document.documentElement;
root.style.fontSize = parseFloat(getComputedStyle(root).fontSize) * scale + "px";
const kept = () => {
  const bar = document.getElementById("composer");
  return !bar || root.style.getPropertyValue("--dock-h").trim() === bar.offsetHeight + "px";
};
const started = performance.now();
const frame = () => {
  const ok = kept();
  if (ok || performance.now() - started > ceiling) {
    document.removeEventListener("htmx:beforeRequest", refuse, true);
    done(ok);
  } else {
    requestAnimationFrame(frame);
  }
};
requestAnimationFrame(frame);
"""
# Only how long to wait before calling the dock stuck; a dock that follows does so in frames.
DOCK_CEILING_MS = 5000

# One synchronous script, so no htmx poll can swap the DOM halfway through a measurement.
PROBE_GEOMETRY = r"""
const [scrollAllowed, tapAllowed, tappable, floor, edge, fields, fontFloor] = arguments;
for (const d of document.querySelectorAll("details")) d.open = true;
const describe = (el) => {
  const cls = (el.getAttribute("class") || "").trim().split(/\s+/).filter(Boolean);
  return el.tagName.toLowerCase() + cls.map((c) => "." + c).join("");
};
const root = document.documentElement;
const vw = root.clientWidth;
const page = { vw: vw, content: Math.max(root.scrollWidth, document.body.scrollWidth) };
const contained = (el) => {
  for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
    if (getComputedStyle(p).overflowX === "visible") continue;
    const r = p.getBoundingClientRect();
    if (r.left >= -0.5 && r.right <= vw + 0.5) return true;
  }
  return false;
};
const scrollers = [], spills = [], taps = [], edges = [], spilled = new Set();
for (const el of document.body.querySelectorAll("*")) {
  const style = getComputedStyle(el);
  const sideways = style.overflowX === "auto" || style.overflowX === "scroll";
  // A box that can pan sideways under the left edge takes the swipe that means back.
  if (sideways && el.getBoundingClientRect().width > 0 && el.getBoundingClientRect().left < edge) {
    edges.push({ what: describe(el), left: el.getBoundingClientRect().left });
  }
  if ((style.overflowX === "auto" || style.overflowX === "scroll")
      && el.scrollWidth > el.clientWidth + 1) {
    scrollers.push({ what: describe(el), content: el.scrollWidth, box: el.clientWidth,
      allowed: scrollAllowed.find((s) => el.matches(s)) || null });
  }
  const r = el.getBoundingClientRect();
  if (r.width > 0 && (r.right > vw + 0.5 || r.left < -0.5) && !contained(el)) {
    spilled.add(el);
    // Name only the outermost offender; its children spill because it does.
    if (!spilled.has(el.parentElement)) {
      spills.push({ what: describe(el), left: r.left, right: r.right });
    }
  }
}
// A bar fixed to the bottom covers that band of the page at every scroll, even at its end.
const vh = root.clientHeight;
const docked = [];
let covered = 0;
for (const el of document.body.querySelectorAll("*")) {
  if (getComputedStyle(el).position !== "fixed") continue;
  const r = el.getBoundingClientRect();
  if (r.height > 0 && r.bottom >= vh - 0.5) {
    docked.push(el);
    covered = Math.max(covered, vh - r.top);
  }
}
const end = Math.max(root.scrollHeight, document.body.scrollHeight) - covered;
const buried = [];
let measured = 0;
for (const el of document.querySelectorAll(tappable)) {
  const r = el.getBoundingClientRect();
  const style = getComputedStyle(el);
  if (r.width === 0 || r.height === 0 || style.visibility === "hidden") continue;
  if (covered > 0 && !docked.some((d) => d.contains(el)) && r.bottom + window.scrollY > end + 0.5) {
    buried.push({ what: describe(el), bottom: r.bottom + window.scrollY, end: end });
  }
  measured += 1;
  if (r.width >= floor - 0.5 && r.height >= floor - 0.5) continue;
  taps.push({ what: describe(el), width: r.width, height: r.height,
    allowed: tapAllowed.find((s) => el.matches(s)) || null });
}
const zooms = [];
for (const el of document.querySelectorAll(fields)) {
  const size = parseFloat(getComputedStyle(el).fontSize);
  if (size < fontFloor - 0.01) zooms.push({ what: describe(el), size: size });
}
// A table cell narrower than its widest word has broken that word across lines.
const cells = [];
for (const cell of document.querySelectorAll(".prose th, .prose td")) {
  const style = getComputedStyle(cell);
  const inner = cell.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
  const walker = document.createTreeWalker(cell, NodeFilter.SHOW_TEXT);
  let widest = 0, word = "";
  for (let t = walker.nextNode(); t; t = walker.nextNode()) {
    for (const m of t.data.matchAll(/\S+/g)) {
      const range = document.createRange();
      range.setStart(t, m.index);
      range.setEnd(t, m.index + m[0].length);
      let width = 0;
      for (const r of range.getClientRects()) width += r.width;
      if (width > widest) { widest = width; word = m[0]; }
    }
  }
  if (widest > inner + 0.5) cells.push({ what: describe(cell), word: word, widest: widest, inner: inner });
}
// A card's kind and pane id never ellipsize; only the workspace gives way. The one excuse is a
// chip wider than its card's whole line, which only the largest text on the narrowest phone makes.
const cut = [];
for (const chip of document.querySelectorAll(".card__harness, .card__pane")) {
  const line = chip.parentElement.clientWidth;
  if (chip.scrollWidth > chip.clientWidth + 0.5 && chip.scrollWidth <= line + 0.5) {
    cut.push({ what: describe(chip), text: chip.textContent, full: chip.scrollWidth, shown: chip.clientWidth });
  }
}
// An agent's bar and the stats strip under it, when this page has them.
const box = (el) => el.getBoundingClientRect();
let bar = null;
const nav = document.querySelector("header.topbar nav.tabs--3[aria-label=View]");
if (nav) {
  const back = document.querySelector("header.topbar .topbar__back");
  const word = back ? back.querySelector(".topbar__word") : null;
  const line = document.querySelector("header.topbar #agent-stats .stats__line");
  const meter = line ? line.querySelector(".meter") : null;
  bar = {
    tabs: [...nav.querySelectorAll("a.tab")].map((t) => ({
      label: t.textContent.trim(), width: box(t).width, height: box(t).height })),
    oneRow: !!back && box(back).top < box(nav).bottom && box(nav).top < box(back).bottom,
    name: back ? back.getAttribute("aria-label") : null,
    word: word ? getComputedStyle(word).display !== "none" : null,
    // What is drawn: the toggle's box, less the reach its negative margin takes back.
    strip: line ? { height: box(line).height + parseFloat(getComputedStyle(line).marginBottom),
      top: box(line).top, under: box(nav).bottom } : null,
    meter: meter ? { width: box(meter).width, wants: parseFloat(getComputedStyle(meter).width),
      right: box(meter).right, edge: box(line).right } : null,
    cut: line ? [...line.querySelectorAll(".stats__fact")]
      .filter((f) => f.scrollWidth > f.clientWidth + 0.5 || box(f).right > vw + 0.5)
      .map((f) => f.textContent) : [],
  };
}
return { page: page, scrollers: scrollers, spills: spills, taps: taps, edges: edges,
  buried: buried, measured: measured, zooms: zooms, cells: cells, cut: cut, bar: bar };
"""


class Switch:
    """An ASGI app that forwards to whichever state's app is current."""

    def __init__(self) -> None:
        self.app: ASGIApp | None = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Forward one request."""
        if self.app is None:
            raise RuntimeError("no state is being served")
        await self.app(scope, receive, send)

    def mount(self, app: ASGIApp) -> None:
        """Serve ``app`` from the next request on."""
        self.app = app


@contextmanager
def serve(switch: Switch) -> Iterator[str]:
    """Serve ``switch`` on an ephemeral loopback port, never one a real bridge uses."""
    port = free_port()
    # Open event streams never end on their own; cut them rather than wait on them at the end.
    config = uvicorn.Config(
        switch,
        host="127.0.0.1",
        port=port,
        log_level="error",
        lifespan="off",
        timeout_graceful_shutdown=1,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("the geometry server never started")
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def install(switch: Switch, state: State, scratch: Path) -> None:
    """Build a fresh app for ``state`` over its own fake runner and make it current."""
    home = scratch / state.name
    home.mkdir(parents=True)
    switch.app = build(state, home).http.app


def _wait(
    driver: Marionette, selector: str, base: str, *, settle: bool = True
) -> dict[str, object]:
    deadline = time.monotonic() + READY_SECONDS
    seen: dict[str, object] = {}
    while time.monotonic() < deadline:
        found = driver.script(PROBE_LOADED, selector, base)
        seen = found if isinstance(found, dict) else {}
        # A state held mid-tap never settles: its proof alone is enough.
        if not settle:
            seen["settled"] = True
        if (seen.get("ready") and seen.get("settled")) or seen.get("errorPage"):
            break
        time.sleep(0.05)
    return seen


def _loaded(driver: Marionette, state: State, base: str) -> list[str]:
    """Wait for the state's proof element, tapping first if it needs one, then judge the page."""
    if state.act:
        seen = _wait(driver, state.armed, base)
        if not (seen.get("ready") and seen.get("settled")):
            return _load_problems(seen, state.armed, scripted=True)
        driver.script(state.act)
    seen = _wait(driver, state.ready, base, settle=not state.in_flight)
    return _load_problems(seen, state.ready, scripted=state.scripted)


def _load_problems(seen: dict[str, object], ready: str, *, scripted: bool) -> list[str]:
    problems: list[str] = []
    if seen.get("errorPage"):
        problems.append(f"this is not the app (Firefox error page?): {seen.get('url')}")
    if not seen.get("build"):
        problems.append("no hp-build meta: the base template did not render")
    if not seen.get("ground"):
        problems.append("--ground is unset: app.css did not apply (CSP, MIME or 404)")
    statics = seen.get("statics")
    fetched = {str(s) for s in statics} if isinstance(statics, list) else set()
    wanted = ("/static/app.css", "/static/app.js", "/static/htmx.min.js")
    problems += [
        f"{name} was never fetched: blocked before it loaded"
        for name in wanted[: 3 if scripted else 1]
        if name not in fetched
    ]
    if scripted and seen.get("htmx") != "object":
        problems.append("window.htmx is missing: htmx.min.js did not run")
    if seen.get("hidden"):
        problems.append("document.hidden: every poll would stop on HTTP 286")
    if not seen.get("ready"):
        problems.append(f"{ready} never appeared within {READY_SECONDS:.0f}s")
    elif not seen.get("settled"):
        problems.append(f"htmx never settled within {READY_SECONDS:.0f}s")
    return problems


@dataclass
class Tally:
    """Every problem found, and how often each argued exception was needed."""

    problems: list[str] = field(default_factory=list[str])
    used: dict[str, int] = field(default_factory=dict[str, int])
    taps: int = 0

    def allow(self, selector: str) -> None:
        """Count one use of an exception, so an exception nothing needs shows up as stale."""
        self.used[selector] = self.used.get(selector, 0) + 1


def _rows(found: dict[str, object], key: str) -> list[dict[str, object]]:
    value = found.get(key)
    return [r for r in value if isinstance(r, dict)] if isinstance(value, list) else []


def _judge_cards(found: dict[str, object], where: str, tally: Tally) -> None:
    for row in _rows(found, "cut"):
        tally.problems.append(
            f"{where}: {row['what']} {row['text']!r} is cut to {row['shown']:.1f}px of "
            f"{row['full']:.1f}px; only a card's workspace may ellipsize"
        )


def judge(found: dict[str, object], where: str, tally: Tally) -> None:
    """Turn one geometry probe into problems and exception counts."""
    page = found.get("page")
    if isinstance(page, dict) and page["content"] > page["vw"]:
        tally.problems.append(
            f"{where}: the PAGE scrolls sideways: {page['content']}px in a {page['vw']}px viewport"
        )
    for row in _rows(found, "scrollers"):
        if isinstance(allowed := row["allowed"], str):
            tally.allow(allowed)
        else:
            tally.problems.append(
                f"{where}: unlisted scroller {row['what']}: {row['content']}px in {row['box']}px"
            )
    for row in _rows(found, "edges"):
        tally.problems.append(
            f"{where}: sideways scroller {row['what']} starts {row['left']:.1f}px from the "
            f"left edge, inside the {EDGE_INSET}px the back swipe needs"
        )
    for row in _rows(found, "spills"):
        tally.problems.append(
            f"{where}: {row['what']} spills out of the viewport "
            f"({row['left']:.1f}..{row['right']:.1f}px)"
        )
    for row in _rows(found, "buried"):
        tally.problems.append(
            f"{where}: tap target {row['what']} ends at {row['bottom']:.1f}px, under the bar "
            f"fixed to the bottom even when the page is scrolled to its end ({row['end']:.1f}px)"
        )
    for row in _rows(found, "taps"):
        if isinstance(allowed := row["allowed"], str):
            tally.allow(allowed)
        else:
            tally.problems.append(
                f"{where}: tap target {row['what']} is "
                f"{row['width']:.1f}x{row['height']:.1f}px, under {TAP_FLOOR}px"
            )
    for row in _rows(found, "zooms"):
        tally.problems.append(
            f"{where}: field {row['what']} is set in {row['size']}px type; under "
            f"{FIELD_FONT_FLOOR}px iOS zooms the page in when it takes focus"
        )
    for row in _rows(found, "cells"):
        tally.problems.append(
            f"{where}: table cell {row['what']} is {row['inner']:.1f}px wide, narrower than its "
            f"widest word {row['word']!r} ({row['widest']:.1f}px), so the word breaks"
        )
    _judge_cards(found, where, tally)
    measured = found.get("measured")
    tally.taps += measured if isinstance(measured, int) else 0


def _judge_tabs(bar: dict[str, object], where: str, tally: Tally) -> None:
    tabs = [t for t in bar["tabs"] if isinstance(t, dict)] if isinstance(bar["tabs"], list) else []
    labels = tuple(str(t["label"]) for t in tabs)
    if labels != BAR_TABS:
        tally.problems.append(f"{where}: the agent's bar holds {labels}, not {BAR_TABS}")
    for tab in tabs:
        if (
            float(str(tab["width"])) < TAP_FLOOR - 0.5
            or float(str(tab["height"])) < TAP_FLOOR - 0.5
        ):
            tally.problems.append(
                f"{where}: the {tab['label']} segment is {tab['width']:.1f}x{tab['height']:.1f}px"
            )
    if not bar["oneRow"]:
        tally.problems.append(f"{where}: the three tabs wrapped off the back link's row")
    if bar["name"] != "Back to herd":
        tally.problems.append(f"{where}: the back link is named {bar['name']!r}")


def _judge_word(bar: dict[str, object], where: str, size: "Size", tally: Tally) -> None:
    wants = size.scale == 1.0 and size.width in WORD_WIDTHS
    if bar["word"] is not wants:
        said = "keeps" if bar["word"] else "drops"
        tally.problems.append(
            f"{where}: the back link {said} its word; it should {'keep' if wants else 'drop'} it"
        )


def _judge_strip(bar: dict[str, object], where: str, size: "Size", tally: Tally) -> None:
    strip = bar["strip"]
    if not isinstance(strip, dict):
        tally.problems.append(f"{where}: no stats strip under the agent's bar")
        return
    if float(str(strip["top"])) < float(str(strip["under"])) - 0.5:
        tally.problems.append(f"{where}: the stats strip is not under the tabs")
    if size.scale == 1.0 and float(str(strip["height"])) > STRIP_MAX:
        tally.problems.append(
            f"{where}: the stats strip is {strip['height']:.1f}px tall, over {STRIP_MAX}px"
        )
    meter = bar["meter"]
    if isinstance(meter, dict) and (
        float(str(meter["width"])) < float(str(meter["wants"])) - 0.5
        or float(str(meter["right"])) > float(str(meter["edge"])) + 0.5
    ):
        tally.problems.append(f"{where}: the context meter is squeezed or cut: {meter}")
    cut = bar["cut"]
    if isinstance(cut, list) and cut:
        tally.problems.append(f"{where}: the strip cuts {cut}; it must wrap instead")


def judge_bar(found: dict[str, object], where: str, size: "Size", tally: Tally) -> None:
    """An agent's bar: three 44px tabs on one row, the right back link, the strip under them."""
    bar = found.get("bar")
    if not isinstance(bar, dict):
        return
    _judge_tabs(bar, where, tally)
    _judge_word(bar, where, size, tally)
    _judge_strip(bar, where, size, tally)


def _grow_text(driver: Marionette, scale: float, *, settle: bool = True) -> bool:
    """Set the page's text ``scale`` times its default; whether the dock followed it by itself."""
    kept = driver.script_async(GROW_TEXT, scale, DOCK_CEILING_MS) is True
    if settle:
        eventually(
            lambda: bool(driver.script(f"return ({HTMX_SETTLED})();")), seconds=READY_SECONDS
        )
    return kept


@dataclass(frozen=True)
class Size:
    """A phone width and an iPhone Text Size, as a multiple of the default."""

    width: int
    scale: float = 1.0

    def label(self, name: str) -> str:
        """Where a finding was made, for the report."""
        text = f" at {self.scale:.0%} text" if self.scale != 1.0 else ""
        return f"{self.width}px {name}{text}"


def run_state(driver: Marionette, state: State, base: str, size: Size, tally: Tally) -> None:
    """Load one state at one size; it must prove it loaded before anything is measured."""
    where = size.label(state.name)
    scale = size.scale
    try:
        driver.send("WebDriver:Navigate", {"url": base + state.path})
    except BrowserError as exc:
        # Marionette refuses to land on Firefox's own error page; that is a load failure too.
        tally.problems.append(f"{where}: NOT LOADED: {str(exc)[:160]}")
        return
    problems = _loaded(driver, state, base)
    if problems:
        tally.problems += [f"{where}: NOT LOADED: {p}" for p in problems]
        return
    if scale != 1.0 and not _grow_text(driver, scale, settle=not state.in_flight):
        tally.problems.append(
            f"{where}: the docked composer was not re-measured after the text grew, so the "
            "room the page keeps for it is the old size"
        )
    found = driver.script(
        PROBE_GEOMETRY,
        list(SCROLL_ALLOWED),
        list(TAP_ALLOWED),
        TAPPABLE,
        TAP_FLOOR,
        EDGE_INSET,
        FIELDS,
        FIELD_FONT_FLOOR,
    )
    judge(found if isinstance(found, dict) else {}, where, tally)
    judge_bar(found if isinstance(found, dict) else {}, where, size, tally)
    # One window serves every state, so a remembered choice must not leak into the next.
    driver.script("try { localStorage.clear(); sessionStorage.clear(); } catch (e) {}")
    print(f"  measured {where}", flush=True)


def stale(tally: Tally) -> list[str]:
    """An argued exception that nothing needed is a reason nobody re-checks; it must go."""
    listed = [*SCROLL_ALLOWED, *TAP_ALLOWED]
    return [
        f"exception {s} was not needed by any state: delete it"
        for s in listed
        if s not in tally.used
    ]


def report(tally: Tally, *, whole: bool, runs: int) -> int:
    """Print the exceptions with their reasons and every problem; non-zero on any problem."""
    for kind, table in (("scroller", SCROLL_ALLOWED), ("tap", TAP_ALLOWED)):
        for selector, why in table.items():
            print(f"  allowed {kind} {selector} ({tally.used.get(selector, 0)}x): {why}")
    problems = tally.problems + (stale(tally) if whole else [])
    for problem in problems:
        print(f"  FAIL {problem}")
    verdict = "red" if problems else "green"
    print(
        f"geometry: {runs} runs, {tally.taps} tap targets measured, {len(problems)} problems: {verdict}"
    )
    return 1 if problems else 0


# A task is one size over a slice of the states, or a walk; workers take them from one queue.
Task = dict[str, object]

# One task or one answer on the worker pipe, a JSON object per line.
_LINE = TypeAdapter(dict[str, JsonValue])
WALKS = {
    HISTORY: backnav,
    CHAT_END: chatend,
    FEED_HOLD: feedhold,
    SEND_HOLD: sendhold,
    VOICEOVER: voiceover,
}
# States per task: small enough that the browsers finish together, large enough to amortise
# opening a window.
CHUNK = 14
# Each worker drives its own Firefox; past four they mostly wait on each other for the CPU.
JOBS = max(1, min(4, (os.cpu_count() or 2) // 2))


def _label(task: Task) -> str:
    if task["kind"] == "states":
        return Size(int(str(task["width"])), float(str(task["scale"]))).label("states")
    return f"walk {task['module']} {task.get('names') or ''}".rstrip()


def _strings(value: object) -> list[str]:
    return [str(v) for v in value] if isinstance(value, list) else []


def _do(task: Task, driver: Marionette, base: str, switch: Switch, scratch: Path) -> Tally:
    """Run one task in this worker's browser."""
    tally = Tally()
    if task["kind"] == "states":
        size = Size(int(str(task["width"])), float(str(task["scale"])))
        phone_window(driver, size.width, HEIGHT)
        for state in pick(_strings(task["names"])):
            install(switch, state, scratch)
            run_state(driver, state, base, size, tally)
        driver.send("WebDriver:CloseWindow", {})
        driver.send("WebDriver:SwitchToWindow", {"handle": driver.handles()[0]})
        return tally
    module = str(task["module"])
    if module == CHAT_END and task.get("names"):
        only = frozenset(_strings(task["names"]))
        tally.problems += chatend.run(driver, base, switch.mount, scratch, only)
    else:
        tally.problems += WALKS[module].run(driver, base, switch.mount, scratch)
    return tally


def worker() -> int:
    """Take tasks as JSON lines on stdin and answer each with its tally on stdout."""
    answers = sys.stdout
    # Progress lines go where the parent's own output goes; stdout carries only the answers.
    sys.stdout = sys.stderr
    switch = Switch()
    with (
        tempfile.TemporaryDirectory(prefix="hp-geometry-") as scratch,
        serve(switch) as base,
        browser() as driver,
    ):
        for n, line in enumerate(sys.stdin):
            task: Task = dict(_LINE.validate_json(line))
            try:
                tally = _do(task, driver, base, switch, Path(scratch) / str(n))
            except BrowserError as exc:
                tally = Tally(problems=[f"{_label(task)}: the browser failed: {exc}"])
            answer = {"problems": tally.problems, "used": tally.used, "taps": tally.taps}
            answers.write(json.dumps(answer) + "\n")
            answers.flush()
    return 0


def tasks(only: list[str]) -> tuple[list[Task], int, bool]:
    """Every task ``only`` asks for, the longest first; with the state runs, and whether whole."""
    names = [n for n in only if n not in WALKS]
    chosen = [s.name for s in (pick(names) if names or not only else [])]
    sizes = [Size(w) for w in WIDTHS] + [Size(w, s) for w in TEXT_WIDTHS for s in TEXT_SIZES]
    walks: list[Task] = []
    for module in WALKS:
        if only and module not in only:
            continue
        if module == CHAT_END:
            walks += [{"kind": "walk", "module": module, "names": [n]} for n in chatend.names()]
        else:
            walks.append({"kind": "walk", "module": module})
    states: list[Task] = [
        {"kind": "states", "width": size.width, "scale": size.scale, "names": chosen[i : i + CHUNK]}
        for size in reversed(sizes)
        for i in range(0, len(chosen), CHUNK)
    ]
    # The stream-loss walk is the longest single task, so it starts first.
    ordered = sorted(walks, key=lambda t: t.get("names") != ["stream loss"]) + states
    whole = len(chosen) == len(STATES) and not only
    return ordered, len(chosen) * len(sizes), whole


def _merge(tally: Tally, answer: dict[str, object]) -> None:
    tally.problems += _strings(answer.get("problems"))
    used = answer.get("used")
    for selector, count in used.items() if isinstance(used, dict) else ():
        tally.used[str(selector)] = tally.used.get(str(selector), 0) + int(str(count))
    taps = answer.get("taps")
    tally.taps += taps if isinstance(taps, int) else 0


def drive(work: list[Task], jobs: int) -> Tally:
    """Hand ``work`` to ``jobs`` workers, each with its own server and browser, and add up."""
    queue: Queue[Task] = Queue()
    for task in work:
        queue.put(task)
    tally = Tally()
    lock = threading.Lock()

    def feed(process: subprocess.Popen[str]) -> None:
        stdin, stdout = process.stdin, process.stdout
        if stdin is None or stdout is None:
            raise RuntimeError("a geometry worker has no pipes")
        while True:
            try:
                task = queue.get_nowait()
            except Empty:
                break
            stdin.write(json.dumps(task) + "\n")
            stdin.flush()
            line = stdout.readline()
            with lock:
                if not line:
                    tally.problems.append(f"{_label(task)}: the worker died before it answered")
                    return
                _merge(tally, dict(_LINE.validate_json(line)))
        stdin.close()
        if process.wait() != 0:
            with lock:
                tally.problems.append(f"a geometry worker exited {process.returncode}")

    command = [sys.executable, str(Path(__file__).resolve()), "--worker"]
    processes = [
        subprocess.Popen(
            command, cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
        )
        for _ in range(max(1, min(jobs, len(work))))
    ]
    threads = [threading.Thread(target=feed, args=(p,)) for p in processes]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    for process in processes:
        if process.poll() is None:
            process.kill()
            process.wait()
    return tally


def main(argv: list[str] | None = None) -> int:
    """Run every state at every size, and every walk, across several browsers."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--only", action="append", default=[], help="run just these states")
    parser.add_argument("--jobs", type=int, default=JOBS, help="browsers to run at once")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    # The unreachable states log a warning per request by design; the report is the output.
    logging.getLogger("herdr_pwa").setLevel(logging.ERROR)
    if args.worker:
        return worker()
    work, runs, whole = tasks(args.only)
    return report(drive(work, args.jobs), whole=whole, runs=runs)


if __name__ == "__main__":
    raise SystemExit(main())
