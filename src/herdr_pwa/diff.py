"""A file change in the Chat tab, computed here and drawn by templates/diff.html."""

from difflib import unified_diff
from typing import Annotated, Final

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from herdr_pwa.validation import Flag, Text, or_default

MAX_DIFF_LINES: Final = 60

MAX_DIFF_COLS: Final = 300

MAX_DIFF_INPUT_LINES: Final = 4000

DIFF_CONTEXT: Final = 2

_ADD: Final = "add"
_DEL: Final = "del"
_SAME: Final = "same"
_HUNK: Final = "hunk"

_OLD = AliasChoices("old_string", "oldString")
_NEW = AliasChoices("new_string", "newString")


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


class _Edit(BaseModel):
    """One replacement of a multi-edit call."""

    model_config = ConfigDict(extra="ignore")

    old: Text = Field(default=None, validation_alias=_OLD)
    new: Text = Field(default=None, validation_alias=_NEW)


class EditArgs(BaseModel):
    """A tool call's arguments as far as a file change goes, however a harness spells them."""

    # Every harness names these its own way; what none of them says is simply None.
    model_config = ConfigDict(extra="ignore")

    old: Text = Field(default=None, validation_alias=_OLD)
    new: Text = Field(default=None, validation_alias=_NEW)
    content: Text = None
    file_path: Text = Field(default=None, validation_alias=AliasChoices("file_path", "filePath"))
    notebook_path: Text = None
    path: Text = None
    edits: Annotated[list[Annotated[_Edit | None, or_default(None)]] | None, or_default(None)] = (
        None
    )
    replace_all: Flag = None

    @property
    def names_a_file(self) -> bool:
        """Whether any spelling of a file's path is present."""
        return any(p is not None for p in (self.file_path, self.notebook_path, self.path))


def _pairs(args: EditArgs) -> tuple[list[tuple[str, str]], bool]:
    """Every (old, new) this call changes, and whether it is a whole-file write."""
    if args.edits is not None:
        found = [
            (edit.old, edit.new)
            for edit in args.edits
            if edit is not None and edit.old is not None and edit.new is not None
        ]
        return found, False
    if args.old is not None and args.new is not None:
        return [(args.old, args.new)], False
    if args.content is not None and args.names_a_file:
        return [("", args.content)], True
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


def tool_diff(args: EditArgs | None) -> FileDiff | None:
    """What *args* changes, or ``None`` if this call changes no file."""
    # Decided by the arguments, never the tool name, so a new spelling of "edit" needs no branch.
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
        every=args.replace_all is True,
        whole=whole,
    )
