"""A file change in the Chat tab: the parser, and the wire.

The measured gap this file guards: ``_tool_summary`` picks ONE identifying
argument off a fixed ordered list, ``file_path`` wins, and neither
``old_string`` nor ``new_string`` is on the list -- so an Edit rendered exactly
like a Read and the change never reached the phone at all.

Two halves, deliberately. The top is the parser, which owns the closed set of
node kinds and every cap. The bottom is the WIRE: real records on disk, through
the real reader and the real route, inspected in the real response body -- the
shape the escaping proof used, because a template test renders through the
same environment the server does and so proves the template rather than the
wiring.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from herdr_pwa.diff import (
    MAX_DIFF_COLS,
    MAX_DIFF_INPUT_LINES,
    MAX_DIFF_LINES,
    tool_diff,
)

pytest.importorskip("fastapi.testclient")

from test_chat import (  # noqa: E402
    agent_url,
    build,
    human,
    live_agent,
    transcript_for,
)

#: The whole closed set. A kind outside it means a node reached the template
#: that templates/diff.html has no branch for, which loses content silently.
KINDS = {"add", "del", "same", "hunk"}

EDIT = {
    "file_path": "/repo/a.txt",
    "old_string": "one\ntwo\nthree\n",
    "new_string": "one\nTWO\nthree\n",
}


def diff_block(body: str) -> str:
    """Just the change, so the app's own shell is not under test here."""
    start = body.index('class="diff"')
    return body[start : body.index("</details>", start)]


def kinds(params: object) -> list[str]:
    change = tool_diff(params)
    assert change is not None
    return [line.kind for line in change.lines]


# --------------------------------------------------------------------------
# The parser: decided by the arguments, never by the tool's name
# --------------------------------------------------------------------------


def test_an_edit_becomes_one_added_and_one_removed_row() -> None:
    change = tool_diff(EDIT)
    assert change is not None
    assert (change.added, change.removed) == (1, 1)
    assert set(kinds(EDIT)) <= KINDS
    assert [line.text for line in change.lines if line.kind == "del"] == ["two"]
    assert [line.text for line in change.lines if line.kind == "add"] == ["TWO"]


def test_the_marker_is_off_the_text_so_the_template_owns_the_gutter() -> None:
    """The whole point of a tree. A row whose text still began with '-' would
    put the marker in the DATA, where a stylesheet cannot colour it and a
    screen reader cannot separate it from the line."""
    change = tool_diff(EDIT)
    assert change is not None
    assert not any(
        line.text.startswith(("+", "-"))
        for line in change.lines
        if line.kind in ("add", "del")
    )


@pytest.mark.parametrize(
    "params",
    [
        {"filePath": "/repo/a.txt", "oldString": "a\n", "newString": "b\n"},
        {"file_path": "/repo/a.txt", "old_string": "a\n", "new_string": "b\n"},
    ],
)
def test_both_harnesses_spellings_reach_the_same_code(params: dict) -> None:
    """Claude Code snake-cases, opencode camel-cases. Neither is named."""
    change = tool_diff(params)
    assert change is not None
    assert (change.added, change.removed) == (1, 1)


def test_a_multi_edit_carries_every_edit_in_order() -> None:
    change = tool_diff(
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
    """`edits` is checked FIRST. A MultiEdit also carries a file_path, and a
    fall-through would render the target file as one enormous addition."""
    change = tool_diff(
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
    change = tool_diff({"file_path": "/repo/a.txt", "content": "one\ntwo\n"})
    assert change is not None
    assert change.whole
    assert (change.added, change.removed) == (2, 0)
    assert kinds({"file_path": "/repo/a.txt", "content": "one\n"}) == ["hunk", "add"]


def test_content_without_a_path_is_not_a_write() -> None:
    """`content` is a common argument name. A file was written only when the
    call also says WHICH file."""
    assert tool_diff({"content": "hello\n"}) is None


def test_replace_all_is_carried_because_the_counts_alone_would_lie() -> None:
    """A replace_all's diff is ONE occurrence, however many it touched."""
    change = tool_diff(EDIT | {"replace_all": True})
    assert change is not None
    assert change.every
    assert tool_diff(EDIT) is not None and not tool_diff(EDIT).every  # type: ignore[union-attr]


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
def test_a_call_that_changes_no_file_has_no_diff(params: object) -> None:
    """Every other tool in the harness comes through here."""
    assert tool_diff(params) is None


def test_an_edit_that_changes_nothing_has_no_diff() -> None:
    """A row reading '+0 -0' is noise, not a receipt."""
    assert (
        tool_diff({"file_path": "/a", "old_string": "x\n", "new_string": "x\n"}) is None
    )


# --------------------------------------------------------------------------
# The caps (the same rule, applied to a new kind of text)
# --------------------------------------------------------------------------


def test_a_huge_change_stops_at_the_row_cap() -> None:
    change = tool_diff(
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
    """The magnitude must survive the cap: a clipped diff still has to answer
    'how big was that' exactly, or the degrade path lies about the change."""
    change = tool_diff(
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
    change = tool_diff(
        {"file_path": "/repo/a.js", "old_string": "a\n", "new_string": "z" * 90000}
    )
    assert change is not None
    assert all(len(line.text) <= MAX_DIFF_COLS for line in change.lines)
    assert any(line.clipped for line in change.lines)


def test_past_the_input_cap_the_change_is_counted_and_not_diffed() -> None:
    """difflib over two very large files is the one place this can cost a
    phone request real time. The counts are still the real ones."""
    big = "".join(f"line {n}\n" for n in range(MAX_DIFF_INPUT_LINES + 5))
    change = tool_diff({"file_path": "/repo/big.txt", "content": big})
    assert change is not None
    assert change.counted
    assert change.lines == []
    assert change.added == MAX_DIFF_INPUT_LINES + 5


# --------------------------------------------------------------------------
# The wire: a real Edit, on disk, through the real route
# --------------------------------------------------------------------------

FIXTURE = Path(__file__).parent / "fixtures" / "transcript"
REAL_EDIT = "00000000-0000-4000-8000-000000000015"


def edited(uuid: str, params: dict) -> dict:
    return {
        "type": "assistant",
        "uuid": uuid,
        "timestamp": "2026-08-26T04:56:04.502Z",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": f"t-{uuid}", "name": "Edit", "input": params}
            ],
        },
    }


def render(tmp_path: Path, params: dict) -> str:
    agent = live_agent()
    transcript_for(tmp_path, agent.session, [human("h1", "go"), edited("e1", params)])
    http, _ = build(tmp_path)
    return http.get(agent_url(agent.session, agent.target)).text


def test_the_captured_edit_renders_its_real_change(tmp_path: Path) -> None:
    """The frame on the preview wall, over the wire. These are the bytes the
    agent really sent -- 351 of old_string, 674 of new -- not a fixture
    somebody wrote to make a renderer look good."""
    agent = live_agent()
    records = [
        json.loads(line)
        for line in (FIXTURE / "claude" / f"{REAL_EDIT}.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    transcript_for(tmp_path, agent.session, records)
    http, _ = build(tmp_path)
    body = http.get(agent_url(agent.session, agent.target)).text
    # The Read row and the Edit row name the SAME file. Before the file-change view they
    # rendered identically, which is the whole complaint.
    assert body.count("/home/user/projects/herdr-pwa/README.md") >= 2
    assert 'class="diff__line diff__line--del"' in body
    assert 'class="diff__line diff__line--add"' in body
    assert "tap the answer to a blocked" in body


def test_a_deletion_is_not_a_markdown_bullet(tmp_path: Path) -> None:
    """THE TRAP THIS FEATURE EXISTS TO AVOID. prose.py is markdown-to-tree and
    in markdown a line starting with '-' is a LIST ITEM: every deletion would
    silently become a bullet with its marker eaten. A diff gets its own
    renderer, so a removed line is a row with a gutter mark and never an <li>."""
    body = render(tmp_path, EDIT)
    block = body[
        body.index('class="diff"') : body.index(
            "</details>", body.index('class="diff"')
        )
    ]
    assert "<li" not in block and "<ul" not in block
    assert (
        '<span class="diff__mark">-</span><span class="diff__text">two</span>' in block
    )


def test_the_gutter_mark_is_in_the_markup_and_not_only_in_colour(
    tmp_path: Path,
) -> None:
    """Colour alone fails: a colour-blind reader, a greyscale screenshot, a
    forced-colours mode, and the stylesheet failing to load are four ways to
    lose it. The mark is a text node, and it is not aria-hidden."""
    block = diff_block(render(tmp_path, EDIT))
    assert '<span class="diff__mark">+</span>' in block
    assert '<span class="diff__mark">-</span>' in block
    assert "aria-hidden" not in block


def test_the_gist_is_still_the_path(tmp_path: Path) -> None:
    """After Collie's summarizeToolInput. The one-line summary
    of a tool row is the identifying argument; a diff in it would be a
    multi-line blob in a row whose whole job is to collapse."""
    body = render(tmp_path, EDIT)
    row = body[body.index("<summary") : body.index("</summary>")]
    assert "/repo/a.txt" in row
    assert "diff__line" not in row
    assert "\n" not in row.split('class="tool__arg"')[1].split("</span>")[0]


def test_the_collapsed_row_carries_its_two_counts(tmp_path: Path) -> None:
    """An Edit was indistinguishable from a Read before its body was opened.
    The stat is a fixed two-number badge, not a diff, and it does not grow."""
    body = render(tmp_path, EDIT)
    row = body[body.index("<summary") : body.index("</summary>")]
    assert '<span class="diff__add">+1</span>' in row
    assert "&minus;1" in row


def test_a_hostile_change_is_escaped_over_http(tmp_path: Path) -> None:
    """A diff is the bytes of whatever repository the agent is working in, and
    this app can type at a terminal. There is no |safe anywhere in the path."""
    body = render(
        tmp_path,
        {
            "file_path": "/repo/<img src=x onerror=alert(1)>",
            "old_string": "safe\n",
            "new_string": "<script>alert('x')</script>\n",
        },
    )
    assert "<script>alert(" not in body
    assert "<img src=x" not in body
    assert "&lt;img src=x onerror=alert(1)&gt;" in body
    assert "&lt;script&gt;alert(" in body


def test_no_tag_outside_the_allow_list_reaches_the_page(tmp_path: Path) -> None:
    """templates/diff.html is the whole allow-list; this is it, asserted."""
    body = render(tmp_path, EDIT)
    block = body[
        body.index('class="diff"') : body.index(
            "</details>", body.index('class="diff"')
        )
    ]
    tags = {t.lower() for t in re.findall(r"<\s*/?\s*([a-zA-Z][a-zA-Z0-9]*)", block)}
    assert tags <= {"div", "p", "span"}


# --------------------------------------------------------------------------
# The palette: green and red, out of the app's own tokens
# --------------------------------------------------------------------------

CSS = Path(__file__).parent.parent / "src" / "herdr_pwa" / "static" / "app.css"


def diff_rules() -> str:
    """Every rule whose selector names the change block, as one string."""
    body = re.sub(r"/\*.*?\*/", "", CSS.read_text(encoding="utf-8"), flags=re.S)
    found = [
        match.group(0)
        for match in re.finditer(r"[^{}]+\{[^{}]*\}", body)
        if "diff__" in match.group(0).split("{")[0]
    ]
    assert found, "no rule in app.css draws the change block"
    return "\n".join(found)


def test_the_two_colours_are_aliases_and_never_hexes() -> None:
    """A live theme picker was proposed and this app has ONE palette. A diff
    written in #22863a would be a colour that stops matching the app the day
    the picker lands -- somebody else's bug, filed against somebody else."""
    body = CSS.read_text(encoding="utf-8")
    assert "--diff-add: var(--working);" in body
    assert "--diff-del: var(--blocked);" in body


def test_no_rule_in_the_change_block_names_a_colour_of_its_own() -> None:
    """The block may only reach the palette through a token."""
    assert not re.findall(r"#[0-9A-Fa-f]{3,8}\b", diff_rules())
    for rule in ("--diff-add", "--diff-del"):
        assert f"var({rule})" in diff_rules()
