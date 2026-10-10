"""Drive headless Firefox over the real app at phone widths and hold its geometry to account."""

import argparse
import logging
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import uvicorn
from starlette.types import ASGIApp, Receive, Scope, Send

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import backnav  # noqa: E402
import chatend  # noqa: E402
from firefox import BrowserError, Marionette, browser, free_port, phone_window  # noqa: E402

from tests.catalogue import STATES, State, build, pick  # noqa: E402

# The --only name for the back-navigation walks.
HISTORY = "history"
# The --only name for the Chat tab's scroll walk.
CHAT_END = "chat-end"
# 390 is the phone this app is designed on; 430 is the largest (a Pro Max size, run as a
# Home Screen app); 320 is the narrowest one still sold (iPhone SE 1st gen).
WIDTHS = (390, 430, 320)
HEIGHT = 844
# Apple's HIG minimum and WCAG 2.5.5: 44 CSS px in both directions.
TAP_FLOOR = 44
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
    ".turn__result pre": (
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

# One synchronous script, so no htmx poll can swap the DOM halfway through a measurement.
PROBE_GEOMETRY = r"""
const [scrollAllowed, tapAllowed, tappable, floor, edge] = arguments;
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
return { page: page, scrollers: scrollers, spills: spills, taps: taps, edges: edges,
  buried: buried, measured: measured };
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
    config = uvicorn.Config(switch, host="127.0.0.1", port=port, log_level="error", lifespan="off")
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


def _wait(driver: Marionette, selector: str, base: str) -> dict[str, object]:
    deadline = time.monotonic() + READY_SECONDS
    seen: dict[str, object] = {}
    while time.monotonic() < deadline:
        found = driver.script(PROBE_LOADED, selector, base)
        seen = found if isinstance(found, dict) else {}
        if (seen.get("ready") and seen.get("settled")) or seen.get("errorPage"):
            break
        time.sleep(0.2)
    return seen


def _loaded(driver: Marionette, state: State, base: str) -> list[str]:
    """Wait for the state's proof element, tapping first if it needs one, then judge the page."""
    if state.act:
        seen = _wait(driver, state.armed, base)
        if not (seen.get("ready") and seen.get("settled")):
            return _load_problems(seen, state.armed, scripted=True)
        driver.script(state.act)
    seen = _wait(driver, state.ready, base)
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
    measured = found.get("measured")
    tally.taps += measured if isinstance(measured, int) else 0


def run_state(driver: Marionette, state: State, base: str, width: int, tally: Tally) -> None:
    """Load one state at one width; it must prove it loaded before anything is measured."""
    where = f"{width}px {state.name}"
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
    found = driver.script(
        PROBE_GEOMETRY, list(SCROLL_ALLOWED), list(TAP_ALLOWED), TAPPABLE, TAP_FLOOR, EDGE_INSET
    )
    judge(found if isinstance(found, dict) else {}, where, tally)
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


def main(argv: list[str] | None = None) -> int:
    """Run every state at every width in one browser."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--only", action="append", default=[], help="run just these states")
    args = parser.parse_args(argv)
    # The unreachable states log a warning per request by design; the report is the output.
    logging.getLogger("herdr_pwa").setLevel(logging.ERROR)
    names = [n for n in args.only if n not in (HISTORY, CHAT_END)]
    chosen = pick(names) if names or not args.only else []
    widths = WIDTHS if chosen else ()
    tally = Tally()
    switch = Switch()
    with tempfile.TemporaryDirectory(prefix="hp-geometry-") as scratch, serve(switch) as base:
        try:
            with browser() as driver:
                for width in widths:
                    phone_window(driver, width, HEIGHT)
                    for state in chosen:
                        install(switch, state, Path(scratch) / f"{width}")
                        run_state(driver, state, base, width, tally)
                    driver.send("WebDriver:CloseWindow", {})
                    driver.send("WebDriver:SwitchToWindow", {"handle": driver.handles()[0]})
                # History belongs to a top-level window, and this one is left on the app.
                if not args.only or HISTORY in args.only:
                    tally.problems += backnav.run(driver, base, switch.mount, Path(scratch))
                if not args.only or CHAT_END in args.only:
                    tally.problems += chatend.run(driver, base, switch.mount, Path(scratch))
        except BrowserError as exc:
            print(f"geometry: the browser failed: {exc}", file=sys.stderr)
            return 1
    whole = len(chosen) == len(STATES) and not args.only
    return report(tally, whole=whole, runs=len(chosen) * len(widths))


if __name__ == "__main__":
    raise SystemExit(main())
