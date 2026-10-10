"""A file change in the Chat tab, computed here and drawn by templates/diff.html."""

from difflib import unified_diff
from typing import Final

from pydantic import BaseModel, Field

from herdr_pwa.jsontypes import JsonObject, as_array, as_object

MAX_DIFF_LINES: Final = 60

MAX_DIFF_COLS: Final = 300

MAX_DIFF_INPUT_LINES: Final = 4000

DIFF_CONTEXT: Final = 2

_ADD: Final = "add"
_DEL: Final = "del"
_SAME: Final = "same"
_HUNK: Final = "hunk"

_OLD_KEYS: Final = ("old_string", "oldString")
_NEW_KEYS: Final = ("new_string", "newString")
_PATH_KEYS: Final = ("file_path", "filePath", "notebook_path", "path")
_WHOLE_KEYS: Final = ("content",)


class DiffLine(BaseModel):
    """One row of a change. ``kind`` is from the closed set below."""

    kind: str = Field(description="``add`` | ``del`` | ``same`` | ``hunk``")
    text: str = ""
    clipped: bool = Field(default=False, description="The row was cut at its width.")


class FileDiff(BaseModel):
    """What one tool call did to one file, ready to draw."""

    lines: list[DiffLine] = Field(default_factory=list)
    added: int = 0
    removed: int = 0
    clipped: bool = Field(default=False, description="Rows past the cap were dropped.")
    counted: bool = Field(default=False, description="Too large to diff; only the counts are real.")
    every: bool = Field(default=False, description="``replace_all``: every occurrence.")
    whole: bool = Field(default=False, description="A write: the file's new contents.")


def _first(params: JsonObject, keys: tuple[str, ...]) -> str | None:
    """The first of *keys* present in *params* whose value is a string."""
    for key in keys:
        value = params.get(key)
        if isinstance(value, str):
            return value
    return None


def _pairs(params: JsonObject) -> tuple[list[tuple[str, str]], bool]:
    """Every (old, new) this call changes, and whether it is a whole-file write."""
    edits = params.get("edits")
    if isinstance(edits, list):
        found = [
            (old, new)
            for edit in map(as_object, as_array(edits))
            if edit is not None
            and (old := _first(edit, _OLD_KEYS)) is not None
            and (new := _first(edit, _NEW_KEYS)) is not None
        ]
        return found, False
    old = _first(params, _OLD_KEYS)
    new = _first(params, _NEW_KEYS)
    if old is not None and new is not None:
        return [(old, new)], False
    whole = _first(params, _WHOLE_KEYS)
    if whole is not None and _first(params, _PATH_KEYS) is not None:
        return [("", whole)], True
    return [], False


def _clip_row(text: str) -> tuple[str, bool]:
    """*text* cut at its width budget; a diff row is never wrapped."""
    if len(text) <= MAX_DIFF_COLS:
        return text, False
    return text[:MAX_DIFF_COLS], True


def _split(text: str) -> list[str]:
    """*text* as lines, with no trailing empty line for a trailing newline."""
    return text.splitlines()


def _rows(pairs: list[tuple[str, str]]) -> tuple[list[DiffLine], int, int, bool]:
    """Every pair as diff rows, with the counts taken from the WHOLE change."""
    rows: list[DiffLine] = []
    added = removed = 0
    counted = False
    for old, new in pairs:
        before, after = _split(old), _split(new)
        if max(len(before), len(after)) > MAX_DIFF_INPUT_LINES:
            # Not diffed: SequenceMatcher over two huge files costs a phone request real time.
            counted = True
            added += len(after)
            removed += len(before)
            continue
        pair_rows = _pair_rows(before, after)
        added += sum(row.kind == _ADD for row in pair_rows)
        removed += sum(row.kind == _DEL for row in pair_rows)
        rows.extend(pair_rows)
    return rows, added, removed, counted


def _pair_rows(before: list[str], after: list[str]) -> list[DiffLine]:
    """One pair's unified diff as clipped rows."""
    rows: list[DiffLine] = []
    for raw in _unified(before, after):
        kind, text = _classify(raw)
        body, cut = _clip_row(text)
        rows.append(DiffLine(kind=kind, text=body, clipped=cut))
    return rows


def _unified(before: list[str], after: list[str]) -> list[str]:
    """The unified diff of two line lists, without its two file headers."""
    return list(unified_diff(before, after, n=DIFF_CONTEXT, lineterm=""))[2:]


def _classify(raw: str) -> tuple[str, str]:
    """One unified-diff row as (kind, text), the marker taken off the front."""
    if raw.startswith("@@"):
        return _HUNK, raw
    if raw.startswith("+"):
        return _ADD, raw[1:]
    if raw.startswith("-"):
        return _DEL, raw[1:]
    return _SAME, raw[1:] if raw.startswith(" ") else raw


def tool_diff(params: object) -> FileDiff | None:
    """What *params* changes, or ``None`` if this call changes no file."""
    # Decided by the arguments, never the tool name, so a new spelling of "edit" needs no branch.
    args = as_object(params)
    if args is None:
        return None
    pairs, whole = _pairs(args)
    if not pairs:
        return None
    rows, added, removed, counted = _rows(pairs)
    if not added and not removed:
        return None
    kept = rows[:MAX_DIFF_LINES]
    return FileDiff(
        lines=kept,
        added=added,
        removed=removed,
        clipped=len(kept) < len(rows),
        counted=counted,
        every=args.get("replace_all") is True,
        whole=whole,
    )
