"""Drive the Chat and Pane tabs in headless Firefox: each opens on its newest line, follows new
lines while the reader is at the end, and leaves a reader who scrolled up where they were. An
agent that is asking opens at the top instead, with its question on the first screen. A tool's
result the reader opened stays open across a poll, and the page does not move under them. A
burst of tool calls lands folded, its failure in plain sight, and a fold the reader opened
stays open while calls are added to it.

The chat is paged by messages and live over an event stream: a tool-heavy transcript opens on
twenty messages, "Load earlier" lands the page above without moving the reader, one change is
one refresh, and a lost stream falls back to polling and then recovers."""

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from firefox import Marionette, phone_window
from starlette.types import ASGIApp, Receive, Scope, Send

from herdr_pwa.jsontypes import JsonObject
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
    runner,
    tool_heavy_chat,
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


def _call(n: int, *, failed: bool = False) -> list[JsonObject]:
    """One tool call and its result, with a result long enough to scroll when it is open."""
    call: JsonObject = {
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
    result: JsonObject = {
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


def _burst_chat(calls: int) -> list[JsonObject]:
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
    time.sleep(SETTLE_SECONDS)
    before = driver.script(READ_FOLD)
    # One more call joins the first run, before the failure: the run's text changes, its
    # first call does not.
    grown = _burst_chat(BURST)
    at = TURNS + 2 * FAILS_AT
    write_transcript(home / "transcripts", WORKING_SESSION, [*grown[:at], *_call(99), *grown[at:]])
    driver.script(POKE)
    deadline = time.monotonic() + WAIT_SECONDS
    after: object = None
    while time.monotonic() < deadline:
        after = driver.script(READ_FOLD)
        if isinstance(after, dict) and after.get("swaps") and not after.get("busy"):
            break
        time.sleep(0.1)
    time.sleep(SETTLE_SECONDS)
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
    return problems + _fold_problems(tab.name, after, opened=True)


# Paging and the event stream.

PAGE_MESSAGES = 20
# The rig's stream: a quick watcher, and a fallback poll short enough to see inside a walk.
LIVE_SETTINGS: dict[str, object] = {"events_interval": 0.2, "events_fallback": 2}
# Longer than one fallback poll: a page that polls while its stream is up shows here.
QUIET_SECONDS = 3.0
# How long a second refresh would take to land after the first.
SECOND_SECONDS = 1.5
OUTAGE_SECONDS = 6.0
# Three fallback polls fit in the outage; two leave room for the drop to be noticed.
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


def _until(done: Callable[[], bool], seconds: float = WAIT_SECONDS) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if done():
            return True
        time.sleep(0.1)
    return False


def _append(home: Path, text: str, uuid: str) -> str:
    """Append one agent reply to the transcript, the way the agent writes it."""
    log = next((home / "transcripts").rglob(f"{WORKING_SESSION}.jsonl"))
    with log.open("a") as handle:
        handle.write(json.dumps(assistant_says(text, uuid=uuid)) + "\n")
    return text


def _count(driver: Marionette) -> int:
    found = driver.script(READ_COUNT)
    return int(found) if isinstance(found, int | float) else -1


def _open_live(driver: Marionette, base: str, tab: Tab, rig: Rig) -> list[str]:
    driver.send("WebDriver:Navigate", {"url": base + tab.state.path})
    if not _wait_for(driver, tab.newest):
        return [f"{tab.name}: never drew its newest turn"]
    if not _until(lambda: _hub(rig).listeners >= 1):
        return [f"{tab.name}: the page never opened its event stream"]
    time.sleep(SETTLE_SECONDS)
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
    time.sleep(SETTLE_SECONDS)
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
    time.sleep(SETTLE_SECONDS)
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
    time.sleep(QUIET_SECONDS)
    if _count(driver):
        problems.append(
            f"{tab.name}: a live page fetched its chat {_count(driver)}x with no change"
        )
    driver.script(COUNT_CHAT)
    newest = _append(home, "Event reply one.", "ev1")
    if not _wait_for(driver, newest):
        return [*problems, f"{tab.name}: the change never reached the page"]
    time.sleep(SECOND_SECONDS)
    if _count(driver) != 1:
        problems.append(f"{tab.name}: one change made {_count(driver)} chat refreshes, not 1")
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
        mount(_refusing(rig.http.app))
        hub.drop_all()
        time.sleep(OUTAGE_SECONDS)
        if _count(driver) < MIN_FALLBACK_POLLS:
            problems.append(f"{tab.name}: with the stream down the page polled {_count(driver)}x")
        newest = _append(home, "Polled reply one.", "pl1")
        if not _wait_for(driver, newest):
            problems.append(f"{tab.name}: a change never arrived while the stream was down")
        mount(rig.http.app)
        if not _until(lambda: hub.listeners >= 1, RECONNECT_SECONDS):
            return [*problems, f"{tab.name}: the stream never came back"]
        time.sleep(SECOND_SECONDS)
        driver.script(COUNT_CHAT)
        time.sleep(QUIET_SECONDS)
        if _count(driver):
            problems.append(
                f"{tab.name}: still polling ({_count(driver)}x) after the stream came back"
            )
        newest = _append(home, "Recovered reply one.", "rc1")
        if not _wait_for(driver, newest):
            return [*problems, f"{tab.name}: the recovered stream never brought a change"]
        time.sleep(SECOND_SECONDS)
        if _count(driver) != 1:
            problems.append(
                f"{tab.name}: after recovery one change made {_count(driver)} refreshes"
            )
        return problems

    return walk


def _tabs() -> tuple[tuple[Tab, Callable[[Marionette, str, Tab, Rig, Path], list[str]]], ...]:
    blocked = runner(lambda: herd(**BLOCKED), long_pane(LINES) + capture(MENU))
    chat = State("chat", f"{AGENT}?tab=chat", "", runner(snapshot_frame), long_chat(TURNS))
    pane = State("pane", f"{AGENT}?tab=pane", "", runner(snapshot_frame, long_pane(LINES)))
    asking_chat = State("asking chat", chat.path, "", blocked, long_chat(TURNS))
    asking_pane = State("asking pane", pane.path, "", blocked)
    last_reply = f"Reply {TURNS - 1}:"
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
        (Tab("pane end", pane, last_line, _print), follow),
        (Tab("chat question", asking_chat, last_reply, _say), asked),
        (Tab("pane question", asking_pane, "", _print), asked),
    )


def run(
    driver: Marionette, base: str, mount: Callable[[ASGIApp], None], scratch: Path
) -> list[str]:
    """Every walk on a fresh app in its own phone window; empty when each tab keeps its end."""
    problems: list[str] = []
    chat = State("stream loss", f"{AGENT}?tab=chat", "", runner(snapshot_frame), long_chat(TURNS))
    lost = State(chat.name, chat.path, "", chat.replies, chat.transcript, settings=LIVE_SETTINGS)
    walks = [
        *_tabs(),
        (
            Tab("stream loss", lost, f"Reply {TURNS - 1}:", _say),
            falls_back(mount),
        ),
    ]
    for tab, walk in walks:
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
