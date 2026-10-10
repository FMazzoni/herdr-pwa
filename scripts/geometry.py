"""Drive headless Firefox over the real app at phone widths and hold its geometry to account."""

import argparse
import logging
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import uvicorn
from starlette.types import ASGIApp, Receive, Scope, Send

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from firefox import BrowserError, Marionette, browser, free_port, phone_window  # noqa: E402

from herdr_pwa.engine.transport import EngineUnavailable  # noqa: E402
from herdr_pwa.jsontypes import JsonObject, as_object  # noqa: E402
from tests.webfakes import (  # noqa: E402
    FIXTURES,
    READ,
    SNAPSHOT,
    WORKING_SESSION,
    Reply,
    agents_of,
    assistant_says,
    build_rig,
    human_says,
    read_frame,
    snapshot_frame,
    write_transcript,
)

# 390 is the phone this app is designed on; 320 is the narrowest one still sold (iPhone SE 1st gen).
WIDTHS = (390, 320)
HEIGHT = 844
# Apple's HIG minimum and WCAG 2.5.5: 44 CSS px in both directions.
TAP_FLOOR = 44
READY_SECONDS = 15.0
LOGIN = "geometry@example.com"

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
    ".prose a": (
        "a link inside a sentence the agent wrote; padding it to 44px would tear the line it "
        "sits in apart, and WCAG 2.5.5 exempts targets inline in a block of text"
    ),
}

TAPPABLE = "a[href], button, summary, input, select, textarea, [role=button], [role=link]"

LONG_TITLE = (
    "Reconcile the quarterly infrastructure migration plan across every regional cluster "
    "and write the rollback runbook before the freeze"
)
# No hyphen or slash in it, since either is a place a browser may break the line.
UNBROKEN = "refactorTheEntireNotificationPipelineSoThatEveryRetryIsIdempotent"
UNBROKEN_TITLE = "renameEveryLegacyIdentifierAcrossTheMonorepoWithoutTouchingTests"
LONG_URL = "https://example.com/" + "a1b2c3d4" * 24 + "?query=" + "x" * 80
LONG_QUESTION = (
    "Should I force-push the rewritten history of "
    f"/home/owner/projects/{UNBROKEN}/src/very/deep/module/path.py to main, which "
    "overwrites three commits that other people may already have pulled?"
)
WIDE_ROW = "│ " + " ".join(f"col{i:03d}" for i in range(40)) + " │"
WIDE_UNBROKEN = "x" * 260
WIDE_RULE = "─" * 220
CODE = (
    "def migrate(cluster: str) -> None:\n"
    f"    run(['kubectl', '--context', cluster, 'apply', '-f', '{UNBROKEN}.yaml', "
    "'--server-side', '--force-conflicts', '--field-manager=migration'])\n"
)

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


@dataclass(frozen=True)
class State:
    """One screen of the real app, the fake runner behind it, and the element proving it drew."""

    name: str
    path: str
    ready: str
    replies: Callable[[], dict[str, Reply]]
    transcript: list[JsonObject] = field(default_factory=list[JsonObject])


def _snapshot(frame: JsonObject) -> JsonObject:
    result = as_object(frame["result"])
    snapshot = as_object(result["snapshot"]) if result is not None else None
    if snapshot is None:
        raise RuntimeError("the snapshot fixture changed shape")
    return snapshot


def _herd() -> JsonObject:
    """The recorded snapshot, edited to hold every status and the longest names a card can get."""
    frame = snapshot_frame()
    agents = agents_of(frame)
    edits = (
        ("blocked", "Waiting on a decision"),
        ("done", "Review the open notes"),
        ("idle", LONG_TITLE),
        ("idle", UNBROKEN_TITLE),
    )
    for agent, (status, title) in zip(agents, edits, strict=False):
        agent["agent_status"] = status
        agent["terminal_title"] = title
        agent["terminal_title_stripped"] = title
    workspaces = _snapshot(frame)["workspaces"]
    for workspace in workspaces if isinstance(workspaces, list) else []:
        if isinstance(workspace, dict) and workspace.get("workspace_id") == "w1":
            workspace["label"] = UNBROKEN
    return frame


def _empty() -> JsonObject:
    frame = snapshot_frame()
    _snapshot(frame)["agents"] = []
    return frame


def _question() -> str:
    """The captured AskUserQuestion screen with a question and an option no card was sized for."""
    screen = (FIXTURES / "capture-blocked-askuserquestion-menu" / "detection.txt").read_text()
    screen = screen.replace("Should I force-push to main?", LONG_QUESTION)
    return screen.replace("Open a PR instead", f"Open a PR from {UNBROKEN} instead")


def _pane() -> str:
    """A terminal wider than any phone: a table row, an unbroken token and a rule."""
    rows = (
        "\x1b[32m$\x1b[0m make test",
        WIDE_ROW,
        WIDE_UNBROKEN,
        WIDE_RULE,
        f"\x1b[31merror:\x1b[0m {LONG_URL}",
    )
    return "\n".join(rows) + "\n"


def _chat() -> list[JsonObject]:
    tool_call: JsonObject = {
        "type": "assistant",
        "uuid": "call",
        "timestamp": "2026-10-10T10:00:01.000Z",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "tu1", "name": "Bash", "input": {"command": CODE}}
            ],
        },
    }
    tool_result: JsonObject = {
        "type": "user",
        "uuid": "res",
        "timestamp": "2026-10-10T10:00:02.000Z",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "tu1", "content": WIDE_ROW}],
        },
    }
    prose = (
        f"Done. The plan is below.\n\n```python\n{CODE}```\n\n"
        f"Raw link: {LONG_URL}\n\nAnd [the runbook]({LONG_URL}) as a link."
    )
    return [
        human_says(f"please look at {LONG_URL} and {UNBROKEN}"),
        tool_call,
        tool_result,
        assistant_says(prose, uuid="a2"),
    ]


def _with_read(text: str) -> Callable[[], dict[str, Reply]]:
    return lambda: {SNAPSHOT: _herd(), READ: read_frame(text)}


def _unreachable() -> dict[str, Reply]:
    return {SNAPSHOT: EngineUnavailable("geometry: no runner")}


STATES = (
    State("feed-empty", "/", "#feed-list .summary", lambda: {SNAPSHOT: _empty()}),
    State("feed-herd", "/", "#feed-list .cards", _with_read(_question())),
    State(
        "agent-chat",
        f"/agent/{WORKING_SESSION}?tab=chat",
        ".chat .turn--agent",
        _with_read(""),
        _chat(),
    ),
    State("agent-pane", f"/agent/{WORKING_SESSION}?tab=pane", ".mirror .pane", _with_read(_pane())),
    # A failure fragment only swaps in because app.js overrides htmx 2's no-swap on 5xx.
    State("feed-unreachable", "/", "main > .failure .banner--failure", _unreachable),
    State("agent-unreachable", f"/agent/{WORKING_SESSION}", "main .failure", _unreachable),
    State("workspaces", "/workspaces", ".cards .card", lambda: {SNAPSHOT: _herd()}),
)


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
    if state.transcript:
        write_transcript(home / "transcripts", WORKING_SESSION, state.transcript)
    rig = build_rig(home, state.replies(), headers={}, assume_login=LOGIN)
    switch.app = rig.http.app


def _loaded(driver: Marionette, state: State, base: str) -> list[str]:
    """Wait for the state's proof element, then say what is wrong with the page that loaded."""
    deadline = time.monotonic() + READY_SECONDS
    seen: dict[str, object] = {}
    while time.monotonic() < deadline:
        found = driver.script(PROBE_LOADED, state.ready, base)
        seen = found if isinstance(found, dict) else {}
        if seen.get("ready") or seen.get("errorPage"):
            break
        time.sleep(0.2)
    return _load_problems(seen, state)


def _load_problems(seen: dict[str, object], state: State) -> list[str]:
    problems: list[str] = []
    if seen.get("errorPage"):
        problems.append(f"this is not the app (Firefox error page?): {seen.get('url')}")
    if not seen.get("build"):
        problems.append("no hp-build meta: the base template did not render")
    if not seen.get("ground"):
        problems.append("--ground is unset: app.css did not apply (CSP, MIME or 404)")
    statics = seen.get("statics")
    fetched = {str(s) for s in statics} if isinstance(statics, list) else set()
    problems += [
        f"{name} was never fetched: blocked before it loaded"
        for name in ("/static/app.css", "/static/app.js", "/static/htmx.min.js")
        if name not in fetched
    ]
    if seen.get("htmx") != "object":
        problems.append("window.htmx is missing: htmx.min.js did not run")
    if seen.get("hidden"):
        problems.append("document.hidden: every poll would stop on HTTP 286")
    if not seen.get("ready"):
        problems.append(f"{state.ready} never appeared within {READY_SECONDS:.0f}s")
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


def report(tally: Tally, *, whole: bool) -> int:
    """Print the exceptions with their reasons and every problem; non-zero on any problem."""
    for kind, table in (("scroller", SCROLL_ALLOWED), ("tap", TAP_ALLOWED)):
        for selector, why in table.items():
            print(f"  allowed {kind} {selector} ({tally.used.get(selector, 0)}x): {why}")
    problems = tally.problems + (stale(tally) if whole else [])
    for problem in problems:
        print(f"  FAIL {problem}")
    runs = len(STATES) * len(WIDTHS)
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
    chosen = [s for s in STATES if not args.only or s.name in args.only]
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
    return report(tally, whole=len(chosen) == len(STATES))


if __name__ == "__main__":
    raise SystemExit(main())
