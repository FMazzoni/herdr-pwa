"""The check a frozen fixture cannot make: read the real logs on this box."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from pydantic import JsonValue, TypeAdapter, ValidationError

from tests.engine_seam import copied_from, head_window

# Opt-in, so the gate stays hermetic: point it at ~/.claude/projects to read the real box.
CORPUS = os.environ.get("HP_CORPUS", "")
ROOT = Path(CORPUS).expanduser()

MEASURED_VERSIONS = (
    "2.1.195",
    "2.1.214",
    "2.1.220",
    "2.1.241",
    "2.1.250",
    "2.1.251",
    "2.1.252",
)


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split(".") if part.isdigit())


def _transcripts() -> list[Path]:
    if not CORPUS:
        return []
    try:
        return sorted(ROOT.glob("*/*.jsonl"))
    except OSError:
        return []


# Measured as written on disk, every field kept: this checks the format, not the reader.
_LINE = TypeAdapter(dict[str, JsonValue])


def _raw_records(window: bytes) -> list[dict[str, JsonValue]]:
    found: list[dict[str, JsonValue]] = []
    for raw in window.split(b"\n")[:-1]:
        try:
            found.append(_LINE.validate_json(raw))
        except ValidationError:
            found.append({})
    return found


def _heads() -> list[tuple[Path, list[dict[str, JsonValue]]]]:
    found: list[tuple[Path, list[dict[str, JsonValue]]]] = []
    for path in _transcripts():
        window = head_window(path)
        if window:
            found.append((path, _raw_records(window)))
    return found


def _versions_seen(heads: list[tuple[Path, list[dict[str, JsonValue]]]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for _, records in heads:
        for record in records:
            found = record.get("version")
            if isinstance(found, str) and found:
                counts[found] = counts.get(found, 0) + 1
                break
    return counts


HEADS = _heads()
SEEN = _versions_seen(HEADS)
WHAT_IT_SAW = f"corpus {ROOT}: {len(HEADS)} transcripts, versions {sorted(SEEN)}"

pytestmark = pytest.mark.corpus


def test_every_declared_parent_resolves_to_a_file_that_exists() -> None:
    """An edge that names a log nothing can open is an edge we are misreading."""
    dangling: list[str] = []
    followed = 0
    for path, _ in HEADS:
        parent = copied_from(path)
        if not parent:
            continue
        followed += 1
        if not (path.parent / f"{parent}.jsonl").is_file():
            dangling.append(f"{path.name} -> {parent}")
    assert not dangling, f"declared parents that do not resolve: {dangling}\n{WHAT_IT_SAW}"
    assert followed or SEEN, WHAT_IT_SAW


def test_the_installed_cli_is_one_the_lineage_reader_was_measured_against() -> None:
    """The version ratchet, which is the half that does not depend on any field."""
    newest = max(SEEN, key=_version, default="")
    assert newest in MEASURED_VERSIONS, (
        f"Claude Code {newest} has never been measured against the lineage "
        f"reader. Force a fork on it and check which edge fields it writes, "
        f"then add {newest} to MEASURED_VERSIONS.\n{WHAT_IT_SAW}"
    )


def test_the_corpus_says_which_versions_it_actually_held() -> None:
    """A check on machine-private state must never pass by having nothing in it."""
    assert SEEN, (
        "the corpus holds transcripts and not one declares a CLI version, so "
        f"the ratchet above cannot see anything at all.\n{WHAT_IT_SAW}"
    )


def _own(records: list[dict[str, JsonValue]]) -> str:
    return next((sid for r in records if isinstance(sid := r.get("sessionId"), str)), "")


def _declared(records: list[dict[str, JsonValue]]) -> str:
    return next(
        (
            sid
            for r in records
            if isinstance(parent := r.get("forkedFrom"), dict)
            and isinstance(sid := parent.get("sessionId"), str)
        ),
        "",
    )


def _carried_by(log: Path, stamp: str) -> bool:
    try:
        return f'"session_id":"{stamp}"'.encode() in log.read_bytes()
    except OSError:
        return False


def test_a_foreign_stamp_on_a_declared_fork_names_the_parent_or_came_with_the_copy() -> None:
    """The measurement behind trusting ``forkedFrom`` over the stamp, and the stamp without it."""
    # Measured on 2.1.289: a fork copies each record's stamp verbatim, its parent's foreign ones too.
    unexplained: list[str] = []
    for path, records in HEADS:
        own, declared = _own(records), _declared(records)
        foreign = {
            stamp for r in records if isinstance(stamp := r.get("session_id"), str) and stamp != own
        }
        parent = path.parent / f"{declared}.jsonl"
        stray = {stamp for stamp in foreign - {declared} if not _carried_by(parent, stamp)}
        if declared and stray:
            unexplained.append(f"{path.name}: forkedFrom={declared} stamps={stray}")
    assert not unexplained, (
        "a provenance stamp on a declared fork is neither its parent nor copied from it: "
        f"{unexplained}; the stamp no longer means the session that wrote the record.\n{WHAT_IT_SAW}"
    )


def test_the_reader_survives_every_real_head_on_this_box() -> None:
    """No crash, no exception, whatever anybody's log actually contains."""
    for path, _ in HEADS:
        assert isinstance(copied_from(path), str), path.name
    assert json.dumps(sorted(SEEN)), WHAT_IT_SAW
