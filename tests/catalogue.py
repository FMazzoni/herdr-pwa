"""Every named screen the app can draw, each over its own fake runner, for gates and shots alike."""

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from pydantic import JsonValue

from herdr_pwa.engine.transport import EngineUnavailable
from herdr_pwa.jsontypes import JsonObject, as_object
from tests.filefakes import make_repo, stand_in
from tests.webfakes import (
    AGENT_GET,
    FIXTURES,
    READ,
    SEND_KEYS,
    SNAPSHOT,
    WORKING_PANE,
    WORKING_SESSION,
    FakeWire,
    Reply,
    Rig,
    agents_of,
    assistant_says,
    build_rig,
    human_says,
    read_frame,
    snapshot_frame,
    write_transcript,
)

type Replies = Callable[[], dict[str, Reply]]
type Prime = Callable[[Rig], None]

# The fixture's idle agent on the second workspace, which holds no other agent.
QUIET_WORKSPACE: Final = "w2"
LONELY_WORKSPACE: Final = "w4"

LONG_TITLE: Final = (
    "Reconcile the quarterly infrastructure migration plan across every regional cluster "
    "and write the rollback runbook before the freeze"
)
# No hyphen or slash in it, since either is a place a browser may break the line.
UNBROKEN: Final = "refactorTheEntireNotificationPipelineSoThatEveryRetryIsIdempotent"
UNBROKEN_TITLE: Final = "renameEveryLegacyIdentifierAcrossTheMonorepoWithoutTouchingTests"
LONG_URL: Final = "https://example.com/" + "a1b2c3d4" * 24 + "?query=" + "x" * 80
LONG_QUESTION: Final = (
    "Should I force-push the rewritten history of "
    f"/home/owner/projects/{UNBROKEN}/src/very/deep/module/path.py to main, which "
    "overwrites three commits that other people may already have pulled?"
)
WIDE_ROW: Final = "│ " + " ".join(f"col{i:03d}" for i in range(40)) + " │"
WIDE_UNBROKEN: Final = "x" * 260
WIDE_RULE: Final = "─" * 220
CODE: Final = (
    "def migrate(cluster: str) -> None:\n"
    f"    run(['kubectl', '--context', cluster, 'apply', '-f', '{UNBROKEN}.yaml', "
    "'--server-side', '--force-conflicts', '--field-manager=migration'])\n"
)
# The status line claude draws under its input box, which the Pane tab promotes.
STATUS_TAIL: Final = (
    "\n"
    + "─" * 158
    + "\n\u276f rerun the failing test with -vv and paste the traceback\n"
    + "─" * 158
    + "\n  \x1b[2m\x1b[38;2;153;153;153mherdr-pwa [main] | \x1b[0m"
    "\x1b[38;2;153;153;153mctx: \x1b[0m\x1b[38;5;2m16%\x1b[0m"
    "\x1b[2m\x1b[38;2;153;153;153m | 5h: \x1b[0m\x1b[38;5;3m22%\x1b[0m\n"
    "  \x1b[0m\x1b[38;2;153;153;153m⏸ auto mode on · ← 1 agent\x1b[0m"
)


# The old catalogue's synthetic wizard step: one question of three, its rows tappable, none final.
WIZARD_STEP: Final = (
    "\u2190  \u2612 Focus area  \u2610 Scope  \u2610 Workflow  \u2714 Submit  \u2192\n"
    "\n"
    "How should I approach the work?\n"
    "\n"
    "\u276f 1. Plan first\n"
    "     I draft a plan and get your approval before writing any code.\n"
    "  2. Just build it\n"
    "     I go straight to implementation and show you the diff when done.\n"
    "  3. Build + verify\n"
    "     I implement, then drive the app end-to-end before handing back.\n"
    "  4. Type something.\n"
    "\n"
    "Enter to select \u00b7 Tab/Arrow keys to navigate \u00b7 Esc to cancel\n"
)


@dataclass(frozen=True)
class State:
    """One screen of the real app, the fake runner behind it, and the element proving it drew.

    ``act`` is script run once ``armed`` is on the page, for a screen only a tap reaches;
    ``ready`` is then the proof of the screen the tap produced.
    """

    name: str
    path: str
    ready: str
    replies: Replies
    transcript: list[JsonObject] = field(default_factory=list[JsonObject])
    title: str = ""
    armed: str = ""
    act: str = ""
    login: bool = True
    # A failure screen, served with the 5xx status the app gives it on purpose.
    fails: bool = False
    # The offline fallback is replayed by the worker with no script, so none is expected.
    scripted: bool = True
    prime: Prime | None = None
    settings: dict[str, object] = field(default_factory=dict[str, object])


def capture(name: str) -> str:
    """One captured terminal screen from the fixtures."""
    return (FIXTURES / name / "detection.txt").read_text()


PERMISSION: Final = "capture-blocked-permission-prompt"
PERMISSION_WRAPPED: Final = "capture-blocked-permission-prompt-wrapped"
MENU: Final = "capture-blocked-askuserquestion-menu"
MENU_WRAPPED: Final = "capture-blocked-askuserquestion-wrapped"
CHECKLIST: Final = "capture-blocked-multiselect-checklist"
PLAN: Final = "capture-blocked-plan-approval"
IDLE: Final = "capture-idle-claude-prompt"
WORKING: Final = "capture-working-claude"
CAPTURES: Final = (
    PERMISSION,
    PERMISSION_WRAPPED,
    MENU,
    MENU_WRAPPED,
    CHECKLIST,
    PLAN,
    IDLE,
    WORKING,
)

# Panes of the fixture's agents, in snapshot order.
PANES: Final = (
    "w652ae557d5b9c1:p2",
    "w4:p1",
    "w654c7902e43791:pA",
    "w1:p1",
    WORKING_PANE,
    "w2:p1",
)


def _snapshot(frame: JsonObject) -> JsonObject:
    result = as_object(frame["result"])
    snapshot = as_object(result["snapshot"]) if result is not None else None
    if snapshot is None:
        raise RuntimeError("the snapshot fixture changed shape")
    return snapshot


def _set(frame: JsonObject, pane: str, **fields: str) -> JsonObject:
    """Edit the agent at ``pane`` in place; a ``session`` of ``""`` takes its identity away."""
    agent = next(a for a in agents_of(frame) if a.get("pane_id") == pane)
    for key, value in fields.items():
        if key == "session" and not value:
            agent.pop("agent_session", None)
        elif key == "title":
            agent["terminal_title"] = value
            agent["terminal_title_stripped"] = value
        else:
            agent[key] = value
    return frame


def herd(**statuses: str) -> JsonObject:
    """The recorded snapshot with chosen panes moved to a status, keyed ``p<index>``."""
    frame = snapshot_frame()
    for key, status in statuses.items():
        _set(frame, PANES[int(key[1:])], agent_status=status)
    return frame


def _stressed() -> JsonObject:
    """The recorded snapshot, edited to hold every status and the longest names a card can get."""
    frame = snapshot_frame()
    edits = (
        ("blocked", "Waiting on a decision"),
        ("done", "Review the open notes"),
        ("idle", LONG_TITLE),
        ("idle", UNBROKEN_TITLE),
    )
    for agent, (status, title) in zip(agents_of(frame), edits, strict=False):
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


def _long_question() -> str:
    """The captured AskUserQuestion screen with a question and an option no card was sized for."""
    screen = capture(MENU).replace("Should I force-push to main?", LONG_QUESTION)
    return screen.replace("Open a PR instead", f"Open a PR from {UNBROKEN} instead")


def _wide_pane() -> str:
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


# Everything chat prose renders beyond plain text: tables narrow and wide, math, code, ticks.
RICH_PROSE: Final = r"""Filed your three picks:

| Ticket | Change |
|---|---|
| `a11y` | **Accessibility.** All sizes follow your iPhone's Text Size setting, and nothing is smaller than 13px. |
| `toggle` | **Chat/Pane toggle in the top bar** beside "Herd", so it never scrolls away. |

| Closeness | What | Covers |
|:--|:-:|--:|
| Close | Safari's engine (WebKit) set up as your phone | Layout, scrolling, focus |
| Can't reproduce | iOS fonts, the keyboard animation | Nothing |

| Service | Region | Replicas | p99 latency | Last deploy |
|---|---|---|---|---|
| `checkout-api` | eu-west-1 | 12 | 184ms | 2026-10-09 14:02 UTC |
| `notifications` | ap-southeast-1 | 3 | 96ms | 2026-10-10 07:15 UTC |

The mean is $\bar{x} = \frac{1}{n}\sum_{i=1}^{n} x_i$, and a failed one shows $\frac{a}{$.

$$
f(x) = a_0 + a_1 x + a_2 x^2 + a_3 x^3 + a_4 x^4 + a_5 x^5 + a_6 x^6 + a_7 x^7 + a_8 x^8
$$

```sh
uv sync && systemctl --user restart herdr-pwa.service && herdr-pwa doctor --verbose
```

- [x] Rebase onto main
- [ ] Restart the bridge

The runbook is at https://example.com/runbook#restart.
"""


def rich_chat() -> list[JsonObject]:
    """One agent turn using every kind of prose the Chat tab renders."""
    return [human_says("what changed?", uuid="u0"), assistant_says(RICH_PROSE, uuid="a1")]


def long_chat(turns: int = 24) -> list[JsonObject]:
    """A back-and-forth several screens tall, numbered so the newest turn is easy to find."""
    records: list[JsonObject] = []
    for n in range(turns):
        if n % 2:
            text = f"Reply {n}: checked the cluster and the rollback.\n\n" + "- a step\n" * 3
            records.append(assistant_says(text, uuid=f"a{n}"))
        else:
            records.append(human_says(f"Prompt {n}: what does the migration touch?", uuid=f"u{n}"))
    return records


def _tool_record(kind: str, uuid: str, content: JsonValue) -> JsonObject:
    message: JsonObject = {"role": kind, "content": content}
    return {"type": kind, "uuid": uuid, "timestamp": "2026-10-10T10:00:00.000Z", "message": message}


def tool_heavy_chat(messages: int = 60, calls: int = 6) -> list[JsonObject]:
    """An agent at work: every message followed by a burst of tool calls and their results."""
    records: list[JsonObject] = []
    n = 0
    for m in range(messages):
        if m % 5 == 0:
            records.append(human_says(f"Prompt {m}: keep going on the migration.", uuid=f"h{m}"))
        else:
            text = f"Message {m}: the step landed; running the next check."
            records.append(assistant_says(text, uuid=f"a{m}"))
        for _ in range(calls):
            n += 1
            use: JsonObject = {
                "type": "tool_use",
                "id": f"tu{n}",
                "name": "Bash",
                "input": {"command": f"pytest -q tests/test_step_{n}.py"},
            }
            result: JsonObject = {
                "type": "tool_result",
                "tool_use_id": f"tu{n}",
                "content": f"{n} passed in 0.{n % 10}s",
            }
            records += [
                _tool_record("assistant", f"c{n}", [use]),
                _tool_record("user", f"r{n}", [result]),
            ]
    return records


def long_pane(lines: int = 120) -> str:
    """Terminal scrollback several screens tall, numbered so the newest line is easy to find."""
    return "".join(f"\x1b[32m$\x1b[0m step {n:03d}: applied to the cluster\n" for n in range(lines))


def runner(frame: Callable[[], JsonObject], screen: str = "") -> Replies:
    """A runner whose snapshot is ``frame()`` and whose every pane shows ``screen``."""
    return lambda: {SNAPSHOT: frame(), READ: read_frame(screen)}


def per_pane(frame: Callable[[], JsonObject], screens: dict[str, str]) -> Replies:
    """A runner whose panes each show their own screen, and an idle prompt elsewhere."""

    def replies() -> dict[str, Reply]:
        found: dict[str, Reply] = {SNAPSHOT: frame(), READ: read_frame(capture(IDLE))}
        for pane, screen in screens.items():
            found[f"{READ} {pane}"] = read_frame(screen, pane)
        return found

    return replies


def _unreachable() -> dict[str, Reply]:
    return {SNAPSHOT: EngineUnavailable("catalogue: no runner")}


def _unreadable(frame: Callable[[], JsonObject]) -> Replies:
    return lambda: {SNAPSHOT: frame(), READ: EngineUnavailable("catalogue: the read failed")}


def _outage(after: float, *, failing: bool) -> Prime:
    """Read the herd once, then let ``after`` seconds pass, with the runner gone if ``failing``."""

    def prime(rig: Rig) -> None:
        _ = rig.http.get("/feed")
        if failing:
            rig.wire.replies[SNAPSHOT] = EngineUnavailable("catalogue: the runner went away")
        rig.clock.now += after

    return prime


def _untitled() -> JsonObject:
    """Three agents in one repo whose titles are the shell's prompt string."""
    frame = snapshot_frame()
    shell = "user@host:~/projects"
    for pane, status in zip(PANES[3:], ("working", "idle", "idle"), strict=True):
        _set(frame, pane, title=shell, cwd="/home/user/projects", agent_status=status)
    return frame


def _opencode() -> JsonObject:
    """Four opencode panes that all write the same constant terminal title."""
    frame = snapshot_frame()
    for pane in PANES[:4]:
        _set(frame, pane, agent="opencode", title="OpenCode")
    return frame


def _door(**fields: str) -> Callable[[], JsonObject]:
    """The working pane with no session yet, as a just-started agent has."""
    return lambda: _set(snapshot_frame(), WORKING_PANE, session="", **fields)


def _two_blocked(first: str, second: str) -> Replies:
    """The working pane and its neighbour blocked, each on its own captured question."""
    frame = lambda: herd(p4="blocked", p3="blocked")  # noqa: E731 -- one call site
    return per_pane(frame, {WORKING_PANE: capture(first), PANES[3]: capture(second)})


def _lonely_gone() -> JsonObject:
    """The snapshot with the one agent of its workspace closed."""
    frame = snapshot_frame()
    snapshot = _snapshot(frame)
    snapshot["agents"] = [a for a in agents_of(frame) if a.get("pane_id") != PANES[1]]
    return frame


def _untick(screen: str) -> str:
    return screen.replace("[✔] autumn", "[ ] autumn")


def _agent_info(frame: JsonObject) -> JsonObject:
    agent = next(a for a in agents_of(frame) if a.get("pane_id") == WORKING_PANE)
    return {"id": "x", "result": {"type": "agent_info", "agent": agent}}


def _keyed(screen: str) -> Replies:
    """The working pane blocked on ``screen``, with a runner that takes a key."""

    def replies() -> dict[str, Reply]:
        frame = herd(**BLOCKED)
        return {
            SNAPSHOT: frame,
            READ: read_frame(screen),
            AGENT_GET: _agent_info(frame),
            SEND_KEYS: {"id": "x", "result": {"type": "ok"}},
        }

    return replies


def _lands(status: str, screen: str) -> Prime:
    """Once a key is taken, the herd says ``status`` and the pane shows ``screen``."""

    def prime(rig: Rig) -> None:
        def landed(wire: FakeWire) -> None:
            frame = herd(p4=status)
            wire.replies.update(
                {SNAPSHOT: frame, AGENT_GET: _agent_info(frame), READ: read_frame(screen)}
            )

        rig.wire.after[SEND_KEYS] = landed

    return prime


AGENT: Final = f"/agent/{WORKING_SESSION}"
BLOCKED: Final = {"p4": "blocked"}
DOOR: Final = f"/starting/{WORKING_PANE}"


def _blocked_on(name: str, screen: Callable[[str], str] = str) -> Replies:
    return runner(lambda: herd(**BLOCKED), screen(capture(name)))


def _type_and_send(text: str) -> str:
    return (
        f"document.querySelector('#composer textarea').value = {text!r};"
        "document.querySelector('#composer button[type=submit]').click();"
    )


def _click(selector: str) -> str:
    return f"document.querySelector({selector!r}).click();"


def _tamper(field_name: str, value: str, then: str) -> str:
    return (
        f"for (const el of document.querySelectorAll('input[name={field_name}]')) "
        f"el.value = {value!r};" + _click(then)
    )


FEED: Final = (
    State("feed-empty", "/", "#feed-list .summary", lambda: {SNAPSHOT: _empty()}),
    State("feed-herd", "/", "#feed-list .cards", runner(_stressed, _long_question())),
    State(
        "feed-build-scrollback",
        "/",
        "#feed-list .card--blocked .card__question--unread",
        runner(_stressed, long_pane()),
        title="Feed: a blocked agent whose screen is coloured build output, not a question",
    ),
    State(
        "feed-blocked",
        "/",
        "#feed-list .card--blocked",
        _blocked_on(MENU),
        title="Feed: a blocked agent on top, its question inline",
    ),
    State(
        "feed-question",
        "/",
        "#feed-list .card--blocked",
        _two_blocked(MENU, PERMISSION),
        title="Feed: two blocked agents, their options as tap targets",
    ),
    State(
        "feed-permission",
        "/",
        "#feed-list .card--blocked",
        _two_blocked(PERMISSION, PLAN),
        title="Feed: permission and plan prompts, and what they ask about",
    ),
    State(
        "feed-untitled",
        "/",
        "#feed-list .cards",
        runner(_untitled),
        title="Feed: three agents in one repo, no real titles",
    ),
    State(
        "feed-opencode",
        "/",
        "#feed-list .cards",
        runner(_opencode),
        title="Feed: four opencode panes, one terminal title between them",
    ),
    State(
        "feed-quiet",
        "/",
        "#feed-list .cards",
        runner(snapshot_frame),
        title="Feed: nothing needs you",
    ),
    State(
        "feed-filtered",
        f"/?ws={LONELY_WORKSPACE}",
        "#feed-list .chips .chip--on",
        _blocked_on(MENU),
        title="Feed: narrowed to one workspace, needs-you still whole above it",
    ),
    State(
        "feed-degraded",
        "/",
        "#feed-list .banner--degraded",
        runner(snapshot_frame),
        title="Feed: the runner stopped answering a moment ago",
        prime=_outage(5, failing=True),
    ),
    State(
        "feed-unreadable-question",
        "/",
        "#feed-list .card--blocked",
        _unreadable(lambda: herd(**BLOCKED)),
        title="Feed: a blocked card whose question could not be read",
    ),
    State(
        "feed-stale",
        "/",
        "#feed-list .banner--stale",
        runner(snapshot_frame),
        title="Feed: a snapshot past three cadences old",
        prime=_outage(44, failing=False),
        settings={"snapshot_cache_ttl": 3600.0},
    ),
    State(
        "feed-offline",
        "/",
        "#feed-list .banner--offline",
        runner(snapshot_frame),
        title="Feed: no contact for a quarter of an hour",
        prime=_outage(930, failing=True),
    ),
    # A failure fragment only swaps in because app.js overrides htmx 2's no-swap on 5xx.
    State(
        "unreachable",
        "/",
        "main > .failure .banner--failure",
        _unreachable,
        title="Herd unreachable: the app is up and the runner is not",
        fails=True,
    ),
    State(
        "refused",
        "/",
        "body",
        runner(snapshot_frame),
        title="Refused at the identity gate",
        login=False,
    ),
    State(
        "offline-page",
        "/offline",
        "main .banner--offline",
        runner(snapshot_frame),
        title="Offline: the service worker's fallback",
        scripted=False,
    ),
    State("workspaces", "/workspaces", ".cards .card", lambda: {SNAPSHOT: _stressed()}),
    State("alerts", "/push", "main.sheet", runner(snapshot_frame), title="Alerts settings"),
)

AGENT_PAGE: Final = (
    State(
        "agent-chat",
        f"{AGENT}?tab=chat",
        ".chat .turn--agent",
        runner(_stressed),
        _chat(),
        title="Agent page: Chat, the default tab",
    ),
    State(
        "chat-long",
        f"{AGENT}?tab=chat",
        ".chat .turn--agent",
        runner(_stressed),
        long_chat(),
        title="Chat: a transcript several screens tall, opened on its newest turn",
    ),
    State(
        "chat-tools",
        f"{AGENT}?tab=chat",
        ".chat .turn--agent",
        runner(_stressed),
        tool_heavy_chat(),
        title="Chat: a tool-heavy transcript, twenty messages with their tool calls between them",
    ),
    State(
        "chat-older",
        f"{AGENT}?tab=chat",
        '.chat article[data-hp-key="turn:h20"]',
        runner(_stressed),
        tool_heavy_chat(),
        title="Chat: one earlier page loaded above the newest, the reader's place kept",
        armed=".chat__earlier-button",
        act=_click(".chat__earlier-button"),
    ),
    State(
        "chat-markdown",
        f"{AGENT}?tab=chat",
        ".chat .turn--agent .table-scroll",
        runner(_stressed),
        rich_chat(),
        title="Chat: tables, math, highlighted code, a task list and a bare link",
    ),
    State(
        "chat-blocked",
        f"{AGENT}?tab=chat",
        "#answer .answer__question",
        _blocked_on(MENU),
        long_chat(),
        title="Chat: a long transcript under a question, which keeps the first screen",
    ),
    State(
        "pane-long",
        f"{AGENT}?tab=pane",
        ".mirror .pane",
        runner(_stressed, long_pane()),
        title="Agent page: Pane, scrollback several screens tall, opened on its newest line",
    ),
    State(
        "chat-absent",
        f"{AGENT}?tab=chat",
        ".chat .empty",
        runner(snapshot_frame, capture(WORKING)),
        title="Chat: no transcript was ever written",
    ),
    State(
        "agent-pane",
        f"{AGENT}?tab=pane",
        ".mirror .pane",
        runner(_stressed, _wide_pane()),
        title="Agent page: Pane, a terminal wider than the phone",
    ),
    State(
        "agent-pane-chrome",
        f"{AGENT}?tab=pane",
        ".mirror .pane--status",
        runner(snapshot_frame, capture(WORKING) + STATUS_TAIL),
        title="Agent page: Pane, with the agent's status line promoted",
    ),
    State(
        "agent-unreachable",
        AGENT,
        "main .failure",
        _unreachable,
        title="Agent page: the runner cannot be read",
        fails=True,
    ),
    State(
        "agent-answer",
        f"{AGENT}?tab=pane",
        "#answer .button--option",
        _blocked_on(PERMISSION),
        title="Agent page: a blocked agent, answerable with one tap",
    ),
    State(
        "agent-answer-plan",
        f"{AGENT}?tab=pane",
        "#answer .answer__question",
        _blocked_on(PLAN),
        title="Agent page: the plan-approval screen",
    ),
    State(
        "agent-answer-menu",
        f"{AGENT}?tab=pane",
        "#answer .answer__question",
        _blocked_on(MENU),
        title="Agent page: an AskUserQuestion menu",
    ),
    State(
        "agent-answer-multiselect",
        f"{AGENT}?tab=pane",
        "#answer .answer__question",
        _blocked_on(CHECKLIST, _untick),
        title="Agent page: a multi-select, nothing ticked",
    ),
    State(
        "agent-answer-checklist",
        f"{AGENT}?tab=pane",
        "#answer .answer__question",
        _blocked_on(CHECKLIST),
        title="Agent page: a checklist with one box already ticked",
    ),
    State(
        "agent-answer-wizard-step",
        f"{AGENT}?tab=pane",
        "#answer .button--option",
        runner(lambda: herd(**BLOCKED), WIZARD_STEP),
        title="Agent page: one step of a three-question wizard",
    ),
    State(
        "agent-options-refused",
        f"{AGENT}?tab=pane",
        "#answer .keys--compact",
        _blocked_on(MENU_WRAPPED),
        title="Agent page: the options wrapped, so none are offered as digits",
    ),
    State(
        "agent-options-refused-permission",
        f"{AGENT}?tab=pane",
        "#answer .keys--compact",
        _blocked_on(PERMISSION_WRAPPED),
        title="Agent page: a permission prompt whose host ran off the edge",
    ),
    State(
        "agent-unreadable",
        AGENT,
        "#answer .keys--pad",
        _unreadable(lambda: herd(**BLOCKED)),
        title="Agent page: blocked, and the question could not be read",
    ),
)

TAPPED: Final = (
    State(
        "agent-refused",
        AGENT,
        "#composer [role=alert]",
        runner(snapshot_frame, capture(PERMISSION)),
        title="Agent page: a prompt refused because the screen shows a question",
        armed="#composer textarea",
        act=_type_and_send("run the tests again"),
    ),
    State(
        "destructive-confirm",
        AGENT,
        "#composer [role=alert]",
        runner(lambda: herd(p4="idle"), capture(IDLE)),
        title="Destructive input: held for a second tap",
        armed="#composer textarea",
        act=_type_and_send("rm -rf build && git push --force"),
    ),
    State(
        "agent-interrupt",
        f"{AGENT}?tab=pane",
        "#answer [role=alert]",
        _blocked_on(PERMISSION),
        title="Agent page: ctrl-c, armed",
        armed="#answer .button--danger",
        act=_click("#answer .button--danger"),
    ),
    State(
        "agent-pane-columns",
        f"{AGENT}?tab=pane",
        ".is-columns .pane",
        runner(_stressed, _wide_pane()),
        title="Agent page: Pane, keeping the terminal's columns",
        armed="[data-hp-columns]",
        act=_click("[data-hp-columns]"),
    ),
    State(
        "agent-chat-composing",
        f"{AGENT}?tab=chat",
        "#composer textarea:focus",
        runner(_stressed),
        _chat(),
        title="Agent page: Chat, the composer focused",
        armed="#composer textarea",
        act="document.querySelector('#composer textarea').focus();",
    ),
    State(
        "agent-key-refused",
        f"{AGENT}?tab=pane",
        "#answer [role=alert]",
        _blocked_on(PERMISSION),
        title="Agent page: the screen moved underneath a tapped option",
        armed="#answer .button--option",
        act=_tamper("screen", "0000000000000000", "#answer .button--option"),
    ),
)

_TAP_OPTION: Final = _click("#answer .button--option")

ANSWERED: Final = (
    State(
        "agent-answer-confirmed",
        f"{AGENT}?tab=pane",
        "#answer .banner--sent",
        _keyed(capture(PERMISSION)),
        title="Agent page: a tapped option, and the question went away",
        armed="#answer .button--option",
        act=_TAP_OPTION,
        prime=_lands("working", capture(WORKING)),
    ),
    State(
        "agent-answer-confirmed-moved",
        f"{AGENT}?tab=pane",
        "#answer .banner--sent",
        _keyed(WIZARD_STEP),
        title="Agent page: a wizard row tapped, and the next question is up",
        armed="#answer .button--option",
        act=_TAP_OPTION,
        prime=_lands("blocked", capture(MENU)),
    ),
    State(
        "agent-answer-unconfirmed",
        f"{AGENT}?tab=pane",
        "#answer .banner--unsure",
        _keyed(capture(PERMISSION)),
        title="Agent page: a tapped option, and the question is still showing",
        armed="#answer .button--option",
        act=_TAP_OPTION,
    ),
    State(
        "agent-answer-unconfirmed-moved",
        f"{AGENT}?tab=pane",
        "#answer .banner--unsure",
        _keyed(capture(PERMISSION)),
        title="Agent page: a tapped option, and a different question is up",
        armed="#answer .button--option",
        act=_TAP_OPTION,
        prime=_lands("blocked", capture(MENU)),
    ),
)

LIFECYCLE: Final = (
    State("new-agent", "/new", "main form", runner(snapshot_frame), title="New agent: the form"),
    State(
        "new-agent-refused",
        "/new",
        "main [role=alert]",
        runner(snapshot_frame),
        title="New agent: refused, with the typing still in it",
        armed="main form input[name=cwd]",
        act=(
            "document.querySelector('input[name=name]').value = 'tidy the docs';"
            "document.querySelector('input[name=cwd]').value = 'projects/notes';"
            "document.querySelector('main form').submit();"
        ),
    ),
    State(
        "agent-starting",
        "/starting/w1:p9",
        "main .empty",
        runner(snapshot_frame),
        title="Started, and not yet an agent",
    ),
    State(
        "agent-first-block",
        DOOR,
        "main .keys--pad",
        runner(_door(agent_status="blocked"), capture(PERMISSION)),
        title="It blocked before it had an identity",
    ),
    State(
        "first-block-armed",
        DOOR,
        "main [role=alert]",
        runner(_door(agent_status="blocked"), capture(PERMISSION)),
        title="The interrupt, armed, with no session",
        armed="main .keys--pad",
        act=_click("main .keys--pad form:last-child button"),
    ),
    State(
        "agent-starting-opencode",
        DOOR,
        "main .agent__meta",
        runner(_door(agent="opencode", agent_status="idle")),
        title="Started opencode, whose identity comes with its first prompt",
    ),
    State(
        "close-confirm",
        "/agent/5b609dba-d756-493a-a3fc-b4e09bf0a183/close",
        "main form",
        runner(snapshot_frame),
        title="End this agent: the confirm",
    ),
    State(
        "close-working",
        f"{AGENT}/close",
        "main input[name=acknowledge]",
        runner(snapshot_frame),
        title="End this agent: it is still working",
    ),
    State("workspace-new", "/workspaces/new", "main form", runner(snapshot_frame)),
    State(
        "workspace-rename",
        f"/workspaces/{QUIET_WORKSPACE}/rename",
        "main form",
        runner(snapshot_frame),
    ),
    State(
        "workspace-close",
        "/workspaces/w1/close",
        "main .cards",
        runner(snapshot_frame),
        title="Close a workspace: the cost, by name",
    ),
    State(
        "workspace-close-armed",
        "/workspaces/w1/close",
        "main input[name=armed]",
        runner(snapshot_frame),
        title="Close a workspace: armed",
        armed="main form button",
        act=_click("main form button"),
    ),
    State(
        "workspace-close-moved",
        "/workspaces/w1/close",
        "main [role=alert]",
        runner(snapshot_frame),
        title="Close a workspace: it moved under you",
        armed="main form button",
        act=(
            "const f = document.querySelector('main form');"
            "for (const [n, v] of [['armed', '1'], ['saw', '9']]) {"
            "  const i = document.createElement('input');"
            "  i.type = 'hidden'; i.name = n; i.value = v; f.appendChild(i); }"
            "f.submit();"
        ),
    ),
    State(
        "workspace-close-empty",
        f"/workspaces/{LONELY_WORKSPACE}/close",
        "main form",
        runner(_lonely_gone),
        title="Close an empty workspace: one tap",
    ),
)


def _in_repo(rig: Rig) -> None:
    """The working agent stands in a fixture repository with a change in flight."""
    repo = make_repo(rig.settings.state_dir.parent / "demo-service")
    frame = rig.wire.replies[SNAPSHOT]
    assert isinstance(frame, dict)
    stand_in(frame, repo)


def _files(name: str, query: str, ready: str, title: str) -> State:
    path = f"{AGENT}/files" + (f"?{query}" if query else "")
    return State(name, path, ready, runner(snapshot_frame), prime=_in_repo, title=title)


FILE_REFS: Final = (
    "The 404 branch now logs before it answers: see `src/app/server.py:34`, and the port "
    "moved to 8088 at src/app/server.py:9. The operator notes are in docs/guide.md:3."
)

FILES: Final = (
    State(
        "files-roots",
        "/files",
        "main .cards .card",
        runner(snapshot_frame),
        prime=_in_repo,
        title="Files: every repository an agent is working in",
    ),
    _files("files-changes", "", ".files .flist .frow", "Files: what changed, first"),
    State(
        "files-none",
        f"{AGENT}/files",
        ".files .empty",
        runner(snapshot_frame),
        title="Files tab: an agent with no git repository keeps the tab and says why it is empty",
    ),
    _files(
        "files-diff",
        "to=diff&path=src/app/server.py",
        ".src--diff .src__code--add",
        "Files: one file's change against the last commit",
    ),
    _files("files-tree", "to=tree", ".files .frow--dir", "Files: the tree at the top"),
    _files("files-tree-folder", "path=src/app", ".files .flist .frow", "Files: one folder"),
    _files("files-find", "to=find&q=server", "#find-results .frow", "Files: go to file"),
    _files(
        "files-code",
        "path=src/app/server.py&line=34",
        ".src [data-hp-at]",
        "Files: highlighted Python, opened at a line",
    ),
    State(
        "files-code-columns",
        f"{AGENT}/files?path=src/app/server.py&line=34",
        ".is-columns .src [data-hp-at]",
        runner(snapshot_frame),
        prime=_in_repo,
        title="Files: code with Keep the columns on, unwrapped and scrolling sideways",
        armed=".src [data-hp-at]",
        act=_click("[data-hp-columns]"),
    ),
    _files("files-markdown", "path=docs/guide.md", ".filemd table", "Files: rendered markdown"),
    _files(
        "files-paged",
        "path=logs/big.log&line=1200",
        ".pager .pager__go",
        "Files: a large file, paged plain text",
    ),
    _files(
        "files-minified",
        "path=dist/bundle.min.js",
        ".src .src__cut",
        "Files: a minified line, cut",
    ),
    _files("files-secret", "path=config/.env", ".banner--held", "Files: the secret-file gate"),
    _files("files-image", "path=assets/logo.png", ".fileimg", "Files: an image"),
    State(
        "chat-file-link",
        f"{AGENT}?tab=chat",
        ".chat a.file-ref",
        runner(snapshot_frame),
        [assistant_says(FILE_REFS)],
        prime=_in_repo,
        title="Chat: a path and line the agent wrote, linked into its files",
    ),
)

STATES: Final = (*FEED, *AGENT_PAGE, *TAPPED, *ANSWERED, *LIFECYCLE, *FILES)

# The screens the geometry gate has always held, which every other entry is in addition to.
GEOMETRY_CORE: Final = (
    "feed-empty",
    "feed-herd",
    "agent-chat",
    "agent-pane",
    "unreachable",
    "agent-unreachable",
    "workspaces",
)

# Screens this app draws that the old app had no name for.
V2_ONLY: Final = (
    "feed-herd",
    "feed-build-scrollback",
    "workspaces",
    "alerts",
    "agent-unreachable",
    "agent-chat-composing",
    "chat-long",
    "chat-tools",
    "chat-markdown",
    "chat-blocked",
    "pane-long",
    *(s.name for s in FILES),
)

_RIG: Final = "the rig can answer a key, but no state stages the runner into this outcome yet"
_SCREEN: Final = "needs the old catalogue's synthetic screen ported in as a fixture"

# Every old state with no screen here yet, in ONE place, with why.
NOT_YET: Final[dict[str, str]] = {
    "feed-seen": "no v2 feature yet: there is no seen ledger, so done never demotes",
    "boot": "the shell's first paint lasts until the first poll; the rig cannot hold it there",
    "chat-edit-diff": "no v2 feature yet: chat does not render an Edit's diff",
    "chat-diff-large": "no v2 feature yet: chat does not render an Edit's diff",
    "usage-open": "no v2 feature yet: no usage panel",
    "usage-pushed": "no v2 feature yet: no usage panel",
    "usage-opencode": "no v2 feature yet: no usage panel",
    "usage-unpushed": "no v2 feature yet: no usage panel",
    "chat-may-have-moved": "no v2 feature yet: chat has no moved-transcript note",
    "agent-answer-waiting": "no v2 feature yet: the answer region does not collapse",
    "agent-answer-unearned": _SCREEN,
    "agent-answer-readout": _SCREEN,
    "agent-unknown-shape": _SCREEN,
    "agent-answer-as-question": _SCREEN,
    "agent-answer-accepted": _RIG,
    "agent-answer-unread": _RIG,
    "agent-answer-partially-applied": _RIG,
    "starting-prompt-sent": _RIG,
    "agent-unverified": _RIG,
    "agent-no-echo": _RIG,
    "agent-no-echo-refused": _RIG,
    "agent-at-the-desk": "no v2 feature yet: nothing knows which pane the desk has in front",
    "inbox": "no v2 feature yet: no artifacts inbox",
    "inbox-undo": "no v2 feature yet: no artifacts inbox",
    "inbox-empty": "no v2 feature yet: no artifacts inbox",
    "artifact-html": "no v2 feature yet: no artifact reader",
    "artifact-md": "no v2 feature yet: no artifact reader",
    "artifact-gone": "no v2 feature yet: no artifact reader",
    "feed-artifacts-unread": "no v2 feature yet: no artifacts door on the feed",
    "feed-artifacts-read": "no v2 feature yet: no artifacts door on the feed",
    "feed-artifacts-only": "no v2 feature yet: no artifacts door on the feed",
    "settings-sheet": "no v2 feature yet: no settings sheet; the alerts page is the nearest",
    "settings-light": "no v2 feature yet: one colourway only",
    "settings-contrast": "no v2 feature yet: one colourway only",
    "settings-dnd": "no v2 feature yet: no do-not-disturb sheet",
    "pane-light": "no v2 feature yet: one colourway only",
    "pane-contrast": "no v2 feature yet: one colourway only",
}

# The old app's state keys, as its preview script listed them, pinned so drift on either side shows.
LEGACY_KEYS: Final = (
    "feed-blocked",
    "feed-question",
    "feed-permission",
    "feed-untitled",
    "feed-opencode",
    "feed-seen",
    "feed-filtered",
    "feed-quiet",
    "feed-empty",
    "feed-degraded",
    "feed-unreadable-question",
    "feed-stale",
    "feed-offline",
    "unreachable",
    "refused",
    "boot",
    "offline-page",
    "agent-chat",
    "chat-edit-diff",
    "chat-diff-large",
    "usage-open",
    "usage-pushed",
    "usage-opencode",
    "usage-unpushed",
    "chat-older",
    "chat-absent",
    "chat-may-have-moved",
    "agent-pane",
    "agent-pane-chrome",
    "agent-pane-columns",
    "agent-refused",
    "agent-answer-waiting",
    "agent-answer",
    "agent-answer-plan",
    "agent-answer-menu",
    "agent-answer-multiselect",
    "agent-answer-checklist",
    "agent-answer-wizard-step",
    "agent-answer-unearned",
    "agent-answer-readout",
    "agent-options-refused",
    "agent-options-refused-permission",
    "agent-unknown-shape",
    "agent-unreadable",
    "agent-answer-as-question",
    "agent-answer-confirmed",
    "agent-answer-confirmed-moved",
    "agent-answer-unconfirmed",
    "agent-answer-unconfirmed-moved",
    "agent-answer-accepted",
    "agent-answer-unread",
    "agent-answer-partially-applied",
    "agent-interrupt",
    "agent-at-the-desk",
    "agent-key-refused",
    "new-agent",
    "new-agent-refused",
    "agent-starting",
    "agent-first-block",
    "first-block-armed",
    "agent-starting-opencode",
    "starting-prompt-sent",
    "close-confirm",
    "close-working",
    "workspace-new",
    "workspace-rename",
    "workspace-close",
    "workspace-close-armed",
    "workspace-close-moved",
    "workspace-close-empty",
    "destructive-confirm",
    "agent-unverified",
    "agent-no-echo",
    "agent-no-echo-refused",
    "inbox",
    "inbox-undo",
    "inbox-empty",
    "artifact-html",
    "artifact-md",
    "artifact-gone",
    "feed-artifacts-unread",
    "feed-artifacts-read",
    "feed-artifacts-only",
    "settings-sheet",
    "settings-light",
    "settings-contrast",
    "settings-dnd",
    "pane-light",
    "pane-contrast",
)


def ported() -> list[str]:
    """The old keys that have a screen here, in catalogue order."""
    return [s.name for s in STATES if s.name in LEGACY_KEYS]


def pick(names: list[str]) -> list[State]:
    """The named states in catalogue order, or every state for no names; unknown names raise."""
    known = {s.name for s in STATES}
    unknown = [n for n in names if n not in known]
    if unknown:
        raise KeyError(f"no such state: {', '.join(unknown)}")
    return [s for s in STATES if not names or s.name in names]


LOGIN: Final = "catalogue@example.com"


def build(state: State, home: Path) -> Rig:
    """A fresh app for ``state`` under ``home``, primed, for a browser that sends no identity."""
    if state.transcript:
        write_transcript(home / "transcripts", WORKING_SESSION, state.transcript)
    login: dict[str, object] = {"assume_login": LOGIN} if state.login else {}
    rig = build_rig(home, state.replies(), headers={}, paced=True, **login, **state.settings)
    # An assumed login is honoured only on a loopback name, which is what the browser uses.
    rig.http.base_url = "http://127.0.0.1"
    if state.prime is not None:
        state.prime(rig)
    return rig
