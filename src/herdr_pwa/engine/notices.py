"""What Claude Code writes as a user record but nobody typed: task notices and peer messages."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Final

from herdr_pwa.jsontypes import JsonObject, as_object

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

    @property
    def failed(self) -> bool:
        """Whether it ended any way but completed (failed, killed)."""
        return bool(self.status) and self.status != _DONE


@dataclass(frozen=True, slots=True)
class PeerMessage:
    """A message from another agent: a subagent's hand-back report, or another session."""

    sender: str
    body: str
    report: bool


type Injected = TaskNotice | PeerMessage | Hidden


def without_reminders(text: str) -> str:
    """*text* with every ``<system-reminder>`` block taken out."""
    return _REMINDER.sub("", text).strip()


def _tag(body: str, name: str) -> str:
    found = re.search(rf"<{name}>(.*?)</{name}>", body, re.DOTALL)
    return found.group(1).strip() if found else ""


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
    )


def _peer(origin: JsonObject, text: str) -> PeerMessage | None:
    tagged = _peer_from_tag(text)
    body = origin.get("body")
    if not isinstance(body, str):
        return tagged
    name = origin.get("name") or origin.get("from")
    sender = name if isinstance(name, str) else ""
    return PeerMessage(
        sender=sender or (tagged.sender if tagged else ""),
        body=_report(body),
        report=origin.get("handback") is True or body.startswith(_HANDBACK_FRAME),
    )


def _by_tag(text: str) -> Injected | None:
    if not text:
        return HIDDEN
    if text.startswith("<task-notification>"):
        return _task(text) or HIDDEN
    return _peer_from_tag(text)


def injected(record: JsonObject, content: str, *, human: bool) -> Injected | None:
    """What a string user record is if the harness wrote it, or ``None`` to treat it as text.

    Classified by ``origin.kind`` first, then, for a record no person typed, by the leading
    tag, which is all an older record carries.
    """
    origin = as_object(record.get("origin")) or {}
    kind = origin.get("kind")
    text = without_reminders(content)
    if kind == "task-notification":
        return _task(text) or HIDDEN
    if kind == "peer":
        return _peer(origin, text) or HIDDEN
    if human:
        return None
    return _by_tag(text)
