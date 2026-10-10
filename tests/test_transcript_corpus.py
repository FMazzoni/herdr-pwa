"""The check a frozen fixture cannot make: read the real logs on this box.

The regression is not that anybody wrote bad code. The continuation
walk is correct and its tests are green; Claude Code **2.1.241 stopped writing
the field it reads**, and the fixture that should have noticed is a captured
2.1.214 head, frozen, still carrying it. The feature became a silent no-op and
the suite could not tell. Somebody's chat froze for ninety minutes instead.

So this file reads ``~/.claude/projects`` itself. Two things are asserted, and
neither may depend on the field under test -- selecting transcripts by "declares
a fork or a compaction" is what the first draft did, and a 2.1.241 foreground
fork declares neither, so the very shape that broke would have been invisible to
its own regression test.

Per this repo's rule for checks that read machine-private state, it FAIL-SKIPS
and never fail-passes: an absent or unreadable corpus skips with a reason, and
every failure names the CLI versions it actually saw, so a stale box cannot make
it green by having nothing new in it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from herdr_pwa.engine.transcript import (
    _copied_from,  # noqa: PLC2701 - the edge extractor is the subject
    _head_records,  # noqa: PLC2701
    _head_window,  # noqa: PLC2701
)

ROOT = Path(os.environ.get("HERDR_PWA_CORPUS", "~/.claude/projects")).expanduser()

MEASURED_VERSIONS = (
    "2.1.195",
    "2.1.214",
    "2.1.220",
    "2.1.241",
    "2.1.250",
    "2.1.251",
    "2.1.252",
)
"""Every Claude Code release the lineage reader has actually been measured against.

Raising this is not a formality. It means somebody forced a fork on the new CLI
(``scripts/fork_probe.py``, with the socket redirected) and checked which edge
fields it writes.

2.1.251 was measured and the measurement is FOREGROUND ONLY, which
is the bar this docstring sets and no more: a real logged-in fork
(``--use-my-credentials``), haiku answering in both runs, and a child carrying no
edge field at all -- no ``forkedFrom``, no ``sessionKind``, and the parent's id
appearing in zero records. That is the same thing the reader sees on 2.1.250,
where the ``sessionKind == "bg"`` gate discards a foreground fork's inherited
stamp anyway. What is NOT measured on 2.1.251 is the background handoff, because
a ``--bg`` fork inherits the transient daemon's pane id no matter where the
socket points and this repo has no safe way to force one. The bg
shape is the reader's only working edge source, so if it moves, this tuple will
not be what catches it.

2.1.252 was measured later and is the THIRD release in a row to behave this
way: a real logged-in foreground fork, haiku answering in both runs (4 answers in
the child, 2 in the parent), and a child carrying none of ``forkedFrom``,
``session_id``, ``sessionKind``, ``compactMetadata`` or ``subtype``. The probe
reports that as NO EDGE, which reads like an alarm and is not one here -- it is
the same absence 2.1.250 and 2.1.251 already record, and the ``sessionKind ==
"bg"`` gate would discard a foreground fork's stamp regardless. Background is
unmeasured on 2.1.252 for the same reason it is on 2.1.251.

2.1.243 and 2.1.246 are deliberately ABSENT. Both ran on this box and both are
in the corpus, but only as ordinary sessions -- nobody forked one, and a
``--bg`` fork cannot be pinned to an older release, because the transient daemon
spawns whichever binary is installed (a probe invoked the 2.1.243 and 2.1.246
binaries and got two 2.1.250 transcripts back). Observing a version is not
measuring it, and this tuple only ever gates the NEWEST, so listing them would
buy nothing and claim something untrue.
"""


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split(".") if part.isdigit())


def _transcripts() -> list[Path]:
    try:
        return sorted(ROOT.glob("*/*.jsonl"))
    except OSError:
        return []


def _heads() -> list[tuple[Path, list[dict[str, Any]]]]:
    found: list[tuple[Path, list[dict[str, Any]]]] = []
    for path in _transcripts():
        window = _head_window(path)
        if window:
            found.append((path, list(_head_records(window))))
    return found


def _versions_seen(heads: list[tuple[Path, list[dict[str, Any]]]]) -> dict[str, int]:
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
    reason=f"no readable transcript corpus at {ROOT}; this check reads the real box",
)


@corpus
def test_every_declared_parent_resolves_to_a_file_that_exists() -> None:
    """An edge that names a log nothing can open is an edge we are misreading.

    This is the integrity half, and it is what catches a field whose MEANING
    changed while its name stayed -- an id that is no longer a session id, or a
    parent named from outside its own directory.
    """
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
    """The version ratchet, which is the half that does not depend on any field.

    A rename cannot hide from this: the newest transcript on the box carries the
    CLI version that wrote it, and if that is a release nobody has re-measured,
    this fails in the right module and names it -- instead of the chat quietly
    freezing for somebody reading it from their phone.
    """
    newest = max(SEEN, key=_version, default="")
    assert newest in MEASURED_VERSIONS, (
        f"Claude Code {newest} has never been measured against the lineage "
        f"reader. Force a fork on it and check which edge fields it writes "
        f"(scripts/fork_probe.py), record what you find in "
        f"docs/modules/engine/transcript/_copied_from.md, then add {newest} to "
        f"MEASURED_VERSIONS.\n{WHAT_IT_SAW}"
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
    """The measurement the resolution half's coverage claim actually rests on.

    The ``session_id`` KEY is ordinary -- 121 of 134 head windows carry it --
    and almost always names the file's own session, declaring nothing. What is
    rare is a FOREIGN value: two files on this box, one of which also declares
    ``forkedFrom`` and agrees with it exactly.

    That agreement is the evidence that the stamp means what this reader takes
    it to mean. If a future CLI ever produces a foreign stamp that CONTRADICTS a
    declared fork, the reader is following a pointer whose meaning has changed
    under it, and the whole resolution half is unsafe until somebody re-measures.
    """
    disagreements: list[str] = []
    for path, records in HEADS:
        own = next(
            (r["sessionId"] for r in records if isinstance(r.get("sessionId"), str)),
            "",
        )
        declared = next(
            (
                r["forkedFrom"]["sessionId"]
                for r in records
                if isinstance(r.get("forkedFrom"), dict)
                and isinstance(r["forkedFrom"].get("sessionId"), str)
            ),
            "",
        )
        foreign = {
            r["session_id"]
            for r in records
            if isinstance(r.get("session_id"), str) and r["session_id"] != own
        }
        if declared and foreign and foreign != {declared}:
            disagreements.append(f"{path.name}: forkedFrom={declared} stamps={foreign}")
    assert not disagreements, (
        "a provenance stamp contradicts a declared fork: "
        f"{disagreements}. Re-read docs/modules/engine/transcript/"
        f"_stamped_parent.md -- its coverage claim no longer holds.\n{WHAT_IT_SAW}"
    )


@corpus
def test_the_reader_survives_every_real_head_on_this_box() -> None:
    """No crash, no exception, whatever anybody's log actually contains."""
    for path, _ in HEADS:
        assert isinstance(_copied_from(path), str), path.name
    assert json.dumps(sorted(SEEN)), WHAT_IT_SAW
