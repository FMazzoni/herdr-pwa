"""What Claude Code writes as a user record but nobody typed: task notices and peer messages."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from enum import Enum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

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
# and Task "<description>" was stopped by main session.
_SUMMARY: Final = re.compile(
    r'\A(Agent|Background command|Task) "(.*?)" '
    r"((?:was )?(finished|completed|failed|killed|stopped)(?:\W.*)?)\Z",
    re.DOTALL,
)
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
        said = found.group(3).removeprefix("was ")
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


type Injected = TaskNotice | PeerMessage | Hidden


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
    return TaskNotice(
        summary=_tag(body, "summary") or _tag(body, "task-id"),
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


def _by_tag(text: str) -> Injected | None:
    if not text:
        return HIDDEN
    if text.startswith("<task-notification>"):
        return _task(text) or HIDDEN
    return _peer_from_tag(text)


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
