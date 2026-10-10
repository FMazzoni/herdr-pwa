"""Every write the runner is asked to make, recorded before it is made.

Writes only: reads, push subscribe/unsubscribe, and the app's own refusals are not recorded.
"""

import json
import logging
import os
import secrets
import stat
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Literal, Self

from herdr_pwa import jsonio, statedir
from herdr_pwa.auth import Identity
from herdr_pwa.statedir import FILE_MODE, StateError

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

    @classmethod
    def of(cls, identity: Identity, *, client_ip: str = "", forwarded_for: str = "") -> Self:
        """The origin of a request that ``auth.authorize`` admitted as this identity."""
        return cls(identity.login, identity.via, client_ip, forwarded_for)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _payload_fields(text: str, *, limit: int, omitted: str) -> dict[str, object]:
    total = len(text)
    if omitted:
        # Decided from the screen, never by guessing whether the text looks secret.
        return {
            "payload": None,
            "payload_chars": total,
            "payload_truncated": False,
            "payload_omitted": omitted,
        }
    truncated = total > limit
    return {
        "payload": text[:limit] + _MARK.format(total=total) if truncated else text,
        "payload_chars": total,
        "payload_truncated": truncated,
        "payload_omitted": "",
    }


class Entry:
    """One write in flight: its attempt is on disk and its outcome is owed."""

    def __init__(self, audit: "AuditLog", record: dict[str, object]) -> None:
        self._audit: AuditLog = audit
        self._record: dict[str, object] = record
        self._outcome: str = ""

    @property
    def id(self) -> str:
        """The id both of this write's records carry."""
        return str(self._record["id"])

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
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._state_dir: Path = state_dir
        self._max_bytes: int = max_bytes
        self._payload_chars: int = payload_chars
        self._clock: Callable[[], datetime] = clock
        self._lock: threading.Lock = threading.Lock()

    @property
    def path(self) -> Path:
        """The live file, in a state directory created 0700 and checked."""
        try:
            return statedir.ensure_dir(self._state_dir) / AUDIT_FILENAME
        except StateError as exc:
            raise AuditUnavailableError(str(exc)) from exc

    def preflight(self) -> None:
        """Prove at startup that a write could be recorded; a box that cannot record does not serve."""
        with self._lock:
            os.close(self._open())

    def record(self, origin: Origin, write: Write) -> Entry:
        """Append and fsync the attempt, then hand back the handle its outcome closes."""
        if write.endpoint not in ENDPOINTS:
            raise ValueError(f"{write.endpoint!r} is not one of {sorted(ENDPOINTS)}")
        entry: dict[str, object] = {
            "v": 1,
            "phase": ATTEMPT,
            "id": secrets.token_hex(8),
            "ts": self._clock().isoformat(),
            "endpoint": write.endpoint,
            "target": write.target,
            "session": write.session,
            "actor": origin.actor,
            "actor_via": origin.via,
            "client_ip": origin.client_ip,
            "forwarded_for": origin.forwarded_for,
            **_payload_fields(write.payload, limit=self._payload_chars, omitted=write.omitted),
        }
        with self._lock:
            self._append(entry)
        return Entry(self, entry)

    def close(self, attempt: dict[str, object], result: str) -> None:
        """Append the outcome; it cannot fail closed, so it fails loud and never raises."""
        record = {
            "v": 1,
            "phase": OUTCOME,
            "id": attempt["id"],
            "ts": self._clock().isoformat(),
            "endpoint": attempt["endpoint"],
            "target": attempt["target"],
            "result": result,
        }
        try:
            with self._lock:
                self._append(record)
        except AuditUnavailableError:
            log.exception(
                "audit %s: the write HAPPENED and its outcome %r was not recorded; "
                "the attempt stands alone and reads as 'this may have run'",
                attempt["id"],
                result,
            )

    def _open(self) -> int:
        path = self.path
        try:
            handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, FILE_MODE)
        except OSError as exc:
            raise AuditUnavailableError(f"cannot open {path}: {exc}") from exc
        try:
            _checked_mode(handle, path)
        except BaseException:
            os.close(handle)
            raise
        return handle

    def _append(self, record: dict[str, object]) -> None:
        line = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        self._rotate(len(line))
        handle = self._open()
        try:
            _ = os.write(handle, line)
            os.fsync(handle)
        except OSError as exc:
            raise AuditUnavailableError(f"cannot append to {self.path}: {exc}") from exc
        finally:
            os.close(handle)

    def _rotate(self, incoming: int) -> None:
        path = self.path
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return
        except OSError as exc:
            raise AuditUnavailableError(f"cannot measure {path}: {exc}") from exc
        if size + incoming <= self._max_bytes:
            return
        try:
            _ = path.replace(path.with_name(ROTATED_FILENAME))
        except OSError as exc:
            raise AuditUnavailableError(f"cannot rotate {path}: {exc}") from exc


def _checked_mode(handle: int, path: Path) -> None:
    # Checked after every open: a file chmod-ed wider since is put back before it is written.
    try:
        if stat.S_IMODE(os.fstat(handle).st_mode) != FILE_MODE:
            os.fchmod(handle, FILE_MODE)
        mode = stat.S_IMODE(os.fstat(handle).st_mode)
    except OSError as exc:
        raise AuditUnavailableError(f"cannot check the mode of {path}: {exc}") from exc
    if mode != FILE_MODE:
        raise AuditUnavailableError(f"{path} is mode {mode:04o}, expected 0600")


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


def _parse_line(line: str, where: str) -> dict[str, object]:
    try:
        parsed = jsonio.as_dict(jsonio.loads(line))
    except json.JSONDecodeError:
        parsed = None
    # A damaged line is reported in place, never skipped.
    return parsed if parsed is not None else {"phase": UNREADABLE, "ts": "", "at": where}


def records(state_dir: Path, *, limit: int = 50) -> list[dict[str, object]]:
    """The most recent records across both generations, oldest first."""
    base = statedir.ensure_dir(state_dir)
    found: list[dict[str, object]] = []
    for name in (ROTATED_FILENAME, AUDIT_FILENAME):
        for number, line in enumerate(_lines(base / name), start=1):
            if line.strip():
                found.append(_parse_line(line, f"{name}:{number}"))
    return found[-limit:] if limit > 0 else found


def describe(record: dict[str, object]) -> str:
    """One record as one readable line."""
    phase = str(record.get("phase", "?"))
    stamp = str(record.get("ts", ""))[:19]
    head = f"{stamp}  {record.get('endpoint', '?')!s:<7} {record.get('target') or '-'!s:<12}"
    if phase == OUTCOME:
        return f"{head} -> {record.get('result', '?')}"
    if phase != ATTEMPT:
        return f"{stamp}  UNREADABLE RECORD at {record.get('at', '?')}"
    where = record.get("forwarded_for") or record.get("client_ip") or "?"
    who = f"{record.get('actor', '?')} via {record.get('actor_via', '?')} from {where}"
    return f"{head} {who}  {_shown(record)}"


def _shown(record: dict[str, object]) -> str:
    omitted = record.get("payload_omitted")
    if omitted:
        return f"<payload withheld: {omitted}, {record.get('payload_chars', 0)} chars>"
    payload = record.get("payload")
    return "" if payload is None else json.dumps(payload, ensure_ascii=False)
