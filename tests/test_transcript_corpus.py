"""The check a frozen fixture cannot make: read the real logs on this box."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from herdr_pwa.engine.transcript import (
    _copied_from,  # pyright: ignore[reportPrivateUsage]
    _head_records,  # pyright: ignore[reportPrivateUsage]
    _head_window,  # pyright: ignore[reportPrivateUsage]
)
from herdr_pwa.jsontypes import JsonObject, get_object

# Opt-in, so the gate stays hermetic: point it at ~/.claude/projects to read the real box.
CORPUS = os.environ.get("HERDR_PWA_CORPUS", "")
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


def _heads() -> list[tuple[Path, list[JsonObject]]]:
    found: list[tuple[Path, list[JsonObject]]] = []
    for path in _transcripts():
        window = _head_window(path)
        if window:
            found.append((path, list(_head_records(window))))
    return found


def _versions_seen(heads: list[tuple[Path, list[JsonObject]]]) -> dict[str, int]:
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

corpus = pytest.mark.skipif(
    not HEADS,
    reason=f"no readable transcript corpus at {CORPUS or '(HERDR_PWA_CORPUS unset)'}; this check reads the real box",
)


@corpus
def test_every_declared_parent_resolves_to_a_file_that_exists() -> None:
    """An edge that names a log nothing can open is an edge we are misreading."""
    dangling: list[str] = []
    followed = 0
    for path, _ in HEADS:
        parent = _copied_from(path)
        if not parent:
            continue
        followed += 1
        if not (path.parent / f"{parent}.jsonl").is_file():
            dangling.append(f"{path.name} -> {parent}")
    assert not dangling, f"declared parents that do not resolve: {dangling}\n{WHAT_IT_SAW}"
    assert followed or SEEN, WHAT_IT_SAW


@corpus
def test_the_installed_cli_is_one_the_lineage_reader_was_measured_against() -> None:
    """The version ratchet, which is the half that does not depend on any field."""
    newest = max(SEEN, key=_version, default="")
    assert newest in MEASURED_VERSIONS, (
        f"Claude Code {newest} has never been measured against the lineage "
        f"reader. Force a fork on it and check which edge fields it writes, "
        f"then add {newest} to MEASURED_VERSIONS.\n{WHAT_IT_SAW}"
    )


@corpus
def test_the_corpus_says_which_versions_it_actually_held() -> None:
    """A check on machine-private state must never pass by having nothing in it."""
    assert SEEN, (
        "the corpus holds transcripts and not one declares a CLI version, so "
        f"the ratchet above cannot see anything at all.\n{WHAT_IT_SAW}"
    )


@corpus
def test_a_foreign_provenance_stamp_never_disagrees_with_a_declared_fork() -> None:
    """The measurement the resolution half's coverage claim actually rests on."""
    disagreements: list[str] = []
    for path, records in HEADS:
        own = next(
            (sid for r in records if isinstance(sid := r.get("sessionId"), str)),
            "",
        )
        declared = next(
            (
                sid
                for r in records
                if (parent := get_object(r, "forkedFrom")) is not None
                and isinstance(sid := parent.get("sessionId"), str)
            ),
            "",
        )
        foreign = {
            stamp for r in records if isinstance(stamp := r.get("session_id"), str) and stamp != own
        }
        if declared and foreign and foreign != {declared}:
            disagreements.append(f"{path.name}: forkedFrom={declared} stamps={foreign}")
    assert not disagreements, (
        "a provenance stamp contradicts a declared fork: "
        f"{disagreements}; the stamped-parent coverage claim no longer holds.\n{WHAT_IT_SAW}"
    )


@corpus
def test_the_reader_survives_every_real_head_on_this_box() -> None:
    """No crash, no exception, whatever anybody's log actually contains."""
    for path, _ in HEADS:
        assert isinstance(_copied_from(path), str), path.name
    assert json.dumps(sorted(SEEN)), WHAT_IT_SAW
