"""Every named screen the app can draw, each over its own fake runner, for gates and shots alike."""

import copy
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

from pydantic import JsonValue, TypeAdapter

from herdr_pwa.engine import EngineUnavailable
from herdr_pwa.panetext import as_text_read
from tests._jsonpath import obj
from tests.filefakes import make_repo, stand_in
from tests.test_opencode import MIXED_ROWS
from tests.test_opencode import build as opencode_db
from tests.webfakes import (
    AGENT_GET,
    FIXTURES,
    IDLE_SESSION,
    PROMPT,
    READ,
    SEND_KEYS,
    SNAPSHOT,
    WORKING_PANE,
    WORKING_SESSION,
    FakeWire,
    Hold,
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
    f"/home/user/projects/{UNBROKEN}/src/very/deep/module/path.py to main, which "
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
    "\x1b[2m\x1b[38;2;153;153;153m | \x1b[0m\x1b[38;5;3m12m\x1b[0m\n"
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
    transcript: list[dict[str, JsonValue]] = field(default_factory=list[dict[str, JsonValue]])
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
    # Proved while its tap is still out, so htmx never settles: wait for ``ready`` alone.
    in_flight: bool = False


def capture(name: str) -> str:
    """One captured terminal screen from the fixtures."""
    return (FIXTURES / name / "detection.txt").read_text()


def rig_screen(name: str) -> str:
    """One of the screens written for the rig, in colour."""
    return (FIXTURES / "rig" / name).read_text()


PERMISSION: Final = "capture-blocked-permission-prompt"
PERMISSION_WRAPPED: Final = "capture-blocked-permission-prompt-wrapped"
MENU: Final = "capture-blocked-askuserquestion-menu"
MENU_WRAPPED: Final = "capture-blocked-askuserquestion-wrapped"
CHECKLIST: Final = "capture-blocked-multiselect-checklist"
PLAN: Final = "capture-blocked-plan-approval"
OC_PERMISSION: Final = "capture-blocked-opencode-permission"
OC_QUESTION: Final = "capture-blocked-opencode-question"
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


def _snapshot(frame: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return obj(frame, "result", "snapshot")


def _set(frame: dict[str, JsonValue], pane: str, **fields: str) -> dict[str, JsonValue]:
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


def herd(**statuses: str) -> dict[str, JsonValue]:
    """The recorded snapshot with chosen panes moved to a status, keyed ``p<index>``."""
    frame = snapshot_frame()
    for key, status in statuses.items():
        _set(frame, PANES[int(key[1:])], agent_status=status)
    return frame


def _stressed() -> dict[str, JsonValue]:
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


def _empty() -> dict[str, JsonValue]:
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


def _chat() -> list[dict[str, JsonValue]]:
    tool_call: dict[str, JsonValue] = {
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
    tool_result: dict[str, JsonValue] = {
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


def rich_chat() -> list[dict[str, JsonValue]]:
    """One agent turn using every kind of prose the Chat tab renders."""
    return [human_says("what changed?", uuid="u0"), assistant_says(RICH_PROSE, uuid="a1")]


def long_chat(turns: int = 24) -> list[dict[str, JsonValue]]:
    """A back-and-forth several screens tall, numbered so the newest turn is easy to find."""
    records: list[dict[str, JsonValue]] = []
    for n in range(turns):
        if n % 2:
            text = f"Reply {n}: checked the cluster and the rollback.\n\n" + "- a step\n" * 3
            records.append(assistant_says(text, uuid=f"a{n}"))
        else:
            records.append(human_says(f"Prompt {n}: what does the migration touch?", uuid=f"u{n}"))
    return records


# Real records Claude Code wrote into an orchestrator's log, scrubbed of paths, mail and hosts.
NOTICES: Final = FIXTURES / "transcript" / "notices" / "notices.jsonl"


# A fixture record as Claude Code wrote it, every field kept, for a test to edit.
RECORD: Final = TypeAdapter(dict[str, JsonValue])


# Real API-error records (every shape found locally) set among the notice records in the order
# a full context was measured to refuse them: each retry set off by a notice or a peer message.
API_ERRORS: Final = FIXTURES / "transcript" / "notices" / "api-errors.jsonl"


def _records(path: Path) -> list[dict[str, JsonValue]]:
    found = (RECORD.validate_json(line) for line in path.read_text().splitlines())
    # Its cwd is another machine's repo; kept, it would read as a session started elsewhere.
    return [{k: v for k, v in record.items() if k != "cwd"} for record in found]


# Real slash-command records, every shape found locally, each with the caveat before it and the
# output after it, in the order Claude Code writes them.
SLASH_COMMANDS: Final = FIXTURES / "transcript" / "notices" / "slash-commands.jsonl"
# Real /compact runs: the line as sent, the boundary, the summary, the command and what it printed.
COMPACTION: Final = FIXTURES / "transcript" / "notices" / "compaction.jsonl"
# Real slash commands Claude Code wrote as `type: system` records rather than user ones.
SYSTEM_COMMANDS: Final = FIXTURES / "transcript" / "notices" / "system-commands.jsonl"
# Built, not captured: no local log has a failing command, so its stderr takes the stdout shape.
COMMAND_STDERR: Final = FIXTURES / "transcript" / "notices" / "command-stderr.jsonl"
# Built on the measured shapes: prompts the person queued while a turn ran, taken or waiting.
QUEUED: Final = FIXTURES / "transcript" / "notices" / "queued.jsonl"
# The same words sent three times, typed, queued and taken, and queued and waiting.
QUEUED_REPEAT: Final = FIXTURES / "transcript" / "notices" / "queued-repeat.jsonl"
# Built on the measured shapes: `!` commands the person ran, each followed by what it printed.
SHELL_COMMANDS: Final = FIXTURES / "transcript" / "notices" / "shell-commands.jsonl"


def notice_records() -> list[dict[str, JsonValue]]:
    """The scrubbed records, in the order they were written."""
    return _records(NOTICES)


def api_error_records() -> list[dict[str, JsonValue]]:
    """The scrubbed API-error sequence, in fixture order."""
    return _records(API_ERRORS)


def slash_command_records() -> list[dict[str, JsonValue]]:
    """The scrubbed slash-command records, in fixture order."""
    return _records(SLASH_COMMANDS)


def compaction_records() -> list[dict[str, JsonValue]]:
    """The scrubbed /compact runs, in fixture order."""
    return _records(COMPACTION)


def system_command_records() -> list[dict[str, JsonValue]]:
    """The scrubbed slash commands written as system records, in fixture order."""
    return _records(SYSTEM_COMMANDS)


def command_stderr_records() -> list[dict[str, JsonValue]]:
    """Two failing slash commands, a user record's and a system record's, in fixture order."""
    return _records(COMMAND_STDERR)


def queued_records() -> list[dict[str, JsonValue]]:
    """Prompts queued mid-turn with the queue's own records around them, in fixture order."""
    return _records(QUEUED)


def queued_repeat_records() -> list[dict[str, JsonValue]]:
    """One prompt's words sent three times, two of them through the queue, in fixture order."""
    return _records(QUEUED_REPEAT)


def shell_command_records() -> list[dict[str, JsonValue]]:
    """Three `!` commands between two prompts, in fixture order."""
    return _records(SHELL_COMMANDS)


def origin_of(record: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """A record's ``origin``, empty when it has none."""
    origin = record.get("origin")
    return origin if isinstance(origin, dict) else {}


def _origin_kind(record: dict[str, JsonValue]) -> str:
    kind = origin_of(record).get("kind")
    return kind if isinstance(kind, str) else ""


def notices_of(kind: str, *, report: bool | None = None) -> list[dict[str, JsonValue]]:
    """The fixture's records of one ``origin.kind``, peers narrowed to hand-backs or not."""
    found = [r for r in notice_records() if _origin_kind(r) == kind]
    if report is None:
        return found
    return [r for r in found if (origin_of(r).get("handback") is True) == report]


def failed_notice(status: str = "failed", uuid: str = "failed-notice") -> dict[str, JsonValue]:
    """A real task notification with its status rewritten, since every one recorded completed."""
    record = copy.deepcopy(notices_of("task-notification")[0])
    message = obj(record, "message")
    text = str(message["content"]).replace(
        "<status>completed</status>", f"<status>{status}</status>"
    )
    message["content"] = text.replace(" finished</summary>", f" {status}</summary>")
    record["uuid"] = uuid
    return record


def notice_record(uuid: str) -> dict[str, JsonValue]:
    """The fixture's record whose uuid starts with *uuid*."""
    return next(r for r in notice_records() if str(r.get("uuid", "")).startswith(uuid))


# The fixture's background commands, the notice whose task reported on both sides of it, and
# the notice whose task had not reported yet (its report is left out of the fixture).
COMMAND_EXIT_1: Final = "c023cd52"
COMMAND_EXIT_0: Final = "21e18294"
REPORTED: Final = "117a9f64"
REPORTED_BEFORE: Final = "b075cfee"
REPORTED_AFTER: Final = "ed3ba510"
UNREPORTED: Final = "1a962ca2"


def failed_command() -> dict[str, JsonValue]:
    """A real background command with the ending a failed one is recorded with."""
    record = copy.deepcopy(notice_record(COMMAND_EXIT_1))
    message = obj(record, "message")
    text = str(message["content"]).replace("<status>completed</status>", "<status>failed</status>")
    ending = " completed (exit code 1: No matches found)</summary>"
    message["content"] = text.replace(ending, " failed with exit code 1</summary>")
    record["uuid"] = "failed-command"
    return record


# A task whose report has no table: a table's hyphenated word breaks in a cell at 200% type, in a
# card or a notice alike, which is the markdown table's to fix, not this screen's.
SURVEYED: Final = "a8d1211107eec7f53"


def notice_report_chat() -> list[dict[str, JsonValue]]:
    """A prompt, an agent's notice and its report, one yet to report, and two commands."""
    task = [r for r in notice_records() if SURVEYED in json.dumps(r)]
    return [
        notices_of("human")[1],
        *task,
        notice_record(UNREPORTED),
        notice_record(COMMAND_EXIT_1),
        failed_command(),
    ]


def task_notice_chat() -> list[dict[str, JsonValue]]:
    """A prompt, the three tasks it started finishing, and the agent's answer."""
    humans = notices_of("human")
    # The answer without its usage: five days of turn time on the strip is the log's age, not news.
    answer = "Three surveys are back; the audit says lift the engine and slim it."
    tasks = notices_of("task-notification")[:3]
    return [humans[0], *tasks, assistant_says(answer, uuid="a-tasks")]


def failed_notice_chat() -> list[dict[str, JsonValue]]:
    """A prompt, then one task that completed, one that failed and one that was killed."""
    done = notices_of("task-notification")[0]
    return [notices_of("human")[0], done, failed_notice(), failed_notice("killed", "killed-notice")]


def report_chat() -> list[dict[str, JsonValue]]:
    """A prompt and a long hand-back report a subagent sent in answer, a bulleted list in it."""
    reports = notices_of("peer", report=True)
    return [
        notices_of("human")[1],
        next(r for r in reports if "The scroll-anchor fix is merged" in json.dumps(r)),
    ]


def report_table_chat() -> list[dict[str, JsonValue]]:
    """A prompt and a hand-back report whose table cells hold shas, paths and ticket refs."""
    reports = notices_of("peer", report=True)
    return [
        notices_of("human")[1],
        next(r for r in reports if "f00d123/manifest-0101" in json.dumps(r)),
    ]


def peer_message_chat() -> list[dict[str, JsonValue]]:
    """A prompt, then a message another session sent in."""
    others = [r for r in notices_of("peer", report=False) if "cross-session" in json.dumps(r)]
    return [notices_of("human")[1], *others]


def _tool_record(kind: str, uuid: str, content: JsonValue) -> dict[str, JsonValue]:
    message: dict[str, JsonValue] = {"role": kind, "content": content}
    return {"type": kind, "uuid": uuid, "timestamp": "2026-10-10T10:00:00.000Z", "message": message}


def tool_heavy_chat(messages: int = 60, calls: int = 6) -> list[dict[str, JsonValue]]:
    """An agent at work: every message followed by a burst of tool calls and their results."""
    records: list[dict[str, JsonValue]] = []
    n = 0
    for m in range(messages):
        if m % 5 == 0:
            records.append(human_says(f"Prompt {m}: keep going on the migration.", uuid=f"h{m}"))
        else:
            text = f"Message {m}: the step landed; running the next check."
            records.append(assistant_says(text, uuid=f"a{m}"))
        for _ in range(calls):
            n += 1
            use: dict[str, JsonValue] = {
                "type": "tool_use",
                "id": f"tu{n}",
                "name": "Bash",
                "input": {"command": f"pytest -q tests/test_step_{n}.py"},
            }
            result: dict[str, JsonValue] = {
                "type": "tool_result",
                "tool_use_id": f"tu{n}",
                "content": f"{n} passed in 0.{n % 10}s",
            }
            records += [
                _tool_record("assistant", f"c{n}", [use]),
                _tool_record("user", f"r{n}", [result]),
            ]
    return records


# The stats strip's states write their stamps a few minutes before the shot, so a running turn
# reads as one, not as the day since the fixtures were recorded.
def _ago(minutes: float) -> str:
    moment = datetime.now(UTC) - timedelta(minutes=minutes)
    return moment.isoformat().replace("+00:00", "Z")


def _spent(
    record: dict[str, JsonValue], message: str, *, prompt: int, out: int
) -> dict[str, JsonValue]:
    """``record`` as an answer that carries the usage Claude Code writes beside it."""
    body = obj(record, "message")
    body.update(
        id=message,
        model="claude-haiku-4-5-20251001",
        usage={
            "input_tokens": 12,
            "cache_creation_input_tokens": 2_400,
            "cache_read_input_tokens": prompt - 2_412,
            "output_tokens": out,
        },
    )
    record["timestamp"] = _ago(1)
    return record


# One turn of work, its stamps minutes before the shot: a prompt, prose, three tool calls, and
# then the stage it is at: a tool still running, thinking after the last result, gone quiet,
# asking for approval, or done.
TURN_STAGES: Final = ("tool", "thinking", "quiet", "asking", "done")
# How long the "done" turn ran, prompt to answer: 4m 12s.
TURN_WORKED_SECONDS: Final = 252


def _at(
    record: dict[str, JsonValue], seconds_ago: float, message: str = ""
) -> dict[str, JsonValue]:
    """``record`` stamped ``seconds_ago``; an answer also carries the usage every answer does."""
    record["timestamp"] = _ago(seconds_ago / 60)
    if message:
        body = obj(record, "message")
        body.update(id=message, model="claude-haiku-4-5-20251001", usage={"output_tokens": 40})
    return record


def _call(
    n: int, name: str, args: dict[str, JsonValue], seconds_ago: float
) -> dict[str, JsonValue]:
    use: dict[str, JsonValue] = {"type": "tool_use", "id": f"st{n}", "name": name, "input": args}
    return _at(_tool_record("assistant", f"sc{n}", [use]), seconds_ago, f"sm{n}")


def _result(n: int, text: str, seconds_ago: float) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {"type": "tool_result", "tool_use_id": f"st{n}", "content": text}
    return _at(_tool_record("user", f"sr{n}", [result]), seconds_ago)


def turn_chat(stage: str) -> list[dict[str, JsonValue]]:
    """A turn at ``stage`` (one of ``TURN_STAGES``), stamped against the wall clock now."""
    # Quiet is the thinking stage left alone for three minutes.
    late = 180.0 if stage == "quiet" else 0.0
    start = late + (TURN_WORKED_SECONDS if stage == "done" else 150.0)
    records = [
        _at(human_says("Run the chat tests and fix whatever fails.", uuid="sh"), start),
        _at(assistant_says("Reading the transcript adapter first.", uuid="sa1"), start - 5, "sm0"),
        _call(1, "Read", {"file_path": "/repo/src/herdr_pwa/engine/transcript.py"}, start - 8),
        _result(1, "1800 lines", start - 9),
        _call(2, "Edit", {"file_path": "/repo/src/herdr_pwa/web/chat.py"}, start - 40),
        _result(2, "The file was updated.", start - 41),
        _call(3, "Bash", {"command": "uv run pytest -q tests/test_chat_fold.py"}, late + 20),
    ]
    if stage in ("thinking", "quiet", "done"):
        records.append(_result(3, "12 passed in 0.4s", late + 15))
    if stage == "done":
        records.append(_at(assistant_says("All twelve pass now.", uuid="sa2"), 0, "sm9"))
    return records


# Three workers dispatched in the background ten minutes ago; one has finished, and the turn's own
# log has been still for three minutes since, as an orchestrator's is while it waits on them.
SPAWNED_WORKERS: Final = (
    ("a1", "Name the prompt group"),
    ("a2", "Remove the foreign capture corpus, every clone and every reader of it"),
    ("a3", "Show what spawned agents are doing"),
)


def _launch(n: int, agent: str, description: str, seconds_ago: float) -> list[dict[str, JsonValue]]:
    """An Agent call started in the background, and the result that names its task id."""
    args: dict[str, JsonValue] = {
        "description": description,
        "subagent_type": "general-purpose",
        "run_in_background": True,
        "prompt": "Your ticket.",
    }
    result = _result(n, "Async agent launched successfully.", seconds_ago)
    result["toolUseResult"] = {
        "isAsync": True,
        "status": "async_launched",
        "agentId": agent,
        "description": description,
    }
    return [_call(n, "Agent", args, seconds_ago), result]


def _finished(agent: str, description: str, seconds_ago: float) -> dict[str, JsonValue]:
    """A background agent's notice as the harness delivers it mid-turn: an attachment only."""
    prompt = (
        f"<task-notification>\n<task-id>{agent}</task-id>\n<status>completed</status>\n"
        f'<summary>Agent "{description}" finished</summary>\n</task-notification>'
    )
    attachment: dict[str, JsonValue] = {
        "type": "queued_command",
        "prompt": prompt,
        "commandMode": "task-notification",
        "origin": {"kind": "task-notification"},
    }
    record: dict[str, JsonValue] = {"type": "attachment", "uuid": f"sf-{agent}"}
    record["attachment"] = attachment
    return _at(record, seconds_ago)


def spawned_chat() -> list[dict[str, JsonValue]]:
    """An orchestrator's turn waiting on the workers it started (``SPAWNED_WORKERS``)."""
    records = [_at(human_says("Dispatch the three tickets.", uuid="sh"), 600)]
    for n, (agent, description) in enumerate(SPAWNED_WORKERS, start=1):
        records.extend(_launch(n, agent, description, 590 - n))
    first, said = SPAWNED_WORKERS[0]
    records.append(_finished(first, said, 240))
    records.append(
        _at(assistant_says("One is done; waiting on the other two.", uuid="sa"), 180, "sm9")
    )
    return records


def _spawned_at(rig: Rig) -> None:
    write_transcript(rig.settings.state_dir.parent / "transcripts", WORKING_SESSION, spawned_chat())


# What the person typed while the orchestrator waited on its workers, still in the queue.
CROWDED_QUEUED: Final = "When the workers are done, merge the first branch and run the gate."


def crowded_chat() -> list[dict[str, JsonValue]]:
    """The orchestrator waiting on two running workers, with one prompt queued behind them."""
    queued: dict[str, JsonValue] = {"type": "queue-operation", "operation": "enqueue"}
    queued["content"] = CROWDED_QUEUED
    return [*spawned_chat(), _at(queued, 60)]


def _crowded_at(rig: Rig) -> None:
    write_transcript(rig.settings.state_dir.parent / "transcripts", WORKING_SESSION, crowded_chat())


# What an idle agent left running: a dev server, a watcher, a long build, as their calls name them.
BACKGROUND_SHELLS: Final = (
    ("Serve the docs on port 4000", "npm run docs -- --port 4000"),
    ("", "uv run python scripts/shots.py --watch --states chat-idle-shells chat-idle-both"),
    ("Build the release bundle", "npm run build"),
)


def _shell(
    n: int, description: str, command: str, seconds_ago: float
) -> list[dict[str, JsonValue]]:
    """A Bash call sent to the background, and the result that names its task id."""
    args: dict[str, JsonValue] = {"command": command, "run_in_background": True}
    if description:
        args["description"] = description
    result = _result(n, f"Command running in background with ID: bg{n}.", seconds_ago)
    result["toolUseResult"] = {"stdout": "", "stderr": "", "backgroundTaskId": f"bg{n}"}
    return [_call(n, "Bash", args, seconds_ago + 1), result]


# A dev server an earlier turn left running, which no later turn waits on.
OLD_SERVER: Final = ("Serve the site on port 8000", "python -m http.server 8000")


@dataclass(frozen=True)
class LeftRunning:
    """What an agent left running: shells and agents its last turn started, servers an older one did."""

    shells: int = 0
    agents: int = 0
    old: int = 0


def background_chat(left: LeftRunning) -> list[dict[str, JsonValue]]:
    """A finished turn that left ``left``'s shells and agents running behind it, after an earlier
    turn that left its old servers running."""
    shells, agents, old = left.shells, left.agents, left.old
    records: list[dict[str, JsonValue]] = []
    if old:
        records.append(_at(human_says("Start the site server.", uuid="sh0"), 3600))
        for n in range(old):
            records.extend(_shell(20 + n, *OLD_SERVER, 3500 - 10 * n))
    records.append(_at(human_says("Start the docs server and the watchers.", uuid="sh"), 900))
    for n, (description, command) in enumerate(BACKGROUND_SHELLS[:shells], start=1):
        records.extend(_shell(n, description, command, 880 - 60 * n))
    for n, (agent, description) in enumerate(SPAWNED_WORKERS[:agents], start=10):
        records.extend(_launch(n, agent, description, 700 - n))
    records.append(_at(assistant_says("All started; they run on.", uuid="sa"), 600, "sm9"))
    return records


def _background_at(left: LeftRunning, *, server: bool = False) -> Prime:
    """The working pane's agent left work running; *server*: an idle one left only an old server."""

    def prime(rig: Rig) -> None:
        roots = rig.settings.state_dir.parent / "transcripts"
        write_transcript(roots, WORKING_SESSION, background_chat(left))
        if server:
            write_transcript(roots, IDLE_SESSION, background_chat(LeftRunning(old=1)))

    return prime


def _turn_at(stage: str) -> Prime:
    """Write the turn as the state is built, so its stamps are seconds old when shot."""

    def prime(rig: Rig) -> None:
        roots = rig.settings.state_dir.parent / "transcripts"
        write_transcript(roots, WORKING_SESSION, turn_chat(stage))

    return prime


def _working_as(status: str) -> Callable[[], dict[str, JsonValue]]:
    return lambda: _set(snapshot_frame(), WORKING_PANE, agent_status=status)


# A turn two days and three hours old: an orchestrating session whose newest typed prompt is days
# behind it, which is how the strip met a day-scale turn.
LONG_TURN_MINUTES: Final = (2 * 24 + 3) * 60


# Claude Code on Bedrock records the provider's model id, which no label shortens.
BEDROCK_MODEL: Final = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"


def usage_chat(turn_minutes: float = 4, model: str = "") -> list[dict[str, JsonValue]]:
    """The chat fixture, its prompt sent ``turn_minutes`` ago and its answers carrying usage."""
    records = _chat()
    records[0]["timestamp"] = _ago(turn_minutes)
    for record in (
        _spent(records[1], "m1", prompt=118_000, out=2_100),
        _spent(records[3], "m2", prompt=124_000, out=36_000),
    ):
        if model:
            obj(record, "message")["model"] = model
    return records


def _usage_transcript(rig: Rig) -> None:
    """Write the usage chat as the state is built, so its turn is four minutes old when shot."""
    write_transcript(rig.settings.state_dir.parent / "transcripts", WORKING_SESSION, usage_chat())


def _long_turn_transcript(rig: Rig) -> None:
    """The usage chat with its turn running for days, as an orchestrating session's does."""
    write_transcript(
        rig.settings.state_dir.parent / "transcripts",
        WORKING_SESSION,
        usage_chat(LONG_TURN_MINUTES),
    )


def _crowded_transcript(rig: Rig) -> None:
    """A days-long turn on a model id too long to share a narrow strip with every other fact."""
    write_transcript(
        rig.settings.state_dir.parent / "transcripts",
        WORKING_SESSION,
        usage_chat(LONG_TURN_MINUTES, BEDROCK_MODEL),
    )


OPENCODE_STORE: Final = FIXTURES / "transcript" / "opencode"
OPENCODE_SESSION: Final = "ses_3fd62ba5bffemIxWVhRda5wgS7"
OPENCODE_DIRECTORY: Final = "/home/user/projects/herdr-pwa"


def _opencode_working() -> dict[str, JsonValue]:
    """The working pane as an opencode agent, holding the captured opencode session."""
    frame = snapshot_frame()
    agent = next(a for a in agents_of(frame) if a.get("pane_id") == WORKING_PANE)
    agent["agent"] = "opencode"
    agent["agent_session"] = {
        "agent": "opencode",
        "kind": "id",
        "source": "herdr:opencode",
        "value": OPENCODE_SESSION,
    }
    # Where the captured session was started; anywhere else is a pane whose identity is doubted.
    agent["cwd"] = agent["foreground_cwd"] = OPENCODE_DIRECTORY
    return frame


def _opencode_elsewhere() -> dict[str, JsonValue]:
    """The opencode pane reporting a session its store says was started in another directory."""
    frame = _opencode_working()
    agent = next(a for a in agents_of(frame) if a.get("pane_id") == WORKING_PANE)
    agent["cwd"] = agent["foreground_cwd"] = "/home/user/projects/notes-app"
    return frame


def _opencode_store(rig: Rig) -> None:
    """The captured opencode store under the rig's transcript root, its first prompt just sent."""
    root = rig.settings.state_dir.parent / "transcripts"
    asked = int((datetime.now(UTC) - timedelta(minutes=6)).timestamp() * 1000)
    for source in OPENCODE_STORE.rglob("*.json"):
        target = root / source.relative_to(OPENCODE_STORE)
        target.parent.mkdir(parents=True, exist_ok=True)
        record = RECORD.validate_json(source.read_text())
        if record.get("role") == "user":
            record["time"] = {"created": asked}
        target.write_text(json.dumps(record))


MIXED_SESSION: Final = "ses_0dfb24cf5ffemJcBmqyE29sVBi"


def _opencode_mixed() -> dict[str, JsonValue]:
    """The working pane as an opencode agent whose session switched models twice."""
    frame = _opencode_working()
    agent = next(a for a in agents_of(frame) if a.get("pane_id") == WORKING_PANE)
    agent_session = agent["agent_session"]
    assert isinstance(agent_session, dict)
    agent_session["value"] = MIXED_SESSION
    agent["cwd"] = agent["foreground_cwd"] = "/home/user/project"
    return frame


def _recent(row: dict[str, JsonValue], shift: int) -> dict[str, JsonValue]:
    """One message row moved *shift* milliseconds later, in its column and in its record."""
    record = RECORD.validate_json(str(row["data"]))
    times = record.get("time")
    if isinstance(times, dict):
        record["time"] = {k: v + shift if isinstance(v, int) else v for k, v in times.items()}
    stamp = row["time_created"]
    moved = stamp + shift if isinstance(stamp, int) else stamp
    return {**row, "data": json.dumps(record), "time_created": moved}


def _opencode_mixed_store(rig: Rig) -> None:
    """Real rows from four models, one of whose provider records nothing, as opencode's SQLite."""
    messages = MIXED_ROWS["message"]
    newest = max(row["time_created"] for row in messages if isinstance(row["time_created"], int))
    # Moved so the newest answer is a minute old, else the running turn reads as months long.
    now = int((datetime.now(UTC) - timedelta(minutes=1)).timestamp() * 1000)
    rows = {**MIXED_ROWS, "message": [_recent(row, now - newest) for row in messages]}
    opencode_db(rig.settings.state_dir.parent / "transcripts" / "opencode.db", rows)


def long_pane(lines: int = 120) -> str:
    """Terminal scrollback several screens tall, numbered so the newest line is easy to find."""
    return "".join(f"\x1b[32m$\x1b[0m step {n:03d}: applied to the cluster\n" for n in range(lines))


def runner(frame: Callable[[], dict[str, JsonValue]], screen: str = "") -> Replies:
    """A runner whose snapshot is ``frame()`` and whose every pane shows ``screen``."""
    return lambda: {SNAPSHOT: frame(), READ: read_frame(screen)}


def per_pane(frame: Callable[[], dict[str, JsonValue]], screens: dict[str, str]) -> Replies:
    """A runner whose panes each show their own screen, and an idle prompt elsewhere."""

    def replies() -> dict[str, Reply]:
        found: dict[str, Reply] = {SNAPSHOT: frame(), READ: read_frame(capture(IDLE))}
        for pane, screen in screens.items():
            found[f"{READ} {pane}"] = read_frame(screen, pane)
        return found

    return replies


def _unreachable() -> dict[str, Reply]:
    return {SNAPSHOT: EngineUnavailable("catalogue: no runner")}


def _unreadable(frame: Callable[[], dict[str, JsonValue]]) -> Replies:
    return lambda: {SNAPSHOT: frame(), READ: EngineUnavailable("catalogue: the read failed")}


def _outage(after: float, *, failing: bool) -> Prime:
    """Read the herd once, then let ``after`` seconds pass, with the runner gone if ``failing``."""

    def prime(rig: Rig) -> None:
        _ = rig.http.get("/feed")
        if failing:
            rig.wire.replies[SNAPSHOT] = EngineUnavailable("catalogue: the runner went away")
        rig.clock.now += after

    return prime


def _untitled() -> dict[str, JsonValue]:
    """Three agents in one repo whose titles are the shell's prompt string."""
    frame = snapshot_frame()
    shell = "user@host:~/projects"
    for pane, status in zip(PANES[3:], ("working", "idle", "idle"), strict=True):
        _set(frame, pane, title=shell, cwd="/home/user/projects", agent_status=status)
    return frame


def _opencode() -> dict[str, JsonValue]:
    """Four opencode panes that all write the same constant terminal title."""
    frame = snapshot_frame()
    for pane in PANES[:4]:
        _set(frame, pane, agent="opencode", title="OpenCode")
    return frame


def _door(**fields: str) -> Callable[[], dict[str, JsonValue]]:
    """The working pane with no session yet, as a just-started agent has."""
    return lambda: _set(snapshot_frame(), WORKING_PANE, session="", **fields)


def _drawn_in_colour(name: str) -> Callable[[dict[str, JsonValue]], dict[str, JsonValue]]:
    """A capture's pane read: its text for a text read, its colour read for a colour one."""
    text = capture(name)
    ansi = (FIXTURES / name / "visible.ansi").read_text()
    return lambda params: read_frame(ansi if params.get("format") == "ansi" else text)


def _opencode_blocked_on(name: str) -> Replies:
    """The working pane as an opencode agent stopped on one of its captured dialogs."""
    frame = lambda: _set(herd(**BLOCKED), WORKING_PANE, agent="opencode")  # noqa: E731
    return lambda: {SNAPSHOT: frame(), READ: _drawn_in_colour(name)}


def _opencode_listing() -> Replies:
    """An opencode agent whose transcript ends on a numbered list with a coloured row in it."""
    ansi = (FIXTURES / "rig" / "opencode-scrollback-list.ansi").read_text()
    text = as_text_read(ansi)
    frame = lambda: _set(herd(**BLOCKED), WORKING_PANE, agent="opencode")  # noqa: E731
    return lambda: {
        SNAPSHOT: frame(),
        READ: lambda params: read_frame(ansi if params.get("format") == "ansi" else text),
    }


def _two_blocked(first: str, second: str) -> Replies:
    """The working pane and its neighbour blocked, each on its own captured question."""
    frame = lambda: herd(p4="blocked", p3="blocked")  # noqa: E731 -- one call site
    return per_pane(frame, {WORKING_PANE: capture(first), PANES[3]: capture(second)})


def _lonely_gone() -> dict[str, JsonValue]:
    """The snapshot with the one agent of its workspace closed."""
    frame = snapshot_frame()
    snapshot = _snapshot(frame)
    snapshot["agents"] = [a for a in agents_of(frame) if a.get("pane_id") != PANES[1]]
    return frame


def _untick(screen: str) -> str:
    return screen.replace("[✔] autumn", "[ ] autumn")


def _agent_info(frame: dict[str, JsonValue]) -> dict[str, JsonValue]:
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


def _prompted(screen: str) -> Replies:
    """The working pane idle on ``screen``, with a runner that takes a prompt it never shows."""

    def replies() -> dict[str, Reply]:
        frame = herd(p4="idle")
        return {
            SNAPSHOT: frame,
            READ: read_frame(screen),
            AGENT_GET: _agent_info(frame),
            PROMPT: {"id": "x", "result": {"type": "ok"}},
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


def _held_key(rig: Rig) -> None:
    """The runner sits on a tapped key, so the row's "Sending…" line can be seen."""
    rig.wire.after[SEND_KEYS] = Hold()


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
        "feed-context-full",
        "/",
        "#feed-list .card--blocked .card__question",
        runner(lambda: herd(p4="idle")),
        transcript=api_error_records()[:9],
        title="Feed: an agent stopped on a full context, its retries folded, asking for /compact",
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
        "chat-task-notice",
        f"{AGENT}?tab=chat",
        ".chat .turn--notice",
        runner(_stressed),
        task_notice_chat(),
        title="Chat: three background tasks finishing, each one quiet line",
    ),
    State(
        "chat-task-failed",
        f"{AGENT}?tab=chat",
        ".chat .notice--failed",
        runner(_stressed),
        failed_notice_chat(),
        title="Chat: a task that failed and one that was killed, said in red",
    ),
    State(
        "chat-notice-report",
        f"{AGENT}?tab=chat",
        ".chat .notice--agent[open] .notice__report",
        runner(_stressed),
        notice_report_chat(),
        title="Chat: an agent's notice opened on the report it sent, which has no card of its own",
        armed=".chat .notice--agent > summary",
        act=_click(".chat .notice--agent > summary"),
    ),
    State(
        "chat-notice-command",
        f"{AGENT}?tab=chat",
        ".chat .notice--command[open] .notice__result",
        runner(_stressed),
        notice_report_chat(),
        title="Chat: a background command's notice opened on the whole command it ran",
        armed=".chat .notice--command > summary",
        act=_click(".chat .notice--command > summary"),
    ),
    State(
        "chat-slash-commands",
        f"{AGENT}?tab=chat",
        ".chat .notice--ran[open] .notice__result",
        runner(_stressed),
        slash_command_records(),
        title="Chat: slash commands the person ran, each a quiet line, /compact opened on its output",
        armed=".chat .notice--ran > summary",
        act=_click(".chat .notice--ran > summary"),
    ),
    State(
        "chat-compacted",
        f"{AGENT}?tab=chat",
        ".chat .notice--ran[open] .notice__result",
        runner(_stressed),
        [*compaction_records(), *system_command_records(), *command_stderr_records()],
        title="Chat: /compact once, opened on its summary; system-record and failing commands",
        armed=".chat .notice--ran > summary",
        act=_click(".chat .notice--ran > summary"),
    ),
    State(
        "chat-queued",
        f"{AGENT}?tab=chat",
        ".chat .turn--agent",
        runner(_stressed),
        queued_records(),
        title="Chat: prompts sent while the agent worked, each once where it took them; one waits",
    ),
    State(
        "chat-queued-open",
        f"{AGENT}?tab=chat",
        ".chat .queued[open] .queued__text",
        runner(_stressed),
        queued_records(),
        title="Chat: the queued line opened on the prompt that waits",
        armed=".chat .queued__head",
        act=_click(".chat .queued__head"),
    ),
    State(
        "chat-bash-command",
        f"{AGENT}?tab=chat",
        ".chat .turn--agent",
        runner(_stressed),
        shell_command_records(),
        title="Chat: `!` commands the person ran, each their line opening on what it printed",
    ),
    State(
        "chat-notices",
        f"{AGENT}?tab=chat",
        ".chat .notice--command",
        runner(_stressed),
        notice_report_chat(),
        title="Chat: agents and commands that stopped, each a labelled line that opens",
    ),
    State(
        "chat-report",
        f"{AGENT}?tab=chat",
        ".chat .turn--peer",
        runner(_stressed),
        report_chat(),
        title="Chat: a subagent's hand-back report, folded",
    ),
    State(
        "chat-report-open",
        f"{AGENT}?tab=chat",
        ".chat .turn--peer .turn__more[open]",
        runner(_stressed),
        report_chat(),
        title="Chat: a subagent's hand-back report, opened with Show all",
        armed=".chat .turn--peer .turn__more summary",
        act=_click(".chat .turn--peer .turn__more summary"),
    ),
    State(
        "chat-report-table",
        f"{AGENT}?tab=chat",
        ".chat .turn--peer:has(.turn__more[open]) .prose table",
        runner(_stressed),
        report_table_chat(),
        title="Chat: a report opened on a table whose cells hold shas, paths and ticket refs",
        armed=".chat .turn--peer .turn__more summary",
        act=_click(".chat .turn--peer .turn__more summary"),
    ),
    State(
        "chat-peer-message",
        f"{AGENT}?tab=chat",
        ".chat .turn--peer",
        runner(_stressed),
        peer_message_chat(),
        title="Chat: a message another Claude session sent in",
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
        "chat-unsupported",
        f"{AGENT}?tab=chat",
        # A harness this app cannot read is read on its Pane, so the note is the way there.
        ".chat .empty .chat__go[data-hp-replace]",
        runner(lambda: _set(snapshot_frame(), WORKING_PANE, agent="cursor"), capture(WORKING)),
        title="Chat: a harness that keeps no transcript this app can read",
    ),
    State(
        "agent-first-unverified",
        f"{AGENT}?first=unverified",
        # The first prompt's fate is on the pane, so the banner is a door to it.
        ".banner--held .banner__go[data-hp-replace]",
        runner(_stressed),
        title="Agent page: opened on a first prompt the pane did not show",
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
        ".mirror .pane",
        runner(snapshot_frame, capture(WORKING) + STATUS_TAIL),
        prime=_usage_transcript,
        title="Agent page: Pane, its input box and status line gone; the strip says what they said",
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
        "agent-answer-more-keys",
        f"{AGENT}?tab=pane",
        "#answer .answer__more[open] .keys--pad",
        _blocked_on(PERMISSION),
        title="Agent page: the screen's other keys unfolded under the answers",
        armed="#answer .answer__more-head",
        act=_click("#answer .answer__more-head"),
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
        # The note names the Pane as a link to it, not as a direction.
        "#answer .answer__note .answer__go[data-hp-replace]",
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
        "agent-answer-opencode-permission",
        f"{AGENT}?tab=pane",
        "#answer .answer__question",
        _opencode_blocked_on(OC_PERMISSION),
        title="Agent page: opencode's permission dialog, its buttons and the one in focus",
    ),
    State(
        "agent-answer-opencode-question",
        f"{AGENT}?tab=pane",
        "#answer .answer__question",
        _opencode_blocked_on(OC_QUESTION),
        title="Agent page: opencode's question dialog, rows earned by its colour cursor",
    ),
    State(
        "agent-answer-opencode-listing",
        f"{AGENT}?tab=pane",
        "#answer",
        _opencode_listing(),
        title="Agent page: a numbered list in opencode's transcript, not a question",
    ),
    State(
        "agent-unreadable",
        AGENT,
        "#answer .answer__keys .keys--compact",
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
        "agent-unverified",
        AGENT,
        # The idle screen never shows the prompt, so it is sent unverified: the door to look.
        "#composer .banner--held .banner__go[data-hp-replace]",
        _prompted(capture(IDLE)),
        title="Agent page: a prompt sent, but the pane did not show it",
        armed="#composer textarea",
        act=_type_and_send("run the tests again"),
    ),
    State(
        "agent-interrupt",
        f"{AGENT}?tab=chat",
        "#answer .keys__stop--armed",
        runner(_working_as("working")),
        prime=_turn_at("tool"),
        title="Stop… from a stale page: the server holds it and asks in the answer region",
        armed="#agent-tray .tray__slot--stop form",
        # Submitted as a page whose script never armed it would: the form, not the tap.
        act="document.querySelector('#agent-tray .tray__slot--stop form').requestSubmit();",
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
        # The region always carries the hidden no-reply notice, which is also an alert.
        "#answer [role=alert]:not([data-hp-lost])",
        _blocked_on(PERMISSION),
        title="Agent page: the screen moved underneath a tapped option",
        armed="#answer .button--option",
        act=_tamper("screen", "0000000000000000", "#answer .button--option"),
    ),
)

_TAP_OPTION: Final = _click("#answer .button--option")

ANSWERED: Final = (
    State(
        "agent-answer-sending",
        f"{AGENT}?tab=pane",
        "#answer form.htmx-request .option__sending",
        _keyed(capture(PERMISSION)),
        title="Agent page: a tapped option still on its way, the row saying so",
        armed="#answer .button--option",
        act=_TAP_OPTION,
        prime=_held_key,
        in_flight=True,
    ),
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
        "new-agent-opencode-models",
        "/new",
        "main form",
        runner(snapshot_frame),
        title="New agent: the form, with opencode models declared",
        settings={"opencode_models": ("opencode/big-pickle", "anthropic/claude-sonnet-4-5")},
    ),
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
        "starting-first-unverified",
        "/starting/w1:p9?first=unverified",
        # No Pane tab here yet, so the note says where to look and Check again keeps it.
        'main .banner--held ~ .actions a[href$="?first=unverified"]',
        runner(snapshot_frame),
        title="Started, its first prompt not seen on the pane, and not yet an agent",
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
    assert not callable(frame)
    assert not isinstance(frame, Exception)
    stand_in(frame, repo)


def _files(name: str, query: str, ready: str, title: str) -> State:
    path = f"{AGENT}/files" + (f"?{query}" if query else "")
    return State(name, path, ready, runner(snapshot_frame), prime=_in_repo, title=title)


FILE_REFS: Final = (
    "The 404 branch now logs before it answers: see `src/app/server.py:34`, and the port "
    "moved to 8088 at src/app/server.py:9. The operator notes are in docs/guide.md:3."
)

STATS: Final = (
    State(
        "usage-chat",
        f"{AGENT}?tab=chat",
        "#agent-stats .meter",
        runner(_stressed),
        prime=_usage_transcript,
        title="Chat with the stats strip: model, context meter, the running turn, output",
    ),
    State(
        "usage-long-turn",
        f"{AGENT}?tab=chat",
        "#agent-stats .meter",
        runner(_stressed),
        prime=_long_turn_transcript,
        title="The stats strip on a turn two days old: the time in its largest unit, on one line",
    ),
    State(
        "usage-crowded",
        f"{AGENT}?tab=chat",
        "#agent-stats .stats__model",
        runner(_stressed),
        prime=_crowded_transcript,
        title="A strip with more facts than a narrow line holds: the last ones wait in the sheet",
    ),
    State(
        "usage-statusline",
        f"{AGENT}?tab=chat",
        "#agent-stats .stats__status",
        runner(_stressed, rig_screen("claude-statusline.ansi")),
        prime=_usage_transcript,
        title="The strip is the pane's own statusline: its first row, cut at the phone's width",
    ),
    State(
        "usage-statusline-open",
        f"{AGENT}?tab=chat",
        "#agent-stats details[open] .stats__status-rows",
        runner(_stressed, rig_screen("claude-statusline.ansi")),
        prime=_usage_transcript,
        title="The statusline's sheet: every row whole, wrapped between its pieces, then the figures",
        armed="#agent-stats .stats__line",
        act=_click("#agent-stats .stats__line"),
    ),
    State(
        "usage-statusline-narrow",
        f"{AGENT}?tab=chat",
        "#agent-stats .stats__status",
        runner(_stressed, rig_screen("claude-statusline-narrow.ansi")),
        prime=_usage_transcript,
        title="The statusline a 58-column pane cut short: the strip shows what the screen shows",
    ),
    State(
        "usage-statusline-powerline",
        f"{AGENT}?tab=chat",
        "#agent-stats details[open] .stats__status-rows",
        runner(_stressed, rig_screen("claude-statusline-powerline.ansi")),
        prime=_usage_transcript,
        title="A powerline statusline: its words and foregrounds, its arrows and icons as gaps",
        armed="#agent-stats .stats__line",
        act=_click("#agent-stats .stats__line"),
    ),
    State(
        "usage-statusline-bars",
        f"{AGENT}?tab=chat",
        "#agent-stats details[open] .stats__status-rows",
        runner(_stressed, rig_screen("claude-statusline-bars.ansi")),
        prime=_usage_transcript,
        title="A bar statusline: block meters, box-drawing bars, bullets and emoji, as printed",
        armed="#agent-stats .stats__line",
        act=_click("#agent-stats .stats__line"),
    ),
    State(
        "usage-statusline-rows",
        f"{AGENT}?tab=chat",
        "#agent-stats details[open] .stats__status-rows",
        runner(_stressed, rig_screen("claude-statusline-rows.ansi")),
        prime=_usage_transcript,
        title="A statusline of three rows and the mode row: the line is the first, the sheet has all",
        armed="#agent-stats .stats__line",
        act=_click("#agent-stats .stats__line"),
    ),
    State(
        "usage-open",
        f"{AGENT}?tab=chat",
        "#agent-stats details[open] .stats__rows",
        runner(_stressed),
        prime=_usage_transcript,
        title="The stats strip opened: every figure named, none summed",
        armed="#agent-stats .stats__line",
        act=_click("#agent-stats .stats__line"),
    ),
    State(
        "usage-opencode",
        f"/agent/{OPENCODE_SESSION}?tab=chat",
        "#agent-stats .stats__model",
        runner(_opencode_working),
        prime=_opencode_store,
        title="An opencode agent's strip: a model with no known window shows tokens, not a percent",
    ),
    State(
        "usage-opencode-models",
        f"/agent/{MIXED_SESSION}?tab=chat",
        "#agent-stats details[open] .stats__row--model",
        runner(_opencode_mixed),
        prime=_opencode_mixed_store,
        title="An opencode sheet split by model, one whose provider records nothing said so",
        armed="#agent-stats .stats__line",
        act=_click("#agent-stats .stats__line"),
    ),
    State(
        "chat-doubted-identity",
        f"/agent/{OPENCODE_SESSION}?tab=chat",
        ".chat .chat__note[role=status]",
        runner(_opencode_elsewhere),
        prime=_opencode_store,
        title="Chat: the pane reports a session started elsewhere, said above it, and writes refused",
    ),
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

# Turns Stop the animation on as the alerts page in another tab does (the device's storage, and
# the event a browser fires in every other tab), then watches the bubble frame by frame: its
# letters (where they stand, their colour, any lift) must not change over STILL_FRAMES frames
# while its timer ticks on. Only then is the proof drawn; a letter that moves, or a timer that
# stops, leaves it undrawn.
STILL_FRAMES: Final = 60
STILL_WATCH: Final = r"""
localStorage.setItem("hp-still", "1");
window.dispatchEvent(new StorageEvent("storage", { key: "hp-still", newValue: "1" }));
""" + (
    r"""
const look = () => {
  const words = document.querySelector("#agent-tray .bubble__text");
  if (!words) return "gone";
  const parts = [words.childElementCount];
  for (const node of [words, ...words.children]) {
    const box = node.getBoundingClientRect();
    const style = getComputedStyle(node);
    parts.push(box.x, box.y, box.width, style.color, style.transform);
  }
  return parts.join("|");
};
const clock = () => (document.querySelector("#agent-tray .bubble__time") || {}).textContent;
const pressed = localStorage.getItem("hp-still") === "1";
let first = null;
let started = null;
let frames = 0;
const watch = () => {
  const seen = look();
  if (first === null) {
    first = seen;
    started = clock();
  } else if (seen !== first) {
    document.body.dataset.probeMoved = "a letter moved";
    return;
  }
  frames += 1;
  if (frames >= FRAMES && clock() !== started) {
    if (pressed && seen !== "gone") document.body.dataset.probeStill = "";
    return;
  }
  requestAnimationFrame(watch);
};
requestAnimationFrame(watch);
""".replace("FRAMES", str(STILL_FRAMES))
)

TURNS: Final = (
    State(
        "chat-working-tool",
        f"{AGENT}?tab=chat",
        "#agent-tray",
        runner(_working_as("working")),
        prime=_turn_at("tool"),
        title="Chat while a tool runs: the bubble names the tool",
    ),
    State(
        "chat-working-still",
        f"{AGENT}?tab=chat",
        "body[data-probe-still]",
        runner(_working_as("working")),
        prime=_turn_at("tool"),
        title="Chat while a tool runs, Stop the animation on: the words stand still, the timer ticks",
        armed="#agent-tray .bubble__glyph",
        act=STILL_WATCH,
    ),
    State(
        "chat-working-thinking",
        f"{AGENT}?tab=chat",
        "#agent-tray",
        runner(_working_as("working")),
        prime=_turn_at("thinking"),
        title="Chat while the agent thinks between tools: the turn's one verb",
    ),
    State(
        "chat-working-quiet",
        f"{AGENT}?tab=chat",
        "#agent-tray",
        runner(_working_as("working")),
        prime=_turn_at("quiet"),
        title="Chat with a working agent that has written nothing for three minutes",
    ),
    State(
        "chat-working-spawned",
        f"{AGENT}?tab=chat",
        "#agent-tray",
        runner(_working_as("working")),
        prime=_spawned_at,
        title="Chat while the agent waits on agents it started in the background",
    ),
    State(
        "chat-working-crowded",
        f"{AGENT}?tab=chat",
        "#agent-tray",
        runner(_working_as("working")),
        prime=_crowded_at,
        title="Chat while the agent waits on two agents it started, one prompt queued",
    ),
    State(
        "chat-working-sheet",
        f"{AGENT}?tab=chat",
        "#agent-tray details[open] .tray__pad",
        runner(_working_as("working")),
        prime=_crowded_at,
        title="The status row's sheet open: the agents it started, Stop and the key pad",
        armed="#agent-tray .tray__row",
        act=_click("#agent-tray .tray__row"),
    ),
    State(
        "chat-working-armed",
        f"{AGENT}?tab=chat",
        "#agent-tray .tray__head[data-hp-arming='ctrl-c'] .tray__armed:not([hidden])",
        runner(_working_as("working")),
        prime=_crowded_at,
        title="Stop armed in place: Keep going where Stop… was, Stop now filled on the far side",
        armed="#agent-tray .tray__row",
        act=_click("#agent-tray .tray__row") + _click("#agent-tray .tray__slot--stop .tray__arm"),
    ),
    State(
        "chat-working-esc-armed",
        f"{AGENT}?tab=chat",
        "#agent-tray .tray__head[data-hp-arming='esc'] .tray__armed:not([hidden])",
        runner(_working_as("working")),
        prime=_turn_at("tool"),
        title="Esc… while the agent works arms the same confirm, Keep going where Esc… was",
        armed="#agent-tray .tray__row",
        act=_click("#agent-tray .tray__row") + _click("#agent-tray .tray__slot--esc .tray__arm"),
    ),
    State(
        "chat-working-quiet-sheet",
        f"{AGENT}?tab=chat",
        "#agent-tray details[open] .tray__note",
        runner(_working_as("working")),
        prime=_turn_at("quiet"),
        title="The sheet of an agent gone quiet: herdr still says working, and Stop… stays",
        armed="#agent-tray .tray__row",
        act=_click("#agent-tray .tray__row"),
    ),
    State(
        "chat-idle",
        f"{AGENT}?tab=chat",
        ".chat .turn--agent",
        runner(_working_as("idle")),
        prime=_turn_at("done"),
        title="Chat with an idle agent whose last turn is over",
    ),
    State(
        "chat-idle-shells",
        f"{AGENT}?tab=chat",
        "#agent-tray .tray__row--background",
        runner(_working_as("idle")),
        prime=_background_at(LeftRunning(shells=2)),
        title="Idle, with two shells it started still running: the row says so",
    ),
    State(
        "chat-idle-agent",
        f"{AGENT}?tab=chat",
        "#agent-tray .tray__row--background",
        runner(_working_as("idle")),
        prime=_background_at(LeftRunning(agents=1)),
        title="Idle, with an agent it started still running",
    ),
    State(
        "chat-idle-both",
        f"{AGENT}?tab=chat",
        "#agent-tray .tray__row--background",
        runner(_working_as("idle")),
        prime=_background_at(LeftRunning(shells=3, agents=2, old=1)),
        title="Idle, with shells and agents it started still running, and an older server",
    ),
    State(
        "chat-idle-both-sheet",
        f"{AGENT}?tab=chat",
        "#agent-tray details[open] .spawned",
        runner(_working_as("idle")),
        prime=_background_at(LeftRunning(shells=3, agents=2, old=1)),
        title="The sheet of an idle agent: agents, then shells (the last turn's first), by name and time",
        armed="#agent-tray .tray__row",
        act=_click("#agent-tray .tray__row"),
    ),
    State(
        "feed-idle-pending",
        "/",
        "#feed-list .card__status--running",
        runner(_working_as("idle")),
        prime=_background_at(LeftRunning(shells=1, agents=2), server=True),
        title="The feed: an idle agent whose work still runs leads the idle cards in blue; one that "
        "left only an old server stays plain idle",
    ),
    State(
        "chat-needs-you",
        f"{AGENT}?tab=chat",
        "#answer .answer__question",
        runner(_working_as("blocked"), capture(PERMISSION)),
        prime=_turn_at("asking"),
        title="Chat with the agent stopped on a permission question",
    ),
    State(
        "chat-worked",
        f"{AGENT}?tab=chat",
        ".chat .turn--agent",
        runner(_working_as("done")),
        prime=_turn_at("done"),
        title="Chat after a turn ends: how long it worked and how many tools it called",
    ),
)

STATES: Final = (*FEED, *AGENT_PAGE, *TAPPED, *ANSWERED, *LIFECYCLE, *STATS, *FILES, *TURNS)


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
