"""Every write the runner is asked to make, recorded before it is made.

Writes only: reads, push subscribe/unsubscribe, and the app's own refusals are not recorded.
"""

import json
import logging
import os
import secrets
import stat
import threading
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Annotated, Literal, NotRequired, Self, TypedDict

from pydantic import Field, TypeAdapter, ValidationError

from herdr_pwa import statedir
from herdr_pwa.auth import Identity
from herdr_pwa.clock import Clock
from herdr_pwa.statedir import FILE_MODE

log = logging.getLogger(__name__)

AUDIT_FILENAME = "audit.log"
# Two generations bound the disk use with no dated files and so no reaper.
ROTATED_FILENAME = "audit.log.1"
PROMPT = "prompt"
KEYS = "keys"
START = "start"
# Close is recorded because it is the one write with no undo.
CLOSE = "close"
ENDPOINTS = frozenset({PROMPT, KEYS, START, CLOSE})
ATTEMPT = "attempt"
OUTCOME = "outcome"
UNREADABLE = "unreadable"
OMITTED_NO_ECHO = "no_echo_prompt"
UNRECORDED = "unrecorded"
_MARK = "…[truncated: {total} chars sent]"


class Attempt(TypedDict):
    """The record written, and fsynced, before the write it names is made."""

    v: Literal[1]
    phase: Literal["attempt"]
    id: str
    ts: str
    endpoint: str
    target: str
    session: str
    actor: str
    actor_via: str
    client_ip: str
    forwarded_for: str
    # Absent from records written before they were kept; a forged write shows its own names here.
    host: NotRequired[str]
    origin: NotRequired[str]
    payload: str | None
    payload_chars: int
    payload_truncated: bool
    payload_omitted: str


class Outcome(TypedDict):
    """The record written after the write, naming what it did."""

    v: Literal[1]
    phase: Literal["outcome"]
    id: str
    ts: str
    endpoint: str
    target: str
    result: str


class Unreadable(TypedDict):
    """A line that is not one of the two records, reported where it is rather than skipped."""

    phase: Literal["unreadable"]
    ts: str
    at: str


# A record is read by key, as a person reading the log would; the phase says which one it is.
type Record = Attempt | Outcome | Unreadable
_LINE: TypeAdapter[Attempt | Outcome] = TypeAdapter(
    Annotated[Attempt | Outcome, Field(discriminator="phase")]
)


class AuditUnavailableError(Exception):
    """The attempt could not be recorded, so the write it records must not happen."""


@dataclass(frozen=True, slots=True)
class Write:
    """What one write asks the runner to do; the payload is truncated by the log, not the caller."""

    endpoint: str
    target: str = ""
    session: str = ""
    payload: str = ""
    # Set when the screen showed a no-echo prompt: the length is kept, the text is not.
    omitted: str = ""


@dataclass(frozen=True, slots=True)
class Origin:
    """Who is asking and from where; neither address is treated as identity."""

    actor: str
    via: str
    client_ip: str = ""
    forwarded_for: str = ""
    # The Host and Origin headers the request carried, as sent.
    host: str = ""
    origin: str = ""

    @classmethod
    def of(cls, identity: Identity, *, client_ip: str = "", headers: Mapping[str, str]) -> Self:
        """The origin of a request that ``auth.authorize`` admitted as this identity."""
        lowered = {key.lower(): value for key, value in headers.items()}
        return cls(
            identity.login,
            identity.via,
            client_ip,
            lowered.get("x-forwarded-for", ""),
            lowered.get("host", ""),
            lowered.get("origin", ""),
        )


def _iso(at: float) -> str:
    """A wall-clock stamp as the log writes it: ISO 8601 in UTC."""
    return datetime.fromtimestamp(at, UTC).isoformat()


def _payload(text: str, *, limit: int, omitted: str) -> tuple[str | None, bool]:
    if omitted:
        # Decided from the screen, never by guessing whether the text looks secret.
        return None, False
    if len(text) > limit:
        return text[:limit] + _MARK.format(total=len(text)), True
    return text, False


class Entry:
    """One write in flight: its attempt is on disk and its outcome is owed."""

    def __init__(self, audit: "AuditLog", record: Attempt) -> None:
        self._audit: AuditLog = audit
        self._record: Attempt = record
        self._outcome: str = ""

    @property
    def id(self) -> str:
        """The id both of this write's records carry."""
        return self._record["id"]

    def outcome(self, result: str) -> None:
        """Name what the write did; the outcome record carries it verbatim."""
        self._outcome = result.strip() or UNRECORDED

    def __enter__(self) -> Self:
        """The attempt is already recorded; this only hands back the handle."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        """Write the outcome whatever happened, and never swallow the exception."""
        result = self._outcome
        if not result and exc_type is not None:
            result = f"error:{exc_type.__name__}"
        if not result:
            result = UNRECORDED
            log.error("audit %s: the write named no outcome; this is a caller bug", self.id)
        self._audit.close(self._record, result)
        return False


class AuditLog:
    """The append-only JSONL record of every write, and its only writer."""

    def __init__(
        self,
        state_dir: Path,
        *,
        max_bytes: int,
        payload_chars: int,
        clock: Clock = time.time,
    ) -> None:
        self._state_dir: Path = state_dir
        self._max_bytes: int = max_bytes
        self._payload_chars: int = payload_chars
        self._clock: Clock = clock
        self._lock: threading.Lock = threading.Lock()

    @property
    def path(self) -> Path:
        """The live file, in a state directory created 0700 and checked."""
        return statedir.ensure_dir(self._state_dir) / AUDIT_FILENAME

    def preflight(self) -> None:
        """Prove at startup that a write could be recorded; a box that cannot record does not serve.

        Raises:
            AuditUnavailableError: the log cannot be opened at 0600.
        """
        try:
            with self._lock:
                os.close(self._open())
        except OSError as exc:
            raise AuditUnavailableError(f"cannot open the audit log: {exc}") from exc

    def record(self, origin: Origin, write: Write) -> Entry:
        """Append and fsync the attempt, then hand back the handle its outcome closes.

        Raises:
            ValueError: the endpoint is not one of the writes this log records.
            AuditUnavailableError: the attempt could not be recorded, so the write must not happen.
        """
        if write.endpoint not in ENDPOINTS:
            raise ValueError(f"{write.endpoint!r} is not one of {sorted(ENDPOINTS)}")
        payload, truncated = _payload(
            write.payload, limit=self._payload_chars, omitted=write.omitted
        )
        attempt: Attempt = {
            "v": 1,
            "phase": ATTEMPT,
            "id": secrets.token_hex(8),
            "ts": _iso(self._clock()),
            "endpoint": write.endpoint,
            "target": write.target,
            "session": write.session,
            "actor": origin.actor,
            "actor_via": origin.via,
            "client_ip": origin.client_ip,
            "forwarded_for": origin.forwarded_for,
            "host": origin.host,
            "origin": origin.origin,
            "payload": payload,
            "payload_chars": len(write.payload),
            "payload_truncated": truncated,
            "payload_omitted": write.omitted,
        }
        try:
            with self._lock:
                self._append(attempt)
        except OSError as exc:
            raise AuditUnavailableError(f"cannot record {write.endpoint}: {exc}") from exc
        return Entry(self, attempt)

    def close(self, attempt: Attempt, result: str) -> None:
        """Append the outcome; it cannot fail closed, so it fails loud and never raises."""
        outcome: Outcome = {
            "v": 1,
            "phase": OUTCOME,
            "id": attempt["id"],
            "ts": _iso(self._clock()),
            "endpoint": attempt["endpoint"],
            "target": attempt["target"],
            "result": result,
        }
        try:
            with self._lock:
                self._append(outcome)
        except OSError:
            log.exception(
                "audit %s: the write HAPPENED and its outcome %r was not recorded; "
                "the attempt stands alone and reads as 'this may have run'",
                attempt["id"],
                result,
            )

    def _open(self) -> int:
        path = self.path
        handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, FILE_MODE)
        try:
            _checked_mode(handle, path)
        except BaseException:
            os.close(handle)
            raise
        return handle

    def _append(self, record: Attempt | Outcome) -> None:
        line = _LINE.dump_json(record) + b"\n"
        self._rotate(len(line))
        handle = self._open()
        try:
            _ = os.write(handle, line)
            os.fsync(handle)
        finally:
            os.close(handle)

    def _rotate(self, incoming: int) -> None:
        path = self.path
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return
        if size + incoming > self._max_bytes:
            _ = path.replace(path.with_name(ROTATED_FILENAME))


def _checked_mode(handle: int, path: Path) -> None:
    # Checked after every open: a file chmod-ed wider since is put back before it is written.
    if stat.S_IMODE(os.fstat(handle).st_mode) != FILE_MODE:
        os.fchmod(handle, FILE_MODE)
    mode = stat.S_IMODE(os.fstat(handle).st_mode)
    if mode != FILE_MODE:
        raise PermissionError(f"{path} is mode {mode:04o}, expected 0600")


@dataclass(frozen=True, slots=True)
class Recorder:
    """One request's audit handle: the log plus who is asking."""

    audit: AuditLog
    origin: Origin

    def record(self, write: Write) -> Entry:
        """``AuditLog.record`` with this request's origin supplied."""
        return self.audit.record(self.origin, write)


def _lines(path: Path) -> Iterator[str]:
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            yield from handle
    except FileNotFoundError:
        return


def _parse_line(line: str, where: str) -> Record:
    try:
        return _LINE.validate_json(line)
    except ValidationError:
        # A damaged line is reported in place, never skipped.
        return {"phase": UNREADABLE, "ts": "", "at": where}


def records(state_dir: Path, *, limit: int = 50) -> list[Record]:
    """The most recent records across both generations, oldest first."""
    base = statedir.ensure_dir(state_dir)
    found: list[Record] = []
    for name in (ROTATED_FILENAME, AUDIT_FILENAME):
        for number, line in enumerate(_lines(base / name), start=1):
            if line.strip():
                found.append(_parse_line(line, f"{name}:{number}"))
    return found[-limit:] if limit > 0 else found


def describe(record: Record) -> str:
    """One record as one readable line."""
    stamp = record["ts"][:19]
    if record["phase"] == UNREADABLE:
        return f"{stamp}  UNREADABLE RECORD at {record['at']}"
    head = f"{stamp}  {record['endpoint']:<7} {record['target'] or '-':<12}"
    if record["phase"] == OUTCOME:
        return f"{head} -> {record['result']}"
    where = record["forwarded_for"] or record["client_ip"] or "?"
    host = record.get("host", "")
    who = f"{record['actor']} via {record['actor_via']} from {where}" + (
        f" to {host}" if host else ""
    )
    return f"{head} {who}  {_shown(record)}"


def _shown(record: Attempt) -> str:
    omitted = record["payload_omitted"]
    if omitted:
        return f"<payload withheld: {omitted}, {record['payload_chars']} chars>"
    payload = record["payload"]
    return "" if payload is None else json.dumps(payload, ensure_ascii=False)
