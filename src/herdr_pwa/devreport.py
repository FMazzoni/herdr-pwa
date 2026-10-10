"""Debug-only reports of the real phone's geometry and navigation, kept as JSONL in the state dir."""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from herdr_pwa import jsonio, statedir

REPORT_FILENAME: Final = "device-report.jsonl"
ROTATED_FILENAME: Final = "device-report.jsonl.1"
# Two generations bound the disk a forgotten flag can use.
MAX_FILE_BYTES: Final = 4 * 1024 * 1024
MAX_EVENTS: Final = 200
SESSION_PATTERN: Final = re.compile(r"[A-Za-z0-9_-]{1,64}")


class ReportError(Exception):
    """The posted report is not the shape the store keeps."""


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


@dataclass(frozen=True, slots=True)
class ReportStore:
    """Appends one line per posted batch; the page's session id ties a load chain together."""

    state_dir: Path
    clock: Callable[[], str] = _utc_now

    @property
    def path(self) -> Path:
        """The live report file."""
        return self.state_dir / REPORT_FILENAME

    def append(self, document: dict[str, object]) -> None:
        """Validate one batch and append it with the server's receive time.

        Raises:
            ReportError: the batch is malformed.
            OSError: the append failed.
            statedir.StateError: the file has the wrong mode.
        """
        session = jsonio.as_str(document.get("session"))
        if session is None or not SESSION_PATTERN.fullmatch(session):
            raise ReportError("'session' must be 1-64 characters of [A-Za-z0-9_-]")
        events = jsonio.as_list(document.get("events"))
        if events is None or len(events) > MAX_EVENTS:
            raise ReportError(f"'events' must be a list of at most {MAX_EVENTS}")
        record = {"received": self.clock(), **document}
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        _ = statedir.ensure_dir(self.state_dir)
        self._rotate()
        statedir.append_line(self.path, line)

    def _rotate(self) -> None:
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            return
        if size > MAX_FILE_BYTES:
            _ = self.path.replace(self.state_dir / ROTATED_FILENAME)


def read_batches(state_dir: Path) -> list[dict[str, object]]:
    """Every readable batch in the live file, oldest first; a torn line is skipped."""
    text = statedir.read_text(state_dir / REPORT_FILENAME) or ""
    batches: list[dict[str, object]] = []
    for line in text.splitlines():
        try:
            parsed = jsonio.as_dict(jsonio.loads(line))
        except json.JSONDecodeError:
            continue
        if parsed is not None:
            batches.append(parsed)
    return batches


def session_events(batches: list[dict[str, object]], session: str = "") -> tuple[str, list[object]]:
    """The chosen session (the latest when empty) and its events in the order they were posted."""
    latest = (jsonio.as_str(batches[-1].get("session")) or "") if batches else ""
    chosen = session or latest
    events: list[object] = []
    for batch in batches:
        if batch.get("session") == chosen:
            events.extend(jsonio.as_list(batch.get("events")) or [])
    return chosen, events


def _detail(event: dict[str, object]) -> str:
    rest = {k: v for k, v in event.items() if k not in {"t", "kind", "page"}}
    return json.dumps(rest, ensure_ascii=False, separators=(", ", ": "))


def format_event(event: object) -> str:
    """One event as a line: milliseconds since the chain began, page number, kind, the rest."""
    mapping = jsonio.as_dict(event)
    if mapping is None:
        return f"? {event!r}"
    at = jsonio.as_float(mapping.get("t"))
    page = jsonio.as_int(mapping.get("page"))
    kind = jsonio.as_str(mapping.get("kind")) or "?"
    stamp = "?" if at is None else f"{at:9.0f}ms"
    return f"{stamp}  p{page if page is not None else '?'}  {kind:<12} {_detail(mapping)}"


def render(state_dir: Path, session: str = "") -> str:
    """The chosen session, readable, or a note saying there is nothing to read."""
    batches = read_batches(state_dir)
    chosen, events = session_events(batches, session)
    if not events:
        return f"no device report in {state_dir / REPORT_FILENAME}\n"
    lines = [f"session {chosen}: {len(events)} event(s)"]
    lines.extend(format_event(e) for e in events)
    return "\n".join(lines) + "\n"
