"""The pane transform: legibility, colour, and the safety rule."""

import re
import time
from pathlib import Path
from types import ModuleType

import pytest
from pydantic import JsonValue, TypeAdapter

from herdr_pwa import glyphs, panetext, patterns
from herdr_pwa.engine import BlockedPrompt, EngineSettings, HerdrClient, WireReply, as_pane_id
from herdr_pwa.panetext import (
    MAX_RUNS_PER_LINE,
    Run,
    as_text_read,
    cells,
    composer_top,
    is_clean_prose,
    pane_lines,
    pane_view,
    plain_text,
    printed_whole,
)
from tests._wire_support import as_reply

ANSI_DIR = Path(__file__).parent / "fixtures" / "ansi"
SNAPSHOT_FIXTURE = Path(__file__).parent / "fixtures" / "snapshot.example.json"
# A wire frame as herdr sends it, which the fake hands back unread.
_FRAME = TypeAdapter(dict[str, JsonValue])

P3 = as_pane_id("w1:p3")
CAPTURES = sorted(ANSI_DIR.glob("*.ansi"))

# A live blocked claude pane on a permission prompt, captured with colour.
BLOCKED = ANSI_DIR / "claude-blocked-bash-permission.ansi"
BLOCKED_DETECTION = ANSI_DIR / "claude-blocked-bash-permission.detection.ansi"
IDLE = ANSI_DIR / "claude-idle-welcome.ansi"

# The row a phone once clipped mid-URL; its tail is the half that decides the answer.
CLIPPED_LINE = (
    "\u276f Run this bash command now: curl -sS https://example.com/ansi1-colour-probe | head -c 20"
)


def flat(text: str, *, strip: bool = True) -> list[str]:
    return [plain_text(line) for line in pane_lines(text, strip=strip)]


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# The captures are what they claim to be
# --------------------------------------------------------------------------


def test_the_fixtures_are_real_ansi_and_not_another_text_capture() -> None:
    """The colour path needs a byte-faithful ANSI capture, not another ``text`` one."""
    for path in (BLOCKED, IDLE):
        raw = read(path)
        assert "\x1b[" in raw, f"{path.name} carries no escape sequences"
        assert re.search(r"\x1b\[38;2;\d+;\d+;\d+m", raw), "no truecolor"
    assert re.search(r"\x1b\[38;5;\d+m", read(IDLE)), "no 256-colour"
    assert "\x1b[1m" in read(IDLE)
    assert "\x1b[2m" in read(IDLE)


def test_the_detection_source_is_not_the_clean_prose_the_decision_assumed() -> None:
    """Detection keeps the box frames and drops the colour, so it is not clean prose."""
    detection = read(BLOCKED_DETECTION)
    assert "\x1b" not in detection, "detection kept its escapes after all"
    assert "─" in detection, "detection stopped returning the raw pane"
    assert not is_clean_prose(detection)
    # ...and the same content is in the visible read, which still has colour.
    assert CLIPPED_LINE in detection
    assert CLIPPED_LINE in "\n".join(flat(read(BLOCKED)))


def test_clean_prose_is_recognised_when_it_really_is_prose() -> None:
    prose = "Do you want to proceed?\n1. Yes\n2. No\n"
    assert is_clean_prose(prose)
    assert not is_clean_prose("")
    assert not is_clean_prose("   \n\n ")


# --------------------------------------------------------------------------
# Legibility
# --------------------------------------------------------------------------


def test_the_command_being_approved_survives_whole() -> None:
    """Decoration goes; the command does not. That is the entire trade."""
    assert CLIPPED_LINE in flat(read(BLOCKED))


def test_horizontal_rules_are_dropped_and_box_frames_collapsed() -> None:
    lines = flat(read(BLOCKED))
    assert lines, "the pane rendered as nothing"
    for line in lines:
        body = line.strip()
        assert not (len(body) >= 4 and set(body) <= set("─━═╌╍-_=~ ")), (
            f"a horizontal rule survived: {line!r}"
        )
        assert not body.startswith(("│", "╭", "╰", "┌", "└")), f"a frame wall survived: {line!r}"
        assert not body.endswith(("│", "╮", "╯", "┐", "┘")), f"a frame wall survived: {line!r}"


def test_the_pane_gets_dramatically_narrower_and_shorter() -> None:
    """The number the decision turns on: 158 columns is not a phone."""
    raw = read(IDLE)
    plain = re.sub(r"\x1b\[[0-9;]*m", "", raw).split("\n")
    after = flat(raw)
    assert max(len(line) for line in plain) > 150, "the capture is not a wide pane"
    assert sum(len(line) for line in after) < sum(len(line) for line in plain) / 2
    assert len(after) < len(plain)
    # Every line that is now wide is prose that will soft-wrap, not a frame.
    for line in after:
        assert "│" not in line[:1]
        assert "─" * 4 not in line


def test_leading_indentation_survives_but_column_padding_does_not() -> None:
    """A frame is removed; an indent is content."""
    assert flat("│  ╭── x ──╮  │")[0] == "x"
    assert flat("    indented")[0] == "    indented"
    assert flat("a" + " " * 40 + "b")[0] == "a b"
    # ...and a line's leading run of spaces is left alone even when it is long.
    assert flat(" " * 10 + "deep")[0] == " " * 10 + "deep"


def test_ascii_dashes_are_a_rule_only_when_the_whole_line_is_one() -> None:
    """``--force`` is prose. A wall of dashes is not."""
    assert flat("-" * 40) == []
    assert flat("run it with --force to skip") == ["run it with --force to skip"]


def test_padding_and_blank_runs_are_squeezed_not_kept() -> None:
    assert flat("a\n\n\n\n\nb") == ["a", "", "b"]
    assert flat("\n\n  text  \n\n\n") == ["  text"]  # indent kept, padding gone
    assert flat("keep me   \n") == ["keep me"]


def test_the_unstripped_mode_keeps_the_decoration() -> None:
    """The unstripped mode stays byte-honest underneath the transform."""
    raw = read(BLOCKED)
    honest = flat(raw, strip=False)
    assert any("╭" in line or "│" in line for line in honest)
    assert max(len(line) for line in honest) > 150


# --------------------------------------------------------------------------
# Colour
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sequence", "expected"),
    [
        ("\x1b[38;2;153;153;153m", "ansi-bright-black"),  # the observed grey
        ("\x1b[38;2;136;136;136m", "ansi-bright-black"),
        ("\x1b[38;2;255;193;7m", "ansi-bright-yellow"),  # "auto mode on" amber
        ("\x1b[38;2;177;185;249m", "ansi-bright-blue"),  # a selected option
        ("\x1b[38;5;2m", "ansi-green"),
        ("\x1b[38;5;196m", "ansi-bright-red"),
        ("\x1b[38;5;240m", "ansi-bright-black"),  # the grey ramp
        ("\x1b[31m", "ansi-red"),
        ("\x1b[92m", "ansi-bright-green"),
        ("\x1b[30m", "ansi-black"),
    ],
)
def test_every_colour_spelling_lands_on_a_slot(sequence: str, expected: str) -> None:
    """24-bit, 256-colour and basic all quantise onto the same sixteen names."""
    runs = pane_lines(f"{sequence}x")[0]
    assert runs[0].cls == expected


def test_hue_is_what_survives_quantisation() -> None:
    """A red -268 beside a green +3141 keeps its meaning after quantising."""
    line = pane_lines("\x1b[38;2;0;200;80m+3141\x1b[0m \x1b[38;2;220;40;40m-268")[0]
    assert [(run.text, run.cls) for run in line] == [
        ("+3141", "ansi-bright-green"),
        (" ", ""),
        ("-268", "ansi-bright-red"),
    ]


def test_attributes_are_classes_from_a_fixed_set() -> None:
    runs = pane_lines("\x1b[1;3;4mx\x1b[22my")[0]
    assert runs[0].cls == "ansi-b ansi-i ansi-u"
    assert runs[1].cls == "ansi-i ansi-u"


def test_a_reset_really_resets() -> None:
    runs = pane_lines("\x1b[1;31mx\x1b[0my")[0]
    assert runs[0].cls == "ansi-red ansi-b"
    assert runs[1].cls == ""


def test_backgrounds_are_parsed_and_thrown_away() -> None:
    """opencode paints every cell; honouring that repaints the mirror as a slab."""
    runs = pane_lines("\x1b[48;2;16;15;15mx")[0]
    assert runs[0].cls == ""
    runs = pane_lines("\x1b[48;2;16;15;15;32mgreen")[0]
    assert runs[0].cls == "ansi-green"


# --------------------------------------------------------------------------
# Safety -- the rule that does not relax
# --------------------------------------------------------------------------


def test_osc_is_discarded_whole_including_a_hyperlink() -> None:
    """OSC 8 is a hyperlink. Passing it would let pane content inject a URL."""
    stream = "\x1b]8;;https://evil.example/steal\x07click me\x1b]8;;\x07 after"
    assert flat(stream) == ["click me after"]


def test_an_unterminated_osc_swallows_its_payload_rather_than_leaking_it() -> None:
    stream = "before\n\x1b]0;title with <script> in it and no terminator"
    assert flat(stream) == ["before"]


@pytest.mark.parametrize(
    "stream",
    [
        "\x1b[2J\x1b[Hcleared",  # erase + cursor home
        "\x1b[?25lcleared",  # private mode set
        "\x1b[1;1Hcleared",  # cursor position
        "\x1b[10Acleared",  # cursor up
        "\x1bPq#0;2;0;0;0cleared\x1b\\cleared",  # DCS (sixel)
        "\x1b(Bcleared",  # charset select
        "\x1b_APC payload\x1b\\cleared",  # APC
    ],
)
def test_every_non_sgr_sequence_is_discarded_not_rendered(stream: str) -> None:
    line = flat(stream)[0]
    assert "cleared" in line
    assert "\x1b" not in line
    assert "[" not in line
    assert "?" not in line


def test_a_malformed_sgr_is_ignored_rather_than_guessed_at() -> None:
    assert pane_lines("\x1b[38;2;999;0;0mx")[0][0].cls == ""
    assert pane_lines("\x1b[notanumbermx")[0][0].cls == ""
    assert pane_lines("\x1b[" + ";".join(["1"] * 200) + "mx")[0][0].cls == ""


def test_the_run_count_per_line_is_capped_but_no_text_is_lost() -> None:
    """A pathological stream is mostly escapes; the DOM must not follow it."""
    stream = "".join(f"\x1b[3{index % 8}mx" for index in range(MAX_RUNS_PER_LINE * 4))
    line = pane_lines(stream)[0]
    assert len(line) <= MAX_RUNS_PER_LINE
    assert len(plain_text(line)) == MAX_RUNS_PER_LINE * 4


def test_control_characters_never_reach_the_output() -> None:
    line = flat("a\x00b\x07c\x08d\re")[0]
    assert line == "abcde"


# --------------------------------------------------------------------------
# The source choice, on the wire
# --------------------------------------------------------------------------


class _Recorder:
    """The transport seam, answering reads and remembering their params."""

    def __init__(self, answers: list[str]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, dict[str, JsonValue]]] = []

    def call(
        self,
        method: str,
        params: dict[str, JsonValue],
        *,
        timeout: float | None = None,
        idempotent: bool = True,
    ) -> WireReply:
        del timeout, idempotent
        self.calls.append((method, dict(params)))
        if method.endswith("snapshot"):
            return as_reply(_FRAME.validate_json(SNAPSHOT_FIXTURE.read_text(encoding="utf-8")))
        text = self.answers.pop(0) if self.answers else ""
        read: dict[str, JsonValue] = {
            "format": params.get("format", "text"),
            "pane_id": "w1:p3",
            "revision": 7,
            "source": params["source"],
            "tab_id": "w1:t1",
            "text": text,
            "truncated": False,
            "workspace_id": "w1",
        }
        return as_reply({"id": "x", "result": {"type": "pane_read", "read": read}})

    def close(self) -> None:
        return None

    @property
    def reads(self) -> list[dict[str, JsonValue]]:
        return [params for method, params in self.calls if method.endswith("read")]


def _client(answers: list[str]) -> tuple[HerdrClient, _Recorder]:
    settings = EngineSettings(socket_path=Path("/nonexistent/herdr.sock"))
    wire = _Recorder(answers)
    return HerdrClient(settings, transport=wire), wire


def test_the_mirror_read_asks_for_colour_and_says_not_to_strip_it() -> None:
    engine, wire = _client(["x"])
    engine.mirror_read(P3, status="idle")
    assert wire.reads[0]["format"] == "ansi"
    assert wire.reads[0]["strip_ansi"] is False
    assert wire.reads[0]["source"] == "visible"


def test_a_plain_read_still_asks_for_text() -> None:
    """Everything that *parses* a read is unchanged: a parser wants no styling."""
    engine, wire = _client(["x"])
    engine.read(P3)
    assert wire.reads[0]["format"] == "text"
    assert wire.reads[0]["strip_ansi"] is True


def test_a_blocked_agent_tries_detection_first() -> None:
    """A blocked agent reads the detection source first."""
    engine, wire = _client(["Do you want to proceed?\n1. Yes\n2. No\n"])
    text = engine.mirror_read(P3, status="blocked")
    assert [call["source"] for call in wire.reads] == ["detection"]
    assert "Do you want to proceed?" in text


def test_a_blocked_agent_falls_back_when_detection_is_not_prose() -> None:
    """Detection that is not prose falls back to the coloured visible read."""
    engine, wire = _client([read(BLOCKED_DETECTION), read(BLOCKED)])
    text = engine.mirror_read(P3, status="blocked")
    assert [call["source"] for call in wire.reads] == ["detection", "visible"]
    assert "\x1b[" in text, "the fallback lost the colour it fell back for"


def test_a_blocked_agent_falls_back_when_detection_is_empty() -> None:
    """Detection is only trustworthy where herdr detected something."""
    engine, wire = _client(["", "visible text"])
    assert engine.mirror_read(P3, status="blocked") == "visible text"
    assert [call["source"] for call in wire.reads] == ["detection", "visible"]


def test_no_other_status_pays_for_the_detection_round_trip() -> None:
    for status in ("idle", "working", "done", "unknown", ""):
        engine, wire = _client(["x"])
        engine.mirror_read(P3, status=status)
        assert [call["source"] for call in wire.reads] == ["visible"], status


def test_the_drill_ins_look_reads_an_unblocked_screen_once_for_its_mirror_and_its_text() -> None:
    engine, wire = _client([read(BLOCKED), read(BLOCKED)])
    look = engine.mirror(P3, status="idle")
    assert [(call["source"], call["format"]) for call in wire.reads] == [("visible", "ansi")]
    assert look.screen == as_text_read(look.shown)
    assert look.screen == engine.screen(P3), "the text a write checks is the text the page claims"


def test_a_blocked_look_reads_the_visible_screen_beside_the_detection_it_draws() -> None:
    engine, wire = _client(["Do you want to proceed?\n1. Yes\n2. No\n", "visible text  "])
    look = engine.mirror(P3, status="blocked")
    assert [call["source"] for call in wire.reads] == ["detection", "visible"]
    assert "Do you want to proceed?" in look.shown
    assert look.screen == "visible text"


def _imports(module: ModuleType) -> list[str]:
    source = Path(str(module.__file__)).read_text(encoding="utf-8")
    return [
        line
        for line in source.splitlines()
        if line.startswith(("import ", "from ")) and not line.startswith("from __future__")
    ]


def test_the_module_stays_a_leaf() -> None:
    """``panetext`` is imported by the engine and the render layer, so it imports neither."""
    imports = _imports(panetext)
    assert imports, imports
    # The glyph sets and the pattern record import nothing of this app's, so they keep it a leaf.
    assert [line for line in imports if "herdr_pwa" in line] == [
        "from herdr_pwa import glyphs",
        "from herdr_pwa.patterns import Rx",
    ]
    for leaf in (glyphs, patterns):
        assert all("herdr_pwa" not in line for line in _imports(leaf)), _imports(leaf)


# --------------------------------------------------------------------------
# Found by an adversarial pass; each was green before.
# --------------------------------------------------------------------------


def test_a_progress_bar_does_not_hang_the_pane() -> None:
    """``_TAIL_FRAME`` was ``(?:[WS]*[BOX]+)*[WS]*$`` -- catastrophic backtracking."""
    bar = "Building [" + "█" * 18 + "░" * 10 + "] 60%"
    # CPU time of this thread, which other processes' load does not inflate: backtracking costs
    # minutes of it here, a linear scan about a millisecond.
    start = time.thread_time()
    pane_lines(bar)
    pane_lines("cpu ▁▂▃▄▅▆▇█" * 8 + " 74%")
    pane_lines("x" + "█" * 400 + "y")
    spent = time.thread_time() - start
    assert spent < 1.0, f"pane text took {spent:.1f}s of CPU; the regex is backtracking"


def test_the_indentation_of_a_diff_survives_a_gutter() -> None:
    """The code being approved must still read as nested."""
    diff = (
        "│   1  - def handler(req):\n"
        "│   2  -     if req.user.is_admin:\n"
        "│   3  -         return deploy(req)\n"
        "│   4  +     return deploy(req)\n"
    )
    rendered = [plain_text(line) for line in pane_lines(diff)]
    assert rendered[1].endswith("-     if req.user.is_admin:")
    assert rendered[2].endswith("-         return deploy(req)")
    assert rendered[2].index("return") > rendered[1].index("if"), (
        "the nesting that says what is guarded by what was flattened"
    )


@pytest.mark.parametrize(
    "line",
    [
        "        return 1",
        " \x1b[32m       return 1",
        "\x1b[32m \x1b[0m       return 1",
    ],
)
def test_indentation_does_not_depend_on_where_a_colour_change_lands(line: str) -> None:
    """The indent is a property of the line, not of run 0."""
    assert plain_text(pane_lines(line)[0]) == "        return 1"


@pytest.mark.parametrize(
    "payload", ["\x1b[H\x1b[2J", "\x00\x00\x00", "\x1b[31m\x1b[0m", "\x1b[?25l"]
)
def test_text_that_renders_to_nothing_is_not_clean_prose(payload: str) -> None:
    """``str.strip()`` only removes whitespace, so control bytes read as content."""
    assert pane_lines(payload) == [] or not any(
        plain_text(line).strip() for line in pane_lines(payload)
    )
    assert is_clean_prose(payload) is False


def test_an_interior_rule_is_collapsed_and_the_words_around_it_survive() -> None:
    """``_INNER_RULE`` flattens long interior rules and keeps a lone glyph."""
    kept = plain_text(pane_lines("Note: ──────── means separator")[0])
    assert "Note:" in kept
    assert "means separator" in kept
    assert "───" not in kept, "a long interior rule should collapse"
    short = plain_text(pane_lines("a ─ b")[0])
    assert "─" in short, "a lone box glyph is content, not a rule"


# --------------------------------------------------------------------------
# The agent's own input box comes off, and its statusline with it: the stats strip replaces it
# --------------------------------------------------------------------------

# A box border as claude draws it: U+2500 across the whole pane.
BORDER = "─" * 158


def box(*, draft: str = "", status: tuple[str, ...] = ()) -> str:
    """A pane tail shaped exactly like claude's: rule, prompt line, rule, status."""
    rows = [BORDER, f"\u276f {draft}".rstrip(), BORDER, *status]
    return "\n".join(rows)


def test_the_agents_input_box_is_stripped_off_the_tail() -> None:
    """The symptom: two input boxes on one screen, one of them a picture."""
    view = pane_view("real output\nmore output\n\n\n" + box(status=("ctx: 16%",)))
    assert [plain_text(line.runs) for line in view.lines] == [
        "real output",
        "more output",
    ]


def test_every_statusline_row_comes_off_with_the_box_not_just_the_first() -> None:
    """A statusline is arbitrary command output and is routinely two or three rows."""
    rows = ("herdr-pwa [main] | ctx: 16% | 12m | Opus 5", "auto mode on", "x")
    view = pane_view("out\n" + box(status=rows))
    assert [plain_text(line.runs) for line in view.lines] == ["out"]


def test_the_statusline_is_matched_by_position_and_never_by_content() -> None:
    """A user-configured statusline is arbitrary. Nothing may be assumed about it."""
    view = pane_view("out\n" + box(status=("\U0001f984 whatever anybody configured",)))
    assert [plain_text(line.runs) for line in view.lines] == ["out"]


def test_a_pane_with_no_input_box_comes_back_untouched() -> None:
    """Conservative: unsure means unchanged, and the caller falls back to the raw."""
    text = "a\nb\nc"
    view = pane_view(text)
    assert [plain_text(line.runs) for line in view.lines] == ["a", "b", "c"]


@pytest.mark.parametrize(
    "tail",
    [
        pytest.param(f"{BORDER}\n\u276f hi\nnot a border", id="no bottom border"),
        pytest.param(f"plain line\n\u276f hi\n{BORDER}\n st", id="no top border"),
        pytest.param(f"{BORDER}\njust text\n{BORDER}\n st", id="no prompt line"),
        pytest.param("one\ntwo", id="nothing at all"),
    ],
)
def test_a_partial_shape_strips_nothing(tail: str) -> None:
    """Only the WHOLE shape matches. A half-match would hide a real line."""
    view = pane_view("keep me\n" + tail)
    kept = [plain_text(line.runs) for line in view.lines]
    assert "keep me" in kept
    assert kept[-1].strip() == tail.splitlines()[-1].strip()


def test_nothing_above_the_top_border_is_ever_removed() -> None:
    """The strip works at the tail only -- there is no scan over the buffer."""
    body = "\n".join(f"line {n}" for n in range(40))
    view = pane_view(body + "\n" + box(draft="d", status=("s",)))
    assert [plain_text(line.runs) for line in view.lines] == body.split("\n")


# --------------------------------------------------------------------------
# Two questions that look like one, and must never be unified
# --------------------------------------------------------------------------


def test_a_heavy_or_double_rule_is_a_rule_but_not_an_input_box_border() -> None:
    """The narrow predicate is U+2500 only, and this is why."""
    for glyph in ("━", "═", "┄", "—", "="):
        rule = glyph * 158
        view = pane_view(f"keep me\n{rule}\n\u276f draft\n{rule}\n st")
        assert "st" in [plain_text(line.runs).strip() for line in view.lines], glyph


def test_a_wide_rule_clips_and_a_labelled_short_one_does_not() -> None:
    """The broad predicate, whose worst case is a cropped separator."""
    clipped = pane_view("+" + "-" * 40 + "+").lines[0]
    assert clipped.clip is True

    labelled = pane_view("-" * 22 + " Building 20 files").lines[0]
    assert labelled.clip is False, "a row carrying words must still wrap"

    prose = pane_view("run it with --force, seriously").lines[0]
    assert prose.clip is False


def test_the_clip_keeps_every_character_of_the_row() -> None:
    """Clipping is layout. Nothing is dropped; the full text stays in the DOM."""
    row = "+" + "-" * 60 + "+"
    line = pane_view(row).lines[0]
    assert line.clip is True
    assert plain_text(line.runs) == row


def test_column_faithful_mode_does_not_clip() -> None:
    """Columns kept means columns kept. Clipping is the wrap mode's device."""
    assert pane_view("+" + "-" * 60 + "+", strip=False).lines[0].clip is False


# --------------------------------------------------------------------------
# Column-faithful is a different server render, not just a class
# --------------------------------------------------------------------------


def test_column_faithful_mode_keeps_the_padding_wrap_mode_collapses() -> None:
    """Which is the whole reason the mode has to reach the server."""
    row = "name" + " " * 20 + "value"
    assert plain_text(pane_view(row).lines[0].runs) == "name value"
    assert plain_text(pane_view(row, strip=False).lines[0].runs) == row


def test_the_chrome_comes_off_in_column_faithful_mode_too() -> None:
    """`strip` is a question about LAYOUT. Where text goes is a different one."""
    view = pane_view("out\n" + box(draft="hi", status=("s",)), strip=False)
    assert [plain_text(line.runs) for line in view.lines] == ["out"]


def test_a_draft_in_the_input_box_is_never_drawn() -> None:
    view = pane_view("out\n" + box(draft="fix the failing test", status=("s",)))
    assert [plain_text(line.runs) for line in view.lines] == ["out"]


# --------------------------------------------------------------------------
# The DoS shape again: _TAIL_FRAME once hung for minutes on a progress bar.
# --------------------------------------------------------------------------


def test_the_new_rule_predicates_are_linear_on_a_pathological_row() -> None:
    """Every regex added since must be checked for the ``(a+)*`` shape."""
    start = time.thread_time()
    for size in (200, 2000, 20000):
        pane_view("█" * size + "x")
        pane_view("-" * size + " tail")
        pane_view(("─" * size) + "\n\u276f d\n" + ("─" * size) + "\ns")
    # CPU time, as above, so a loaded machine cannot fail a linear scan.
    assert time.thread_time() - start < 2.0


# --------------------------------------------------------------------------
# Width is cells, not characters, and len() is wrong in both directions
# --------------------------------------------------------------------------


# Eight CJK characters: len() says 8, the terminal drew 16.
WIDE_LABEL = "テーブルの見出し"

# Four letters carrying eight combining marks: len() says 12, the terminal drew 4.
STACKED_LABEL = "".join(c + "\u0301\u0323" for c in "hoan")


def test_cells_counts_what_the_terminal_drew_not_what_len_reports() -> None:
    """The two directions, stated on their own before anything depends on them."""
    assert len(WIDE_LABEL) == 8
    assert cells(WIDE_LABEL) == 16, "a fullwidth glyph is wider than its length"

    assert len(STACKED_LABEL) == 12
    assert cells(STACKED_LABEL) == 4, "a combining mark is narrower than its length"

    assert cells("plain ascii") == len("plain ascii")
    assert cells("─" * 20) == 20, "box drawing is Ambiguous, and we count it as one"


def test_a_cjk_label_does_not_get_its_row_cropped() -> None:
    """Thirty dashes and eight CJK characters: 83% rule by len(), 65% by cells."""
    row = "-" * 30 + WIDE_LABEL
    assert len("-" * 30) >= len(row) * 0.75, "len() would call this mostly rule"
    assert pane_view(row).lines[0].clip is False


def test_stacked_diacritics_do_not_hide_a_row_that_is_mostly_rule() -> None:
    """The same test, pushed the other way: len() overstates, so the clip is missed."""
    row = "-" * 30 + STACKED_LABEL
    assert len("-" * 30) < len(row) * 0.75, "len() would call this mostly label"
    assert pane_view(row).lines[0].clip is True


def test_the_input_box_border_floor_is_a_cell_count_by_construction() -> None:
    """len() is honest here only while the alphabet is one one-cell glyph."""
    border = panetext.BORDER_GLYPH * panetext.BORDER_MIN_CELLS
    assert cells(border) == len(border) == panetext.BORDER_MIN_CELLS
    assert panetext.is_box_border(border) is True
    assert panetext.is_box_border(border[:-1]) is False


# --------------------------------------------------------------------------
# A label the terminal only printed half of
# --------------------------------------------------------------------------

CORPUS_DIR = Path(__file__).parent / "fixtures"

CAPTURE_SOURCES = ("visible", "recent", "recent-unwrapped", "detection")

# Menu captures and whether their labels are whole; the -wrapped ones were taken 58 columns wide.
MENU_CAPTURES: dict[str, bool] = {
    "capture-blocked-askuserquestion-menu": True,
    "capture-blocked-permission-prompt": True,
    "capture-blocked-permission-write": True,
    "capture-blocked-trust-folder": True,
    "capture-blocked-askuserquestion-wrapped": False,
    "capture-blocked-permission-prompt-wrapped": False,
}


def _menu(name: str, source: str) -> tuple[str, list[str]]:
    """One capture's raw screen and the labels the parser pulled out of it."""
    raw = (CORPUS_DIR / name / f"{source}.txt").read_text(encoding="utf-8")
    return raw, [option.label for option in BlockedPrompt.parse(raw).options]


@pytest.mark.parametrize("source", CAPTURE_SOURCES)
@pytest.mark.parametrize("name", sorted(MENU_CAPTURES))
def test_the_whole_corpus_agrees_about_which_labels_are_whole(name: str, source: str) -> None:
    """The false-positive guard, and the reason this is parametrized over everything."""
    raw, labels = _menu(name, source)
    assert labels, f"{name}/{source}: no options left to check"
    whole = all(printed_whole(raw, label) for label in labels)
    assert whole is MENU_CAPTURES[name], (
        f"{name}/{source}: expected whole={MENU_CAPTURES[name]}, got {whole} "
        f"for {[label for label in labels if not printed_whole(raw, label)]}"
    )


def test_the_wrapped_permission_label_drops_the_host_it_would_allow() -> None:
    """WHY this is not cosmetics, pinned on the capture rather than described."""
    raw, labels = _menu("capture-blocked-permission-prompt-wrapped", "detection")
    assert "Yes, and don\u2019t ask again for: curl -sS" in labels
    assert not any("example.com" in label for label in labels)
    assert "https://example.com/thing" in raw
    assert printed_whole(raw, "Yes, and don\u2019t ask again for: curl -sS") is False
    # The unwrapped sibling of the same dialog is untouched.
    wide, wide_labels = _menu("capture-blocked-permission-prompt", "detection")
    assert all(printed_whole(wide, label) for label in wide_labels)


def test_a_label_the_screen_never_printed_is_refused() -> None:
    """The merged/garbled half of the rule -- the garbled shape once reported."""
    screen = "Do you want to proceed?\n 1. Yes\n 2. No\n"
    assert printed_whole(screen, "No") is True
    assert printed_whole(screen, "Nohift+tab)") is False
    assert printed_whole(screen, "Yes; No") is False


def test_an_odd_token_in_a_label_is_not_what_makes_it_suspicious() -> None:
    """A semicolon in a label is not evidence of anything."""
    screen = " 1. Run: ls; rm -rf ./tmp (careful)\n 2. No\n"
    assert printed_whole(screen, "Run: ls; rm -rf ./tmp (careful)") is True


def test_a_description_row_is_not_a_continuation_when_the_word_would_have_fit() -> None:
    """The width clause, isolated: same geometry, opposite answers."""
    narrow = " 1. I prefer vibrant blues that remind me of clear\n    skies today\n"
    assert printed_whole(narrow, "I prefer vibrant blues that remind me of clear") is False
    wide = (
        " 1. Force-push\n    Force-push the current branch, overwriting history.\n"
        + "-" * 120
        + "\n"
    )
    assert printed_whole(wide, "Force-push") is True


def test_a_label_beside_a_box_has_only_the_room_left_of_the_box() -> None:
    """A preview drawn right of the rows bounds them, so a wrap there is a wrap."""
    screen = (
        "\u276f 1. Grid of equal-width cards    ┌────────┐\n"
        "     with a fixed gutter          │ [a][b] │\n"
        "  2. List                         └────────┘\n" + "-" * 120 + "\n"
    )
    assert printed_whole(screen, "Grid of equal-width cards") is False
    assert printed_whole(screen, "List") is True


def test_the_input_box_starts_at_its_top_border_and_a_dialog_has_none() -> None:
    fixtures = Path(__file__).parent / "fixtures"
    idle = (fixtures / "capture-idle-claude-prompt" / "detection.txt").read_text().splitlines()
    top = composer_top(idle)
    assert panetext.is_box_border(idle[top]), idle[top]
    assert idle[top + 1].startswith("\u276f")
    blocked = (fixtures / "capture-blocked-permission-prompt" / "detection.txt").read_text()
    assert composer_top(blocked.splitlines()) == len(blocked.splitlines())


# --------------------------------------------------------------------------
# The rows under the box are kept for the stats strip, in order, with their colours
# --------------------------------------------------------------------------

RIG_DIR = Path(__file__).parent / "fixtures" / "rig"
STATUSLINE = (RIG_DIR / "claude-statusline.ansi").read_text()


def test_the_rows_under_the_box_are_kept_in_order_and_off_the_viewport() -> None:
    view = pane_view("out\n" + box(status=("ctx: 16% | Opus 5", "auto mode on", "x")))
    assert [plain_text(line.runs) for line in view.lines] == ["out"]
    assert [plain_text(row) for row in view.status] == ["ctx: 16% | Opus 5", "auto mode on", "x"]


def test_a_pane_with_no_box_has_no_statusline() -> None:
    assert pane_view("a\nb\nc").status == []
    assert pane_view("out\n" + box()).status == []


def test_the_statusline_keeps_every_colour_the_terminal_drew() -> None:
    view = pane_view(STATUSLINE)
    assert len(view.status) == 2
    classes = {run.cls for row in view.status for run in row}
    assert {"ansi-d", "ansi-green", "ansi-yellow", "ansi-red", "ansi-cyan"} <= classes
    assert "ansi-magenta ansi-b" in classes


def test_segments_split_at_the_separators_and_keep_them_as_the_person_printed_them() -> None:
    view = pane_view(STATUSLINE)
    pieces = [plain_text(seg) for seg in panetext.segments(view.status[0])]
    assert pieces == [
        "herdr-pwa [main]", "|", "ctx: 22%", "|", "5h: 46%(now)", "|", "7d: 91%(1d5h)", "|",
        "6h34m", "|", "proj: 41%", "|", "+226/-0", "|", "Opus 5.5",
    ]  # fmt: skip


def test_segments_split_across_right_aligning_padding_and_drop_only_the_padding() -> None:
    row = [Run("+1", "ansi-green"), Run(" " * 60), Run("Opus 5.5", "ansi-magenta ansi-b")]
    pieces = panetext.segments(row)
    assert [plain_text(seg) for seg in pieces] == ["+1", "Opus 5.5"]
    assert pieces[1][0].cls == "ansi-magenta ansi-b"


def test_segments_never_split_inside_a_word_or_on_a_bar_inside_one() -> None:
    row = [Run("a|b  c | d")]
    assert [plain_text(seg) for seg in panetext.segments(row)] == ["a|b", "c", "|", "d"]


def test_segments_split_at_bar_and_bullet_separators_between_whitespace_too() -> None:
    row = [Run("a │ b • c │d")]
    assert [plain_text(seg) for seg in panetext.segments(row)] == ["a", "│", "b", "•", "c │d"]


def test_a_private_use_glyph_is_a_break_between_pieces_and_is_not_kept() -> None:
    """Nerd Font icons and powerline arrows draw as blank boxes on a phone: noise, not words."""
    row = [Run(" main", "ansi-blue"), Run("", "ansi-red"), Run(" ctx 22% ")]
    pieces = panetext.segments(row)
    assert [plain_text(seg) for seg in pieces] == ["main", "ctx 22%"]
    assert pieces[0][0].cls == "ansi-blue"
