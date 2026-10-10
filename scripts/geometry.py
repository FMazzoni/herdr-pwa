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

from firefox import BrowserError, Marionette, browser, free_port, phone_window  # noqa: E402

from tests.catalogue import STATES, State, build, pick  # noqa: E402

# 390 is the phone this app is designed on; 320 is the narrowest one still sold (iPhone SE 1st gen).
WIDTHS = (390, 320)
HEIGHT = 844
# Apple's HIG minimum and WCAG 2.5.5: 44 CSS px in both directions.
TAP_FLOOR = 44
READY_SECONDS = 15.0

SCROLL_ALLOWED: dict[str, str] = {
    ".prose pre": (
        "a code block in agent prose: indentation and columns are the meaning, so the block "
        "scrolls inside its own box instead of wrapping a line of code into a different shape"
    ),
    ".turn__result pre": (
        "a tool's output, which is terminal text with the same column meaning as code; it "
        "scrolls in its own box and the page around it does not move"
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
};
"""

# One synchronous script, so no htmx poll can swap the DOM halfway through a measurement.
PROBE_GEOMETRY = r"""
const [scrollAllowed, tapAllowed, tappable, floor] = arguments;
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
const scrollers = [], spills = [], taps = [], spilled = new Set();
for (const el of document.body.querySelectorAll("*")) {
  const style = getComputedStyle(el);
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
let measured = 0;
for (const el of document.querySelectorAll(tappable)) {
  const r = el.getBoundingClientRect();
  const style = getComputedStyle(el);
  if (r.width === 0 || r.height === 0 || style.visibility === "hidden") continue;
  measured += 1;
  if (r.width >= floor - 0.5 && r.height >= floor - 0.5) continue;
  taps.push({ what: describe(el), width: r.width, height: r.height,
    allowed: tapAllowed.find((s) => el.matches(s)) || null });
}
return { page: page, scrollers: scrollers, spills: spills, taps: taps, measured: measured };
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
        if seen.get("ready") or seen.get("errorPage"):
            break
        time.sleep(0.2)
    return seen


def _loaded(driver: Marionette, state: State, base: str) -> list[str]:
    """Wait for the state's proof element, tapping first if it needs one, then judge the page."""
    if state.act:
        seen = _wait(driver, state.armed, base)
        if not seen.get("ready"):
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
    for row in _rows(found, "spills"):
        tally.problems.append(
            f"{where}: {row['what']} spills out of the viewport "
            f"({row['left']:.1f}..{row['right']:.1f}px)"
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
        PROBE_GEOMETRY, list(SCROLL_ALLOWED), list(TAP_ALLOWED), TAPPABLE, TAP_FLOOR
    )
    judge(found if isinstance(found, dict) else {}, where, tally)
    print(f"  measured {where}", flush=True)


def stale(tally: Tally) -> list[str]:
    """An argued exception that nothing needed is a reason nobody re-checks; it must go."""
    listed = [*SCROLL_ALLOWED, *TAP_ALLOWED]
    return [
        f"exception {s} was not needed by any state: delete it"
        for s in listed
        if s not in tally.used
    ]


def report(tally: Tally, *, runs: int, whole: bool) -> int:
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
    chosen = pick(args.only)
    tally = Tally()
    switch = Switch()
    with tempfile.TemporaryDirectory(prefix="hp-geometry-") as scratch, serve(switch) as base:
        try:
            with browser() as driver:
                for width in WIDTHS:
                    phone_window(driver, width, HEIGHT)
                    for state in chosen:
                        install(switch, state, Path(scratch) / f"{width}")
                        run_state(driver, state, base, width, tally)
                    driver.send("WebDriver:CloseWindow", {})
                    driver.send("WebDriver:SwitchToWindow", {"handle": driver.handles()[0]})
        except BrowserError as exc:
            print(f"geometry: the browser failed: {exc}", file=sys.stderr)
            return 1
    return report(tally, runs=len(chosen) * len(WIDTHS), whole=len(chosen) == len(STATES))


if __name__ == "__main__":
    raise SystemExit(main())
