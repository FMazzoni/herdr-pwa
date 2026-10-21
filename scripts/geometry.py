"""Drive headless Firefox over the real app at phone widths and hold its geometry to account."""

import argparse
import gzip
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from queue import Empty, Queue

import uvicorn
from pydantic import JsonValue, TypeAdapter
from starlette.types import ASGIApp, Message, Receive, Scope, Send

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import affordance  # noqa: E402
import backnav  # noqa: E402
import chatend  # noqa: E402
import feedhold  # noqa: E402
import sendhold  # noqa: E402
import voiceover  # noqa: E402
from firefox import (  # noqa: E402
    Bench,
    BrowserError,
    Marionette,
    browser,
    free_port,
    held,
    inherited,
    phone_window,
)

from herdr_pwa.web.assets import SCRIPTS, STATIC_PREFIX  # noqa: E402
from tests.catalogue import STATES, State, build, pick  # noqa: E402
from tests.waiting import eventually  # noqa: E402
from tests.webfakes import Hold  # noqa: E402

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
# 390 is the phone this app is designed on; 430 is the largest (a Pro Max size, run as a Home
# Screen app); 320 is the narrowest one still sold (iPhone SE 1st gen).
WIDTHS = (390, 430, 320)
HEIGHT = 844
# iPhone Text Size, as a multiple of the default root: 130% is a common setting, 200% is the
# largest the plain slider reaches. The two widths that bracket the phones are run again at each.
TEXT_SIZES = (1.3, 2.0)
TEXT_WIDTHS = (430, 320)
# Apple's HIG minimum and WCAG 2.5.5: 44 CSS px in both directions.
TAP_FLOOR = 44
# Anything that stops a turn stays this far from Send, so a thumb aimed at Send stops nothing.
STOP_REACH = 120
# iOS Safari zooms the page into a focused field whose text is under 16px, and does not zoom back.
FIELD_FONT_FLOOR = 16
FIELDS = "input:not([type=hidden]), textarea, select"
# The page gutter. iOS starts its back swipe at the left edge; how wide that strip is can only
# be measured on the device, so a sideways scroller must at least start inside the gutter.
EDGE_INSET = 16
READY_SECONDS = 15.0
# How long a uvicorn thread on loopback may take to bind.
SERVE_SECONDS = 10.0

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

# After a native submit the next document has no root until its first byte parses: not loaded.
PROBE_LOADED = r"""
const ready = arguments[0];
const base = arguments[1];
const root = document.documentElement;
const statics = performance.getEntriesByType("resource")
  .map((e) => new URL(e.name).pathname)
  .filter((p) => p.startsWith("/static/"));
const build = document.querySelector('meta[name="hp-build"]');
return {
  url: location.href,
  errorPage: !location.href.startsWith(base) || document.documentURI.startsWith("about:"),
  build: build ? build.content : "",
  ground: root ? getComputedStyle(root).getPropertyValue("--ground").trim() : "",
  statics: statics,
  htmx: typeof window.htmx,
  hidden: document.hidden,
  ready: root !== null && document.querySelector(ready) !== null,
  settled: root !== null && (HTMX_SETTLED)(),
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
  const tray = document.getElementById("agent-tray");
  const trayKept = !tray || root.style.getPropertyValue("--tray-h").trim() === tray.offsetHeight + "px";
  return !bar || (trayKept && root.style.getPropertyValue("--dock-h").trim() === bar.offsetHeight + "px");
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

# A tap's outcome lasts only until its region's own poll (twice the answer cadence, 10s), and a
# loaded page can take longer than that to settle: once the tap is answered and its proof drawn,
# every further htmx request is refused, so the screen judged is the outcome, not what replaced it.
HOLD_PROOF = r"""
const ready = arguments[0];
let answered = false;
document.addEventListener("htmx:afterRequest", (event) => {
  const config = event.detail.requestConfig;
  if (config && config.verb !== "get") answered = true;
}, true);
document.addEventListener("htmx:beforeRequest", (event) => {
  if (answered && document.querySelector(ready)) event.preventDefault();
}, true);
"""

# A tap can grow the dock too (a focused composer is taller), and the app re-measures it on the
# resize observer's next frame: wait that out, frame by frame, before the room for it is judged.
DOCK_KEPT = r"""
const [ceiling] = arguments;
const done = arguments[arguments.length - 1];
const root = document.documentElement;
const kept = () => {
  const bar = document.getElementById("composer");
  const tray = document.getElementById("agent-tray");
  const trayKept = !tray || root.style.getPropertyValue("--tray-h").trim() === tray.offsetHeight + "px";
  return !bar || (trayKept && root.style.getPropertyValue("--dock-h").trim() === bar.offsetHeight + "px");
};
const started = performance.now();
const frame = () => {
  const ok = kept();
  if (ok || performance.now() - started > ceiling) done(ok);
  else requestAnimationFrame(frame);
};
requestAnimationFrame(frame);
"""

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
// A bar fixed to the bottom covers that band of the page at every scroll, even at its end, and so
// does a bar fixed on top of it (the status row on the composer).
const vh = root.clientHeight;
const docked = [];
let covered = 0;
const fixed = [...document.body.querySelectorAll("*")]
  .filter((el) => getComputedStyle(el).position === "fixed")
  .map((el) => [el, el.getBoundingClientRect()])
  .sort((a, b) => b[1].bottom - a[1].bottom);
for (const [el, r] of fixed) {
  if (r.height > 0 && r.bottom >= vh - covered - 0.5) {
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
    // What is drawn: the toggle's box, less the reach its negative margin takes back. Open,
    // the line is the sheet's first row shown whole, so the one-line cap is for closed only.
    strip: line ? { height: box(line).height + parseFloat(getComputedStyle(line).marginBottom),
      top: box(line).top, under: box(nav).bottom, open: !!line.closest("details[open]"),
      hidden: getComputedStyle(line.closest(".stats")).display === "none",
      typing: !!document.querySelector("#composer:focus-within") } : null,
    meter: meter ? { width: box(meter).width, wants: parseFloat(getComputedStyle(meter).width),
      right: box(meter).right, edge: box(line).right } : null,
    // Cut: clipped with no ellipsis to say so, past the screen, or half under the one line.
    cut: line ? [...line.querySelectorAll(".stats__fact")]
      .filter((f) => (f.scrollWidth > f.clientWidth + 0.5
        && getComputedStyle(f).textOverflow !== "ellipsis") || box(f).right > vw + 0.5
        || (box(f).top < box(f.parentElement).bottom - 0.5
          && box(f).bottom > box(f.parentElement).bottom + 0.5))
      .map((f) => f.textContent) : [],
  };
}
// G2: a control that stops a turn now keeps out of Send's column and REACH px from it, and a
// confirm's safe answer covers the control whose first tap armed it.
const drawn = (el) => box(el).width > 0 && getComputedStyle(el).visibility !== "hidden";
const send = document.querySelector("#composer button[type=submit]");
const near = [];
if (send && drawn(send)) {
  const s = box(send);
  // In-flow controls as they stand with the page at its end, where the dock meets them.
  const atEnd = Math.max(0, root.scrollHeight - vh) - window.scrollY;
  const pinned = (el) => fixed.some(([f]) => f.contains(el));
  for (const el of document.querySelectorAll(".button--stop, .keys__stop--armed .button--danger")) {
    if (!drawn(el)) continue;
    const b = box(el);
    const lift = pinned(el) ? 0 : atEnd;
    const r = { left: b.left, right: b.right, top: b.top - lift, bottom: b.bottom - lift };
    const gap = Math.hypot(Math.max(0, s.left - r.right, r.left - s.right),
      Math.max(0, s.top - r.bottom, r.top - s.bottom));
    const column = r.left < s.right - 0.5 && r.right > s.left + 0.5;
    if (column || gap < REACH) {
      near.push({ what: describe(el), gap: gap, column: column,
        rect: [r.left, r.top, r.right, r.bottom].map(Math.round),
        send: [s.left, s.top, s.right, s.bottom].map(Math.round) });
    }
  }
}
// G4: a status-row line never opens on a separator. One left of its counts box is clipped away.
const leading = [];
const shown = (el) => box(el).width > 0 && getComputedStyle(el).visibility !== "hidden";
const sameLine = (a, b) => Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 0.5 * a.height;
for (const counts of document.querySelectorAll(".tray__counts")) {
  const edge = box(counts).left;
  const row = counts.closest(".tray__row");
  const words = row ? [...row.querySelectorAll(".tray__words")].filter(shown) : [];
  const before = words.map(box);
  for (const count of counts.querySelectorAll(".tray__count")) {
    const sep = count.querySelector(".tray__sep");
    const text = count.querySelector(".count") || count;
    if (sep && shown(sep) && box(sep).right > edge + 0.5) {
      const s = box(sep);
      // The words sit centred on the row, so beside a wrapped run any overlap puts them on its line.
      const beside = (b) => b.right <= s.left + 0.5 && (words.length && b === before[0]
        ? Math.min(s.bottom, b.bottom) > Math.max(s.top, b.top) : sameLine(s, b));
      if (!before.some(beside)) {
        leading.push({ what: describe(count), text: count.textContent.trim() });
      }
    }
    if (shown(text)) before.push(box(text));
  }
}
// G4: herdr's state word in the row is never dropped, at any width or text size.
for (const word of document.querySelectorAll(".tray__row .tray__words--state")) {
  if (!shown(word) || word.scrollWidth > word.clientWidth + 0.5) {
    leading.push({ what: describe(word), text: word.textContent.trim(), state: true });
  }
}
const uncovered = [];
for (const slot of document.querySelectorAll(".tray__slot")) {
  const arm = slot.querySelector(".tray__arm");
  const keep = slot.querySelector("[data-hp-keep]");
  if (!arm || !keep) continue;
  const a = box(arm), k = box(keep);
  if (k.left > a.left + 0.5 || k.right < a.right - 0.5 || k.top > a.top + 0.5
      || k.bottom < a.bottom - 0.5) {
    uncovered.push({ what: describe(arm), arm: [a.left, a.top, a.right, a.bottom].map(Math.round),
      keep: [k.left, k.top, k.right, k.bottom].map(Math.round) });
  }
}
return { page: page, scrollers: scrollers, spills: spills, taps: taps, edges: edges,
  buried: buried, measured: measured, zooms: zooms, cells: cells, cut: cut, bar: bar,
  near: near, uncovered: uncovered, leading: leading };
""".replace("REACH", str(STOP_REACH))


# The rig's own paths, answered by the switch and never seen by the app.
RIG_PREFIX = "/__rig/"
COLLECTOR_PATH = RIG_PREFIX + "page-errors.js"
REPORT_PATH = RIG_PREFIX + "page-error"
# The first thing in every whole page's head, so it runs before any script the app sends.
COLLECTOR_TAG = f'<script src="{COLLECTOR_PATH}"></script>'.encode()
# A browser reports an uncaught throw and the page carries on, so nothing else would notice it.
# Posted synchronously: once the handler returns the rig has it, so a throw just before a
# read-back or a navigation is never lost in flight, and the read-back needs no wait.
COLLECTOR = r"""(function () {
  "use strict";
  function report(kind, message, source) {
    try {
      var post = new XMLHttpRequest();
      post.open("POST", "REPORT_PATH", false);
      post.setRequestHeader("Content-Type", "application/json");
      post.send(JSON.stringify({ kind: kind, message: String(message), source: String(source),
        page: location.href }));
    } catch (e) {}
  }
  window.addEventListener("error", function (event) {
    report("error", event.message, (event.filename || "?") + ":" + event.lineno + ":" + event.colno);
  });
  window.addEventListener("unhandledrejection", function (event) {
    var reason = event.reason;
    var said = reason instanceof Error ? reason.name + ": " + reason.message : String(reason);
    var stack = reason && reason.stack ? String(reason.stack).split("\n")[0] : "?";
    report("unhandledrejection", said, stack);
  });
})();
""".replace("REPORT_PATH", REPORT_PATH)
_REPORT = TypeAdapter(dict[str, str])


def _header(message: Message, name: bytes) -> str:
    headers: list[tuple[bytes, bytes]] = message.get("headers", [])
    return next((v.decode("latin-1") for k, v in headers if k.lower() == name), "")


def _with_collector(start: Message, body: bytes) -> tuple[Message, bytes]:
    """A whole page with the collector first in its head; anything else exactly as sent."""
    gzipped = _header(start, b"content-encoding") == "gzip"
    plain = gzip.decompress(body) if gzipped else body
    head = plain.find(b"<head>")
    if not plain[:15].lower().startswith(b"<!doctype html") or head < 0:
        return start, body
    at = head + len(b"<head>")
    page = plain[:at] + COLLECTOR_TAG + plain[at:]
    kept: list[tuple[bytes, bytes]] = [
        (k, v)
        for k, v in start.get("headers", [])
        if k.lower() not in (b"content-length", b"content-encoding")
    ]
    headers = [*kept, (b"content-length", str(len(page)).encode())]
    return {**start, "headers": headers}, page


class Switch:
    """An ASGI app that forwards to whichever state's app is current."""

    def __init__(self) -> None:
        self.app: ASGIApp | None = None
        # Writes the current state's runner is sitting on, let go when the next state mounts.
        self.held: list[Hold] = []
        # Writes still being answered; a read's stream never ends, so only writes are counted.
        self.writing = 0
        # Uncaught page errors the collector reported, until the next read-back.
        self.errors: list[dict[str, str]] = []
        self._errors_lock = threading.Lock()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Forward one request."""
        if self.app is None:
            raise RuntimeError("no state is being served")
        if scope["type"] == "http" and str(scope["path"]).startswith(RIG_PREFIX):
            await self._rig(str(scope["path"]), receive, send)
            return
        write = scope["type"] == "http" and scope["method"] != "GET"
        self.writing += write
        try:
            await self.app(scope, receive, _collecting(send) if scope["type"] == "http" else send)
        finally:
            self.writing -= write

    async def _rig(self, path: str, receive: Receive, send: Send) -> None:
        if path == COLLECTOR_PATH:
            body = COLLECTOR.encode()
            kind = b"text/javascript; charset=utf-8"
            headers = [(b"content-type", kind), (b"content-length", str(len(body)).encode())]
            await send({"type": "http.response.start", "status": 200, "headers": headers})
            await send({"type": "http.response.body", "body": body})
            return
        raw = b""
        more = True
        while more:
            message = await receive()
            raw += bytes(message.get("body", b""))
            more = bool(message.get("more_body", False))
        found = path == REPORT_PATH
        if found:
            with self._errors_lock:
                self.errors.append(_REPORT.validate_json(raw))
        await send({"type": "http.response.start", "status": 204 if found else 404, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    def drain(self) -> list[str]:
        """Every page error reported since the last read-back; one line per distinct throw."""
        with self._errors_lock:
            errors, self.errors = self.errors, []
        pages: dict[tuple[str, str, str], list[str]] = {}
        for e in errors:
            thrown = (e.get("kind", "error"), e.get("message", "?"), e.get("source", "?"))
            pages.setdefault(thrown, []).append(e.get("page", "?"))
        return [
            f"uncaught {kind}: {message} at {source}, on {seen[0]}"
            + (f" and {len(seen) - 1} more page loads" if len(seen) > 1 else "")
            for (kind, message, source), seen in pages.items()
        ]

    def mount(self, app: ASGIApp) -> None:
        """Serve ``app`` from the next request on."""
        self.app = app

    def let_go(self) -> list[str]:
        """Release every held write and wait for every write to be answered; what never was."""
        held, self.held = self.held, []
        problems: list[str] = []
        # The tap is drawn as out before it reaches the runner: let go early, it would land late.
        # Its proof can be drawn before the write leaves, so one that never came is said here.
        if not eventually(lambda: all(h.taken.is_set() for h in held), READY_SECONDS):
            problems.append(
                f"a held write never reached the runner within {READY_SECONDS:.0f}s, so the "
                "state's tap may never have been sent"
            )
        for hold in held:
            hold.release.set()
        # The in-flight claim is one per process, so a write still out refuses the next tap.
        if not eventually(lambda: self.writing == 0, READY_SECONDS):
            problems.append(
                "a write was still out when the next state mounted, so its tap may be refused"
            )
        return problems


def _collecting(send: Send) -> Send:
    """``send``, holding back an HTML response until it is whole so a page can get the collector."""
    start: Message | None = None
    body = b""

    async def sending(message: Message) -> None:
        nonlocal start, body
        if message["type"] == "http.response.start" and _header(
            message, b"content-type"
        ).startswith("text/html"):
            start = message
            return
        if start is None or message["type"] != "http.response.body":
            await send(message)
            return
        body += bytes(message.get("body", b""))
        if message.get("more_body", False):
            return
        head, page = _with_collector(start, body)
        await send(head)
        await send({"type": "http.response.body", "body": page})

    return sending


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

    # A thread that has died will never start the server, so stop looking.
    _ = eventually(lambda: server.started or not thread.is_alive(), SERVE_SECONDS)
    if not server.started:
        raise RuntimeError("the geometry server never started")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def install(switch: Switch, state: State, scratch: Path) -> list[str]:
    """Build a fresh app for ``state`` over its own fake runner and make it current.

    The last state's held writes are let go and answered first; what went wrong doing so.
    """
    home = scratch / state.name
    home.mkdir(parents=True)
    problems = switch.let_go()
    rig = build(state, home)
    switch.held = [hook for hook in rig.wire.after.values() if isinstance(hook, Hold)]
    switch.app = rig.http.app
    return problems


def _wait(
    driver: Marionette, selector: str, base: str, *, settle: bool = True
) -> dict[str, object]:
    seen: dict[str, object] = {}

    def loaded() -> bool:
        nonlocal seen
        found = driver.script(PROBE_LOADED, selector, base)
        seen = found if isinstance(found, dict) else {}
        # A state held mid-tap never settles: its proof alone is enough.
        if not settle:
            seen["settled"] = True
        return bool((seen.get("ready") and seen.get("settled")) or seen.get("errorPage"))

    _ = eventually(loaded, READY_SECONDS)
    return seen


def _loaded(driver: Marionette, state: State, base: str) -> list[str]:
    """Wait for the state's proof element, tapping first if it needs one, then judge the page."""
    if state.act:
        seen = _wait(driver, state.armed, base)
        if not (seen.get("ready") and seen.get("settled")):
            return _load_problems(seen, state.armed, scripted=True)
        # A proof already on the page passes whether or not the tap drew anything.
        if driver.script("return document.querySelector(arguments[0]) !== null;", state.ready):
            return [f"{state.ready} is on the page before the tap, so it cannot prove the tap"]
        # One script, so the gap between the armed look and the tap is what it always was.
        driver.script(("" if state.in_flight else HOLD_PROOF) + state.act, state.ready)
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
    # Every script the page runs, not only the first: one blocked section is a page half wired.
    scripts = ("htmx.min.js", *SCRIPTS) if scripted else ()
    wanted = [f"{STATIC_PREFIX}{name}" for name in ("app.css", *scripts)]
    problems += [
        f"{name} was never fetched: blocked before it loaded"
        for name in wanted
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


@dataclass(frozen=True)
class Size:
    """A phone width and an iPhone Text Size, as a multiple of the default."""

    width: int
    scale: float = 1.0

    def label(self, name: str) -> str:
        """Where a finding was made, for the report."""
        text = f" at {self.scale:.0%} text" if self.scale != 1.0 else ""
        return f"{self.width}px {name}{text}"


@dataclass
class Tally:
    """Every problem found, and how often each argued exception was needed."""

    problems: list[str] = field(default_factory=list[str])
    # Tasks run a second time because their worker died; said in the report, never hidden.
    retries: list[str] = field(default_factory=list[str])
    used: dict[str, int] = field(default_factory=dict[str, int])
    taps: int = 0

    def allow(self, selector: str) -> None:
        """Count one use of an exception, so an exception nothing needs shows up as stale."""
        self.used[selector] = self.used.get(selector, 0) + 1


@dataclass(frozen=True)
class Spot:
    """One state measured at one size: its name in the report, the size, and the tally it adds to."""

    where: str
    size: Size
    tally: Tally

    def fail(self, problem: str) -> None:
        """Record a problem found here."""
        self.tally.problems.append(f"{self.where}: {problem}")


def _rows(found: dict[str, object], key: str) -> list[dict[str, object]]:
    value = found.get(key)
    return [r for r in value if isinstance(r, dict)] if isinstance(value, list) else []


def _judge_cards(found: dict[str, object], at: Spot) -> None:
    for row in _rows(found, "cut"):
        at.fail(
            f"{row['what']} {row['text']!r} is cut to {row['shown']:.1f}px of "
            f"{row['full']:.1f}px; only a card's workspace may ellipsize"
        )


def _judge_leading(found: dict[str, object], at: Spot) -> None:
    for row in _rows(found, "leading"):
        if row.get("state"):
            at.fail(f"the status row drops or cuts its state word {row['text']!r}")
        else:
            at.fail(
                f"{row['what']} {row['text']!r} opens a line of the status row with its separator"
            )


def _judge_stops(found: dict[str, object], at: Spot) -> None:
    for row in _rows(found, "near"):
        where = "in Send's column" if row["column"] else f"{row['gap']:.1f}px from Send"
        at.fail(
            f"{row['what']} {row['rect']} stops a turn now and is {where} {row['send']}; "
            f"keep it out of Send's column and {STOP_REACH}px from it"
        )
    for row in _rows(found, "uncovered"):
        at.fail(
            f"Keep going {row['keep']} does not cover {row['what']} {row['arm']}, the control "
            "its first tap armed, so a double tap can land on something else"
        )


def judge(found: dict[str, object], at: Spot) -> None:
    """Turn one geometry probe into problems and exception counts."""
    page = found.get("page")
    if isinstance(page, dict) and page["content"] > page["vw"]:
        at.fail(f"the PAGE scrolls sideways: {page['content']}px in a {page['vw']}px viewport")
    for row in _rows(found, "scrollers"):
        if isinstance(allowed := row["allowed"], str):
            at.tally.allow(allowed)
        else:
            at.fail(f"unlisted scroller {row['what']}: {row['content']}px in {row['box']}px")
    for row in _rows(found, "edges"):
        at.fail(
            f"sideways scroller {row['what']} starts {row['left']:.1f}px from the "
            f"left edge, inside the {EDGE_INSET}px the back swipe needs"
        )
    for row in _rows(found, "spills"):
        at.fail(
            f"{row['what']} spills out of the viewport ({row['left']:.1f}..{row['right']:.1f}px)"
        )
    for row in _rows(found, "buried"):
        at.fail(
            f"tap target {row['what']} ends at {row['bottom']:.1f}px, under the bar "
            f"fixed to the bottom even when the page is scrolled to its end ({row['end']:.1f}px)"
        )
    for row in _rows(found, "taps"):
        if isinstance(allowed := row["allowed"], str):
            at.tally.allow(allowed)
        else:
            at.fail(
                f"tap target {row['what']} is "
                f"{row['width']:.1f}x{row['height']:.1f}px, under {TAP_FLOOR}px"
            )
    for row in _rows(found, "zooms"):
        at.fail(
            f"field {row['what']} is set in {row['size']}px type; under "
            f"{FIELD_FONT_FLOOR}px iOS zooms the page in when it takes focus"
        )
    for row in _rows(found, "cells"):
        at.fail(
            f"table cell {row['what']} is {row['inner']:.1f}px wide, narrower than its "
            f"widest word {row['word']!r} ({row['widest']:.1f}px), so the word breaks"
        )
    _judge_cards(found, at)
    _judge_stops(found, at)
    _judge_leading(found, at)
    measured = found.get("measured")
    at.tally.taps += measured if isinstance(measured, int) else 0


def _judge_tabs(bar: dict[str, object], at: Spot) -> None:
    tabs = [t for t in bar["tabs"] if isinstance(t, dict)] if isinstance(bar["tabs"], list) else []
    labels = tuple(str(t["label"]) for t in tabs)
    if labels != BAR_TABS:
        at.fail(f"the agent's bar holds {labels}, not {BAR_TABS}")
    for tab in tabs:
        if (
            float(str(tab["width"])) < TAP_FLOOR - 0.5
            or float(str(tab["height"])) < TAP_FLOOR - 0.5
        ):
            at.fail(f"the {tab['label']} segment is {tab['width']:.1f}x{tab['height']:.1f}px")
    if not bar["oneRow"]:
        at.fail("the three tabs wrapped off the back link's row")
    if bar["name"] != "Back to herd":
        at.fail(f"the back link is named {bar['name']!r}")


def _judge_word(bar: dict[str, object], at: Spot) -> None:
    wants = at.size.scale == 1.0 and at.size.width in WORD_WIDTHS
    if bar["word"] is not wants:
        said = "keeps" if bar["word"] else "drops"
        at.fail(f"the back link {said} its word; it should {'keep' if wants else 'drop'} it")


def _judge_strip(bar: dict[str, object], at: Spot) -> None:
    strip = bar["strip"]
    if not isinstance(strip, dict):
        at.fail("no stats strip under the agent's bar")
        return
    # Typing, the strip steps aside for the keyboard; at any other time it is there.
    if strip.get("hidden"):
        if not strip.get("typing"):
            at.fail("the stats strip is hidden while nothing is being typed")
        return
    if strip.get("typing"):
        at.fail("the stats strip stays while the composer has focus; it steps aside for typing")
    if float(str(strip["top"])) < float(str(strip["under"])) - 0.5:
        at.fail("the stats strip is not under the tabs")
    if at.size.scale == 1.0 and not strip.get("open") and float(str(strip["height"])) > STRIP_MAX:
        at.fail(f"the stats strip is {strip['height']:.1f}px tall, over {STRIP_MAX}px")
    meter = bar["meter"]
    if isinstance(meter, dict) and (
        float(str(meter["width"])) < float(str(meter["wants"])) - 0.5
        or float(str(meter["right"])) > float(str(meter["edge"])) + 0.5
    ):
        at.fail(f"the context meter is squeezed or cut: {meter}")
    cut = bar["cut"]
    if isinstance(cut, list) and cut:
        at.fail(f"the strip cuts {cut}; a fact leaves the line whole or ends in an ellipsis")


def judge_bar(found: dict[str, object], at: Spot) -> None:
    """An agent's bar: three 44px tabs on one row, the right back link, the strip under them."""
    bar = found.get("bar")
    if not isinstance(bar, dict):
        return
    _judge_tabs(bar, at)
    _judge_word(bar, at)
    _judge_strip(bar, at)


def _grow_text(driver: Marionette, scale: float, *, settle: bool = True) -> bool:
    """Set the page's text ``scale`` times its default; whether the dock followed it by itself."""
    kept = driver.script_async(GROW_TEXT, scale, DOCK_CEILING_MS) is True
    if settle:
        eventually(lambda: bool(driver.script(f"return ({HTMX_SETTLED})();")), READY_SECONDS)
    return kept


def run_state(bench: Bench, state: State, size: Size, tally: Tally) -> None:
    """Load one state at one size; it must prove it loaded before anything is measured."""
    driver, base = bench.driver, bench.base
    at = Spot(size.label(state.name), size, tally)
    scale = size.scale
    try:
        driver.send("WebDriver:Navigate", {"url": base + state.path})
    except BrowserError as exc:
        # Marionette refuses to land on Firefox's own error page; that is a load failure too.
        at.fail(f"NOT LOADED: {str(exc)[:160]}")
        return
    problems = _loaded(driver, state, base)
    if problems:
        for problem in problems:
            at.fail(f"NOT LOADED: {problem}")
        return
    if scale != 1.0 and not _grow_text(driver, scale, settle=not state.in_flight):
        at.fail(
            "the docked composer was not re-measured after the text grew, so the "
            "room the page keeps for it is the old size"
        )
    elif scale == 1.0 and driver.script_async(DOCK_KEPT, DOCK_CEILING_MS) is not True:
        at.fail(
            "the docked composer was not re-measured after it changed size, so the "
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
    judge(found if isinstance(found, dict) else {}, at)
    judge_bar(found if isinstance(found, dict) else {}, at)
    # Last, since it swipes sheets shut and toggles folds: nothing is measured after it.
    for problem in affordance.check(driver):
        at.fail(problem)
    # One window serves every state, so a remembered choice must not leak into the next.
    driver.script("try { localStorage.clear(); sessionStorage.clear(); } catch (e) {}")
    print(f"  measured {at.where}", flush=True)


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
    for task in tally.retries:
        print(f"  RETRIED {task}: its worker died, so it ran once more in a fresh one")
    for problem in problems:
        print(f"  FAIL {problem}")
    verdict = "red" if problems else "green"
    retried = f", {len(tally.retries)} retried" if tally.retries else ""
    print(
        f"geometry: {runs} runs, {tally.taps} tap targets measured{retried}, "
        f"{len(problems)} problems: {verdict}"
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


def _do(task: Task, bench: Bench, switch: Switch) -> Tally:
    """Run one task on this worker's bench; one state's browser failure is that state's alone."""
    driver = bench.driver
    tally = Tally()
    if task["kind"] == "states":
        size = Size(int(str(task["width"])), float(str(task["scale"])))
        phone_window(driver, size.width, HEIGHT)
        for state in pick(_strings(task["names"])):
            try:
                mounted = install(switch, state, bench.scratch)
                tally.problems += [f"{size.label(state.name)}: {p}" for p in mounted]
                run_state(bench, state, size, tally)
            except BrowserError as exc:
                tally.problems.append(f"{size.label(state.name)}: the browser failed: {exc}")
            tally.problems += [f"{size.label(state.name)}: {e}" for e in switch.drain()]
        driver.send("WebDriver:CloseWindow", {})
        driver.send("WebDriver:SwitchToWindow", {"handle": driver.handles()[0]})
        return tally
    module = str(task["module"])
    if module == CHAT_END and task.get("names"):
        only = frozenset(_strings(task["names"]))
        tally.problems += chatend.run(bench, only)
    else:
        tally.problems += WALKS[module].run(bench)
    tally.problems += [f"{_label(task)}: {e}" for e in switch.drain()]
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
                bench = Bench(driver, base, switch.mount, Path(scratch) / str(n))
                tally = _do(task, bench, switch)
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


def _ask(process: subprocess.Popen[str], task: Task) -> str:
    """Hand ``task`` to a worker and read its answer; empty when the worker died first."""
    stdin, stdout = process.stdin, process.stdout
    if stdin is None or stdout is None:
        raise RuntimeError("a geometry worker has no pipes")
    try:
        stdin.write(json.dumps(task) + "\n")
        stdin.flush()
    except OSError:
        return ""
    return stdout.readline()


def _bury(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.kill()
    process.wait()


def drive(work: list[Task], jobs: int) -> Tally:  # noqa: PLR0915 - the nested workers share its queue and tally, and count here
    """Hand ``work`` to ``jobs`` workers, each with its own server and browser, and add up.

    A task whose worker dies goes back on the queue and runs once more, and the report says so;
    a task whose worker dies twice fails. Fresh workers are started for what a death left over.
    """
    queue: Queue[Task] = Queue()
    for task in work:
        queue.put(task)
    tally = Tally()
    lock = threading.Lock()

    def died(task: Task) -> None:
        label = _label(task)
        with lock:
            if task.get("retried"):
                tally.problems.append(f"{label}: the worker died before it answered, twice")
                return
            tally.retries.append(label)
        queue.put({**task, "retried": True})

    def feed(process: subprocess.Popen[str]) -> None:
        while True:
            try:
                task = queue.get_nowait()
            except Empty:
                break
            line = _ask(process, task)
            if not line:
                _bury(process)
                died(task)
                return
            with lock:
                _merge(tally, dict(_LINE.validate_json(line)))
        if process.stdin is not None:
            process.stdin.close()
        if process.wait() != 0:
            with lock:
                tally.problems.append(f"a geometry worker exited {process.returncode}")

    command = [sys.executable, str(Path(__file__).resolve()), "--worker"]
    while not queue.empty():
        # Each worker inherits the run's lock descriptor, so it runs under the lock, not beside it.
        processes = [
            subprocess.Popen(
                command,
                cwd=ROOT,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
                pass_fds=inherited(),
            )
            for _ in range(max(1, min(jobs, queue.qsize())))
        ]
        threads = [threading.Thread(target=feed, args=(p,)) for p in processes]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        for process in processes:
            _bury(process)
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
    # Every worker runs under this one hold, so a direct run queues with the gate and shots.
    with held():
        return report(drive(work, args.jobs), whole=whole, runs=runs)


if __name__ == "__main__":
    raise SystemExit(main())
