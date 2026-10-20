"""What a harness writes that nobody typed or said: task notices, peer messages, API errors,
and the slash commands the person ran in the harness itself."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from enum import Enum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from herdr_pwa.panetext import plain
from herdr_pwa.validation import Flag, Text

# The harness's own wrapper, never shown: it is context for the model, not conversation.
_REMINDER: Final = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)
_TASK: Final = re.compile(r"\A<task-notification>(.*?)</task-notification>", re.DOTALL)
_PEER: Final = re.compile(
    r"<(agent-message|cross-session-message)\b([^>]*)>\n?(.*?)\n?</\1>", re.DOTALL
)
_ATTR: Final = re.compile(r'([\w-]+)="([^"]*)"')
# Measured: the frame before a subagent's report, then the report with every line indented.
_HANDBACK_FRAME: Final = "[Subagent hand-back]"
_HANDBACK_INDENT: Final = "  "
_DONE: Final = "completed"
# Measured summary shapes, across every local log: Agent "<description>" finished,
# Agent "<description>" failed: <why>, Agent "<description>" was stopped by Claude,
# Background command "<command>" completed (exit code 1: No matches found),
# Background command "<command>" failed with exit code 143, ... was stopped because <why>,
# Task "<description>" was stopped by main session, and Agent "<description>" is waiting on its
# own background work, which is an agent that stopped but is not done: it notifies again.
_SUMMARY: Final = re.compile(
    r'\A(Agent|Background command|Task) "(.*?)" '
    r"((?:was |is )?(finished|completed|failed|killed|stopped|waiting)(?:\W.*)?)\Z",
    re.DOTALL,
)
_WAITING: Final = "waiting"
_KINDS: Final = {"Agent": "Agent", "Background command": "Command", "Task": "Task"}
_EXIT: Final = re.compile(r"exit code (-?\d+)")
# What the harness puts in an agent's result in place of the report it delivers as a message.
_RELAYED: Final = re.compile(r"\AThis agent(?:'s report was delivered| has not reported yet)")


class RecordOrigin(BaseModel):
    """A user record's ``origin``: who wrote it, and for a peer, who sent it and what."""

    model_config = ConfigDict(extra="ignore")

    kind: Text = None
    body: Text = None
    name: Text = None
    sender: Text = Field(default=None, validation_alias="from")
    sender_task: Text = Field(default=None, validation_alias="senderTaskId")
    handback: Flag = None


class Hidden(Enum):
    """A record that is harness machinery only, with nothing to show."""

    hidden = "hidden"


HIDDEN: Final = Hidden.hidden


@dataclass(frozen=True, slots=True)
class TaskNotice:
    """A background task stopped: its one-line summary, how it ended, and its result."""

    summary: str
    status: str
    elapsed_ms: int
    result: str
    # The task's id, which a subagent's hand-back names as who it is from.
    task: str = ""
    # Where the harness spooled a background task's output; named, never read.
    output: str = ""

    @property
    def failed(self) -> bool:
        """Whether it ended any way but completed (failed, killed)."""
        return bool(self.status) and self.status != _DONE

    @property
    def kind(self) -> str:
        """What stopped, as the line names it: ``Agent``, ``Command``, or any other ``Task``."""
        found = _SUMMARY.match(self.summary)
        return _KINDS[found.group(1)] if found else "Task"

    @property
    def detail(self) -> str:
        """The agent's description or the command run, out of the summary; else the summary."""
        found = _SUMMARY.match(self.summary)
        return found.group(2) if found else self.summary

    @property
    def outcome(self) -> str:
        """How it ended, in the harness's words, when that says more than one word."""
        found = _SUMMARY.match(self.summary)
        if found is None or found.group(3) == found.group(4):
            return ""
        said = found.group(3).removeprefix("was ").removeprefix("is ")
        return said[:1].upper() + said[1:]

    @property
    def ended(self) -> str:
        """The word or two the line says it ended with: "finished", "exit 0", "failed, exit 1"."""
        found = _SUMMARY.match(self.summary)
        word = found.group(4) if found else self.status
        code = _EXIT.search(found.group(3)) if found else None
        if code is None:
            return word
        return f"{word}, exit {code.group(1)}" if self.failed else f"exit {code.group(1)}"

    @property
    def waiting(self) -> bool:
        """Whether its task stopped with work of its own still running, so it is not over."""
        found = _SUMMARY.match(self.summary)
        return found is not None and found.group(4) == _WAITING

    @property
    def said(self) -> str:
        """The result worth showing: an agent's is only a pointer at its report when relayed."""
        return "" if _RELAYED.match(self.result) else self.result


@dataclass(frozen=True, slots=True)
class PeerMessage:
    """A message from another agent: a subagent's hand-back report, or another session."""

    sender: str
    body: str
    report: bool
    # The sending task's id, the one a task notification carries.
    task: str = ""


# Measured: a slash command is one record, `<command-name>/compact</command-name>` then its
# `<command-message>` and `<command-args>`; what it printed is the next record, on its own.
_COMMAND: Final = "<command-name>"
_STDOUT: Final = "local-command-stdout"
# Not seen in any local log: a failing command's output, assumed to take the stdout shape.
_STDERR: Final = "local-command-stderr"
# Measured: what the harness writes for a command that printed nothing (/chrome, /exit).
_NO_CONTENT: Final = "(no content)"
# The line's label for a slash command, and for its output when the command is not on the page.
SLASH: Final = "Ran"
OUTPUT: Final = "Output"
SLASH_KINDS: Final = frozenset({SLASH, OUTPUT})
# Measured: a `!` command the person ran is `<bash-input>` alone; what it printed is the next
# record, its `<bash-stdout>` then its `<bash-stderr>`.
_BASH_INPUT: Final = "bash-input"
_BASH_STDOUT: Final = "bash-stdout"
_BASH_STDERR: Final = "bash-stderr"
# Measured: what the harness writes for a `!` command that printed nothing.
_NO_OUTPUT: Final = "(Bash completed with no output)"
# How the chat says it, as the command's ending, so a missing fold never reads as a lost output.
PRINTED_NOTHING: Final = "no output"
# What a `!` command leads with, as the person typed it, and the tool its line carries.
SHELL: Final = "!"
# The command whose run the harness's compact summary belongs to.
COMPACT_COMMAND: Final = "/compact"
# A compact summary's label, and its line in Claude Code's words on the boundary written before it.
SUMMARY: Final = "Summary"
SUMMARY_LINE: Final = "Conversation compacted"


@dataclass(frozen=True, slots=True)
class SlashCommand:
    """A slash command the person ran in the harness: its name and what followed it."""

    name: str
    args: str

    @property
    def line(self) -> str:
        """The command as the person typed it: ``/compact keep the tooling``."""
        return f"{self.name} {self.args}".strip()


@dataclass(frozen=True, slots=True)
class CommandOutput:
    """What a slash command printed, terminal styling dropped, and whether it was its stderr."""

    said: str
    failed: bool = False
    # The harness said the command printed nothing, which an output never recorded does not say.
    nothing: bool = False


@dataclass(frozen=True, slots=True)
class ShellCommand:
    """A ``!`` command the person ran in the harness's shell mode."""

    command: str

    @property
    def line(self) -> str:
        """The command as the person typed it: ``! git status``."""
        return f"{SHELL} {self.command}"


type Injected = TaskNotice | PeerMessage | SlashCommand | ShellCommand | CommandOutput | Hidden


# Measured: the text Claude Code writes when a turn no longer fits, with `error: invalid_request`.
_TOO_LONG: Final = "Prompt is too long"
# Measured through opencode, an OpenAI-compatible provider's 400: "Requested token count exceeds
# the model's maximum context length of 202752 tokens. ..."
_OVERFLOW: Final = re.compile(r"\bmaximum context length\b")
# Measured: "You've hit your session limit · resets 5am (<the user's time zone>)", and "weekly".
_LIMIT: Final = re.compile(r"\AYou've hit your (\w+) limit · (resets .+)\Z", re.DOTALL)
# Claude Code's words for why a turn was refused, which an HTTP status is read into.
_INVALID_REQUEST: Final = "invalid_request"
_RATE_LIMIT: Final = "rate_limit"
_BY_STATUS: Final = {400: _INVALID_REQUEST, 429: _RATE_LIMIT}
# Not the API's: the person stopped the turn (opencode's MessageAbortedError).
INTERRUPTED_CODE: Final = "interrupted"
# The line's label for each kind; a task notice's are Agent, Command and Task.
CONTEXT: Final = "Context"
LIMIT: Final = "Limit"
ERROR: Final = "Error"
INTERRUPTED: Final = "Interrupted"
API_ERROR_KINDS: Final = frozenset({CONTEXT, LIMIT, ERROR, INTERRUPTED})
# What unblocks a full context, in either harness: both take /compact.
_COMPACT: Final = "run /compact or start a new session"
# A stalled agent's card and push, which carry no label beside the line as the chat does.
CONTEXT_FULL: Final = f"Context full · {_COMPACT}"


@dataclass(frozen=True, slots=True)
class ApiError:
    """A turn that ended with no answer: what happened, what unblocks it, the harness's words."""

    kind: str
    line: str
    said: str
    # An interrupt is the person's own doing, so it is said without the alarm of a failure.
    failed: bool = True


def status_code(status: int | None) -> str | None:
    """An HTTP status as the code Claude Code would have written for it, if it is one we read."""
    return _BY_STATUS.get(status) if status is not None else None


def api_error(code: str | None, said: str) -> ApiError:
    """What a turn the harness ended in place of an answer means for the person."""
    said = said.strip()
    if code == INTERRUPTED_CODE:
        return ApiError(INTERRUPTED, "", "", failed=False)
    if code == _INVALID_REQUEST and (said.startswith(_TOO_LONG) or _OVERFLOW.search(said)):
        return ApiError(CONTEXT, f"Full · {_COMPACT}", said)
    if code == _RATE_LIMIT:
        return _limit(said)
    return ApiError(ERROR, said, said)


def _limit(said: str) -> ApiError:
    found = _LIMIT.match(said)
    if found is None:
        # A provider's own rate limit, which names no reset this reader could trust.
        return ApiError(LIMIT, "Rate limited · wait, then send it again", said)
    return ApiError(LIMIT, f"{found.group(1).capitalize()} limit · {found.group(2)}", said)


# Measured: Claude Code writes a paste inside a prompt between these, a tag the person never typed.
_PASTE_TAG: Final = re.compile(r'\n?</pasted_content id="[^"]*">|<pasted_content id="[^"]*">\n?')


def without_paste_tags(text: str) -> str:
    """*text* with the tags the harness wraps a paste in taken out, the paste kept."""
    return _PASTE_TAG.sub("", text)


def without_reminders(text: str) -> str:
    """*text* with every ``<system-reminder>`` block taken out."""
    return _REMINDER.sub("", text).strip()


def _tag(body: str, name: str) -> str:
    """A tag's text, its XML entities decoded here and nowhere else (the page escapes it once)."""
    found = re.search(rf"<{name}>(.*?)</{name}>", body, re.DOTALL)
    return html.unescape(found.group(1).strip()) if found else ""


def _millis(text: str) -> int:
    return int(text) if text.isdigit() else 0


def _task(text: str) -> TaskNotice | None:
    found = _TASK.match(text)
    if found is None:
        return None
    body = found.group(1)
    # Measured: some deliveries escape the quotes round the description, as JSON would.
    summary = _tag(body, "summary").replace('\\"', '"')
    return TaskNotice(
        summary=summary or _tag(body, "task-id"),
        status=_tag(body, "status"),
        elapsed_ms=_millis(_tag(body, "duration_ms")),
        result=_tag(body, "result"),
        task=_tag(body, "task-id"),
        output=_tag(body, "output-file"),
    )


def _report(body: str) -> str:
    """A hand-back's report without the frame the harness puts before it."""
    if not body.startswith(_HANDBACK_FRAME):
        return body.strip()
    _, _, report = body.partition("\n")
    lines = (line.removeprefix(_HANDBACK_INDENT) for line in report.splitlines())
    return "\n".join(lines).strip()


def _peer_from_tag(text: str) -> PeerMessage | None:
    found = _PEER.search(text)
    # The harness leads with one line ("Another Claude session sent a message:"), no more.
    if found is None or "\n" in text[: found.start()].strip():
        return None
    attrs = {attr.group(1): attr.group(2) for attr in _ATTR.finditer(found.group(2))}
    body = found.group(3)
    return PeerMessage(
        sender=attrs.get("from-name") or attrs.get("from", ""),
        body=_report(body),
        report=body.startswith(_HANDBACK_FRAME),
        task=attrs.get("from", ""),
    )


def _peer(origin: RecordOrigin, text: str) -> PeerMessage | None:
    tagged = _peer_from_tag(text)
    body = origin.body
    if body is None:
        return tagged
    return PeerMessage(
        sender=origin.name or origin.sender or (tagged.sender if tagged else ""),
        body=_report(body),
        report=origin.handback is True or body.startswith(_HANDBACK_FRAME),
        task=origin.sender_task or origin.sender or "",
    )


def _output(text: str, stream: str) -> CommandOutput | Hidden:
    said = plain(_tag(text, stream)).strip()
    failed = stream == _STDERR
    return CommandOutput(said, failed) if said and said != _NO_CONTENT else HIDDEN


def _shell_output(text: str) -> CommandOutput | Hidden:
    out = plain(_tag(text, _BASH_STDOUT)).strip()
    err = plain(_tag(text, _BASH_STDERR)).strip()
    said = "\n".join(part for part in (out, err) if part and part != _NO_OUTPUT)
    if said:
        return CommandOutput(said, failed=bool(err) and not out)
    return CommandOutput("", nothing=True) if out == _NO_OUTPUT else HIDDEN


def _shell(text: str) -> Injected | None:
    if text.startswith(f"<{_BASH_INPUT}>"):
        return ShellCommand(_tag(text, _BASH_INPUT))
    if text.startswith(f"<{_BASH_STDOUT}>"):
        return _shell_output(text)
    return None


def _by_tag(text: str) -> Injected | None:
    if not text:
        return HIDDEN
    if text.startswith("<task-notification>"):
        return _task(text) or HIDDEN
    if text.startswith(_COMMAND):
        return SlashCommand(_tag(text, "command-name"), _tag(text, "command-args"))
    for stream in (_STDOUT, _STDERR):
        if text.startswith(f"<{stream}>"):
            return _output(text, stream)
    return _shell(text) or _peer_from_tag(text)


def injected(origin: RecordOrigin | None, content: str, *, human: bool) -> Injected | None:
    """What a string user record is if the harness wrote it, or ``None`` to treat it as text.

    Classified by ``origin.kind`` first, then, for a record no person typed, by the leading
    tag, which is all an older record carries.
    """
    kind = origin.kind if origin is not None else None
    text = without_reminders(content)
    if kind == "task-notification":
        return _task(text) or HIDDEN
    if origin is not None and kind == "peer":
        return _peer(origin, text) or HIDDEN
    if human:
        return None
    return _by_tag(text)
