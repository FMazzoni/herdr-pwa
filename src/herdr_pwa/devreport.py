"""Debug-only reports of the real phone's geometry and navigation, kept as JSONL in the state dir."""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Final

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictFloat,
    StrictInt,
    StrictStr,
    StringConstraints,
    ValidationError,
)

from herdr_pwa import statedir
from herdr_pwa.validation import or_default

REPORT_FILENAME: Final = "device-report.jsonl"
ROTATED_FILENAME: Final = "device-report.jsonl.1"
# Two generations bound the disk a forgotten flag can use.
MAX_FILE_BYTES: Final = 4 * 1024 * 1024
MAX_EVENTS: Final = 200
SESSION_PATTERN: Final = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class Batch(BaseModel):
    """One posted batch: the page's session id and its events; anything else it sends is kept."""

    model_config = ConfigDict(extra="allow", frozen=True)

    session: Annotated[StrictStr, StringConstraints(pattern=f"^{SESSION_PATTERN.pattern}$")]
    events: Annotated[list[JsonValue], Field(max_length=MAX_EVENTS)]


class StoredBatch(BaseModel):
    """One line of the report file, read leniently: a field of the wrong shape reads as empty."""

    model_config = ConfigDict(extra="allow", frozen=True)

    session: Annotated[StrictStr, or_default("")] = ""
    events: Annotated[list[JsonValue], or_default([])] = []


class DeviceEvent(BaseModel):
    """One event as the page reported it; everything but these three is its detail."""

    model_config = ConfigDict(extra="allow", frozen=True)

    t: Annotated[StrictFloat | None, or_default(None)] = None
    page: Annotated[StrictInt | None, or_default(None)] = None
    kind: Annotated[StrictStr, or_default("?")] = "?"


@dataclass(frozen=True, slots=True)
class ReportStore:
    """Appends one line per posted batch; the page's session id ties a load chain together."""

    state_dir: Path
    clock: Callable[[], str] = _utc_now

    @property
    def path(self) -> Path:
        """The live report file."""
        return self.state_dir / REPORT_FILENAME

    def append(self, batch: Batch) -> None:
        """Append one batch with the server's receive time; raises OSError when it cannot."""
        line = batch.model_copy(update={"received": self.clock()}).model_dump_json() + "\n"
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


def _batch(line: str) -> StoredBatch | None:
    try:
        return StoredBatch.model_validate_json(line)
    except ValidationError:
        return None


def read_batches(state_dir: Path) -> list[StoredBatch]:
    """Every readable batch in the live file, oldest first; a torn line is skipped."""
    text = statedir.read_text(state_dir / REPORT_FILENAME) or ""
    return [batch for batch in map(_batch, text.splitlines()) if batch is not None]


def session_events(batches: list[StoredBatch], session: str = "") -> tuple[str, list[JsonValue]]:
    """The chosen session (the latest when empty) and its events in the order they were posted."""
    chosen = session or (batches[-1].session if batches else "")
    if not chosen:
        return "", []
    events: list[JsonValue] = []
    for batch in batches:
        if batch.session == chosen:
            events.extend(batch.events)
    return chosen, events


def format_event(event: JsonValue) -> str:
    """One event as a line: milliseconds since the chain began, page number, kind, the rest."""
    try:
        parsed = DeviceEvent.model_validate(event)
    except ValidationError:
        return f"? {event!r}"
    stamp = "?" if parsed.t is None else f"{parsed.t:9.0f}ms"
    page = "?" if parsed.page is None else parsed.page
    detail = json.dumps(parsed.model_extra or {}, ensure_ascii=False, separators=(", ", ": "))
    return f"{stamp}  p{page}  {parsed.kind:<12} {detail}"


def render(state_dir: Path, session: str = "") -> str:
    """The chosen session, readable, or a note saying there is nothing to read."""
    batches = read_batches(state_dir)
    chosen, events = session_events(batches, session)
    if not events:
        return f"no device report in {state_dir / REPORT_FILENAME}\n"
    lines = [f"session {chosen}: {len(events)} event(s)"]
    lines.extend(format_event(e) for e in events)
    return "\n".join(lines) + "\n"
