"""A file change in the Chat tab: the parser, which owns the closed set of row kinds and every cap."""

import pytest
from pydantic import JsonValue

from herdr_pwa.diff import (
    MAX_DIFF_COLS,
    MAX_DIFF_INPUT_LINES,
    MAX_DIFF_LINES,
    EditArgs,
    FileDiff,
    tool_diff,
)

# A kind outside the closed set would reach a template with no branch for it.
KINDS = {"add", "del", "same", "hunk"}

EDIT: dict[str, JsonValue] = {
    "file_path": "/repo/a.txt",
    "old_string": "one\ntwo\nthree\n",
    "new_string": "one\nTWO\nthree\n",
}


def diff(params: JsonValue) -> FileDiff | None:
    """What a harness's tool arguments change, read as the adapters read them."""
    return tool_diff(EditArgs.model_validate(params) if isinstance(params, dict) else None)


def kinds(params: JsonValue) -> list[str]:
    change = diff(params)
    assert change is not None
    return [line.kind for line in change.lines]


def test_an_edit_becomes_one_added_and_one_removed_row() -> None:
    change = diff(EDIT)
    assert change is not None
    assert (change.added, change.removed) == (1, 1)
    assert set(kinds(EDIT)) <= KINDS
    assert [line.text for line in change.lines if line.kind == "del"] == ["two"]
    assert [line.text for line in change.lines if line.kind == "add"] == ["TWO"]


def test_the_marker_is_off_the_text_so_the_template_owns_the_gutter() -> None:
    """A marker left in the text could not be coloured or told apart by a screen reader."""
    change = diff(EDIT)
    assert change is not None
    assert not any(
        line.text.startswith(("+", "-")) for line in change.lines if line.kind in ("add", "del")
    )


@pytest.mark.parametrize(
    "params",
    [
        {"filePath": "/repo/a.txt", "oldString": "a\n", "newString": "b\n"},
        {"file_path": "/repo/a.txt", "old_string": "a\n", "new_string": "b\n"},
    ],
)
def test_both_harnesses_spellings_reach_the_same_code(params: dict[str, JsonValue]) -> None:
    """Claude Code snake-cases, opencode camel-cases. Neither is named."""
    change = diff(params)
    assert change is not None
    assert (change.added, change.removed) == (1, 1)


def test_a_multi_edit_carries_every_edit_in_order() -> None:
    change = diff(
        {
            "file_path": "/repo/a.txt",
            "edits": [
                {"old_string": "a\n", "new_string": "A\n"},
                {"old_string": "b\n", "new_string": "B\n"},
            ],
        }
    )
    assert change is not None
    assert (change.added, change.removed) == (2, 2)
    assert [line.text for line in change.lines if line.kind == "add"] == ["A", "B"]
    assert not change.whole


def test_a_multi_edit_is_not_mistaken_for_a_whole_file_write() -> None:
    """`edits` is checked first, or a MultiEdit's file_path would read as a whole-file write."""
    change = diff(
        {
            "file_path": "/repo/a.txt",
            "content": "the whole file\n" * 50,
            "edits": [{"old_string": "a\n", "new_string": "A\n"}],
        }
    )
    assert change is not None
    assert not change.whole
    assert change.added == 1


def test_a_write_is_the_whole_file_and_says_so() -> None:
    change = diff({"file_path": "/repo/a.txt", "content": "one\ntwo\n"})
    assert change is not None
    assert change.whole
    assert (change.added, change.removed) == (2, 0)
    assert kinds({"file_path": "/repo/a.txt", "content": "one\n"}) == ["hunk", "add"]


def test_content_without_a_path_is_not_a_write() -> None:
    """`content` is a common argument name; a write also says which file."""
    assert diff({"content": "hello\n"}) is None


def test_replace_all_is_carried_because_the_counts_alone_would_lie() -> None:
    """A replace_all's diff is ONE occurrence, however many it touched."""
    change = diff(EDIT | {"replace_all": True})
    assert change is not None
    assert change.every
    plain = diff(EDIT)
    assert plain is not None
    assert not plain.every


@pytest.mark.parametrize(
    "params",
    [
        None,
        "a string",
        {},
        {"file_path": "/repo/a.txt"},
        {"command": "ls -la"},
        {"pattern": "TODO", "path": "/repo"},
        {"file_path": "/repo/a.txt", "old_string": "a\n"},
        {"edits": [{"old_string": "a\n"}, "not a dict"]},
    ],
)
def test_a_call_that_changes_no_file_has_no_diff(params: JsonValue) -> None:
    """Every other tool in the harness comes through here."""
    assert diff(params) is None


def test_an_edit_that_changes_nothing_has_no_diff() -> None:
    """A row reading '+0 -0' is noise, not a receipt."""
    assert diff({"file_path": "/a", "old_string": "x\n", "new_string": "x\n"}) is None


def test_a_huge_change_stops_at_the_row_cap() -> None:
    change = diff(
        {
            "file_path": "/repo/a.txt",
            "old_string": "".join(f"{n}\n" for n in range(400)),
            "new_string": "".join(f"x{n}\n" for n in range(400)),
        }
    )
    assert change is not None
    assert len(change.lines) == MAX_DIFF_LINES
    assert change.clipped


def test_the_counts_are_the_whole_change_and_never_the_clipped_part() -> None:
    """A clipped diff still answers 'how big was that' exactly."""
    change = diff(
        {
            "file_path": "/repo/a.txt",
            "old_string": "".join(f"{n}\n" for n in range(400)),
            "new_string": "".join(f"x{n}\n" for n in range(400)),
        }
    )
    assert change is not None
    assert (change.added, change.removed) == (400, 400)
    assert change.added > len(change.lines)


def test_one_enormous_row_is_clipped_and_never_wrapped() -> None:
    """A minified bundle is one line of tens of thousands of characters."""
    change = diff({"file_path": "/repo/a.js", "old_string": "a\n", "new_string": "z" * 90000})
    assert change is not None
    assert all(len(line.text) <= MAX_DIFF_COLS for line in change.lines)
    assert any(line.clipped for line in change.lines)


def test_past_the_input_cap_the_change_is_counted_and_not_diffed() -> None:
    """difflib over two huge files costs real time; the counts are still the real ones."""
    big = "".join(f"line {n}\n" for n in range(MAX_DIFF_INPUT_LINES + 5))
    change = diff({"file_path": "/repo/big.txt", "content": big})
    assert change is not None
    assert change.counted
    assert change.lines == []
    assert change.added == MAX_DIFF_INPUT_LINES + 5
