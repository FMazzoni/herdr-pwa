"""The pane transform: legibility, colour, and the safety rule.

Three groups of tests, and only the first is about how it looks.

1. **Legibility.** A 158-column desktop pane has to be readable on a 390px
   phone. The decoration strip is asserted against the two live ANSI captures in
   ``tests/fixtures/ansi/``, not against hand-written strings -- a hand-written
   pane is a pane whose author already knew what the parser does.
2. **Colour.** SGR becomes classes out of a closed set, chosen by arithmetic.
3. **Safety, which is the one that matters.** Pane text is attacker-
   influenceable: a coding agent renders whatever is in the repo it is working
   in, and this app can type into that terminal. Nothing the stream contains may
   reach markup or an attribute. The corpus test at the bottom feeds the real
   captures plus a hostile stream through the whole render and asserts that no
   ``<`` originating in the stream survives as markup.
"""

from __future__ import annotations

import itertools
import json
import math
import re
import time
from pathlib import Path
from typing import Any

import pytest

from herdr_pwa import panetext
from herdr_pwa.engine import as_pane_id
from herdr_pwa.engine import client as engine_client
from herdr_pwa.panetext import (
    MAX_RUNS_PER_LINE,
    SLOT_CLASSES,
    cells,
    is_clean_prose,
    pane_lines,
    pane_view,
    plain_text,
    printed_whole,
)
from herdr_pwa.render import STATIC_DIR, render_mirror
from herdr_pwa.settings import Settings

ANSI_DIR = Path(__file__).parent / "fixtures" / "ansi"
SNAPSHOT_FIXTURE = Path(__file__).parent / "fixtures" / "snapshot.example.json"

#: One pane address, through the door a raw string has to go through.
P3 = as_pane_id("w1:p3")
CAPTURES = sorted(ANSI_DIR.glob("*.ansi"))

#: The live blocked pane legibility was argued over: the same permission prompt the
#: reviewer measured clipped at 390px, recaptured with colour on 2026-08-19.
BLOCKED = ANSI_DIR / "claude-blocked-bash-permission.ansi"
#: The same pane read through ``--source detection``.
BLOCKED_DETECTION = ANSI_DIR / "claude-blocked-bash-permission.detection.ansi"
IDLE = ANSI_DIR / "claude-idle-welcome.ansi"

#: The line the whole ticket is about. At 390px the old ``white-space: pre``
#: mirror showed ``❯ Run this bash command now: curl -sS https://exampl`` and no
#: scrollbar, so a reader could not tell it was cut. The tail is the half that
#: decides the answer.
CLIPPED_LINE = (
    "❯ Run this bash command now: curl -sS https://example.com/ansi1-colour-probe | head -c 20"
)

HOSTILE_MARKUP = '<script>alert(1)</script>"><img src=x onerror=alert(2)>'


def flat(text: str, **kwargs: Any) -> list[str]:
    return [plain_text(line) for line in pane_lines(text, **kwargs)]


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# The captures are what they claim to be
# --------------------------------------------------------------------------


def test_the_fixtures_are_real_ansi_and_not_another_text_capture() -> None:
    """Colour needs a byte-faithful ANSI capture; every earlier one is ``text``.

    Asserted rather than trusted, because a capture taken through the wrong flag
    looks exactly like a correct one until a test silently stops exercising the
    parser.
    """
    for path in (BLOCKED, IDLE):
        raw = read(path)
        assert "\x1b[" in raw, f"{path.name} carries no escape sequences"
        assert re.search(r"\x1b\[38;2;\d+;\d+;\d+m", raw), "no truecolor"
    assert re.search(r"\x1b\[38;5;\d+m", read(IDLE)), "no 256-colour"
    assert "\x1b[1m" in read(IDLE) and "\x1b[2m" in read(IDLE)


def test_the_detection_source_is_not_the_clean_prose_the_decision_assumed() -> None:
    """The measurement that shapes the blocked path, pinned as a fixture.

    The decision preferred ``--source detection`` for a blocked agent on the
    grounds that it answers in clean prose. Read live off a blocked claude pane
    on 2026-08-19 (herdr 0.8.0) it does not: it answers with the whole
    158-column pane, box frames intact, and with every escape sequence removed.
    Routing blocked panes there unconditionally would keep the decoration and
    throw away the colour.

    This is a *tripwire*, not a complaint. If herdr's detection source ever does
    start answering in prose, this test fails and the fallback in
    ``HerdrClient.mirror_read`` starts taking the path the decision described --
    which is exactly the day somebody should be told.
    """
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
    """The number the decision turns on: 158 columns is not a phone.

    The strip cannot get every line under 48 columns and is not trying to --
    a hard-wrapped 158-column paragraph is still a paragraph, and CSS
    ``pre-wrap`` finishes the job. What it *can* do is throw out everything
    that is only there to draw a box, which is most of the bytes.
    """
    raw = read(IDLE)
    plain = re.sub(r"\x1b\[[0-9;]*m", "", raw).split("\n")
    after = flat(raw)
    assert max(len(line) for line in plain) > 150, "the capture is not a wide pane"
    assert sum(len(line) for line in after) < sum(len(line) for line in plain) / 2
    assert len(after) < len(plain)
    # Every line that is now wide is prose that will soft-wrap, not a frame.
    for line in after:
        assert "│" not in line[:1] and "─" * 4 not in line


def test_leading_indentation_survives_but_column_padding_does_not() -> None:
    """A frame is removed; an indent is content.

    The difference matters on a pane showing a diff or a numbered menu, where
    the indent is the only thing left saying what is nested under what. So the
    leading trim only fires when there is a box character in front, and the
    padding squeeze deliberately skips a line's own indent.
    """
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
    """The raw read stays byte-honest underneath the transform; legibility owns the strip."""
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
    """The diff semantics: a red -268 beside a green +3141."""
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
    """opencode paints every cell; honouring that repaints the mirror as a slab.

    The parse still has to *consume* the background's parameters, or the ``2``
    of a truecolor background would be re-read as SGR 2 and dim the pane.
    """
    runs = pane_lines("\x1b[48;2;16;15;15mx")[0]
    assert runs[0].cls == ""
    runs = pane_lines("\x1b[48;2;16;15;15;32mgreen")[0]
    assert runs[0].cls == "ansi-green"


def test_the_stylesheet_defines_exactly_the_slots_the_parser_can_emit() -> None:
    """A class the parser can produce and the CSS does not define is invisible
    colour; a class the CSS defines and the parser cannot produce is dead code.
    Both are silent, so both are asserted."""
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    defined = set(re.findall(r"\.mirror \.(ansi-[a-z-]+)\s*\{", css))
    assert defined == set(SLOT_CLASSES) | {"ansi-b", "ansi-d", "ansi-i", "ansi-u"}


def test_the_slot_colours_clear_the_contrast_floor_they_claim() -> None:
    """The colour decision, recomputed rather than trusted to a comment.

    Lift, not render-as-sent: ANSI red 205,0,0 is 3.1:1 on ``--frame`` and ANSI
    black is 1.2:1, so "honest" would mean rendering some runs as very nearly
    nothing. Every slot is checked at the AA floor against the mirror's own
    ground, which is what makes the quantisation worth its cost.

    The values moved into ``--ansi-*`` tokens when the theme work made the ground
    something a theme could move, so this resolves one hop. The FIRST match of
    each name is the dark palette's, which is where this ``--frame`` comes from
    too; every theme's whole set is recomputed in ``tests/test_theme.py``.
    """
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    ground = re.search(r"--frame:\s*(#[0-9A-Fa-f]{6})", css)
    assert ground is not None
    for slot in SLOT_CLASSES:
        found = re.search(rf"\.mirror \.{slot}\s*\{{\s*color:\s*var\((--ansi-[a-z-]+)\)", css)
        assert found is not None, f"{slot} is not painted from a token"
        painted = re.search(rf"{found.group(1)}:\s*(#[0-9A-Fa-f]{{6}})", css)
        assert painted is not None, f"{found.group(1)} is not defined"
        assert _contrast(painted.group(1), ground.group(1)) >= 4.5, slot


# --------------------------------------------------------------------------
# The CHROME palette, which is where the defects actually were
#
# The slot test above guards sixteen colours the ANSI parser can emit. It never
# looked at the app's own tokens, and that is how ``--unknown`` came to be the
# same six hex digits as ``--ink-3``: an agent STATUS painted in the grey the
# meta row uses for ordinary text, with ``--idle`` 1.32:1 away from it. Two
# adjacent states, indistinguishable, in the app whose whole job is showing
# state at a glance.
#
# Everything below ENUMERATES out of app.css. A hand-written list of pairs
# covers only what existed the day somebody wrote it; the token added next
# month is exactly the one nobody adds to the list.
#
# Two assertions, and they measure DIFFERENT things:
#
#   1. contrast against the ground -- a luminance ratio, WCAG 1.4.3. Nothing in
#      this app reaches large-text size (14pt/18pt are ~18.5px/24px and the body
#      is 15px), so 4.5 binds everywhere and there is no 3:1 tier to fall back on.
#   2. separation between statuses -- a perceptual distance in OKLab. A ratio
#      cannot do this job: ``--blocked`` and ``--done`` sit at 1.03:1 and are
#      plainly different colours, while ``--idle`` and ``--unknown`` sat at
#      1.32:1 and were the same grey twice. Luminance ratio and "can you tell
#      these apart" are not the same question, and only the second one is what a
#      status colour is for.
#
# REJECTED, so nobody re-derives it (see CLAUDE.md on writing down the
# counterexample): a third gate requiring every status to be far from every body
# ink. It cannot be satisfied. The body inks are blue-greys spanning the whole
# luminance band a colour must live in to clear 4.5:1 on these dark surfaces, so
# the search returns EMPTY for any cool status at any chroma. The consequence is
# a real design rule rather than a test: statuses carry a hue, furniture does
# not. ``--unknown`` is amber for that reason and not for a decorative one.
# --------------------------------------------------------------------------

#: OKLab distance below which two agent states read as the same colour. The
#: shipped palette's ``--idle``/``--unknown`` pair measured 0.071 here and
#: ``--unknown``/``--ink-3`` measured 0.000; the repaired palette's closest pair
#: is 0.173. The floor sits between them with room on both sides, deliberately:
#: tight against the defect it exists to catch, loose enough that a legitimate
#: retint does not have to fight it.
STATUS_SEPARATION = 0.13


def _app_css() -> str:
    return (STATIC_DIR / "app.css").read_text(encoding="utf-8")


def _tokens(css: str) -> dict[str, str]:
    """Every custom property in the stylesheet whose value is a plain hex."""
    return dict(re.findall(r"(--[a-z0-9-]+):\s*(#[0-9A-Fa-f]{6})\s*;", css))


def _used_as(css: str, prop: str) -> set[str]:
    """Tokens used as the WHOLE value of ``prop``.

    Whole value on purpose. ``color-mix(in srgb, var(--blocked) 14%, transparent)``
    is a tint of a status, not a surface anything is drawn on, and counting it as
    a ground would demand 4.5:1 against a colour no text ever meets.
    """
    pattern = rf"(?<![\w-]){re.escape(prop)}[a-z-]*:\s*var\((--[a-z0-9-]+)\)\s*;"
    return set(re.findall(pattern, css))


def _statuses(css: str) -> set[str]:
    """The agent-state colours, read off the state dots that paint them.

    ``.dot--working { background: var(--working) }`` is the app naming its own
    status colours. A state added later brings its dot with it and lands in this
    set for free -- which is the whole point of not writing the list down here.
    """
    return set(
        re.findall(
            r"\.dot--[a-z0-9-]+\s*\{[^}]*background:\s*var\((--[a-z0-9-]+)\)",
            css,
        )
    )


def _oklab(value: str) -> tuple[float, float, float]:
    """sRGB hex to OKLab. Ottosson's matrices, transcribed."""
    red, green, blue = (
        c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
        for c in (int(value[i : i + 2], 16) / 255 for i in (1, 3, 5))
    )
    long = (0.4122214708 * red + 0.5363325363 * green + 0.0514459929 * blue) ** (1 / 3)
    medium = (0.2119034982 * red + 0.6806995451 * green + 0.1073969566 * blue) ** (1 / 3)
    short = (0.0883024619 * red + 0.2817188376 * green + 0.6299787005 * blue) ** (1 / 3)
    return (
        0.2104542553 * long + 0.7936177850 * medium - 0.0040720468 * short,
        1.9779984951 * long - 2.4285922050 * medium + 0.4505937099 * short,
        0.0259040371 * long + 0.7827717662 * medium - 0.8086757660 * short,
    )


def _separation(one: str, two: str) -> float:
    return math.dist(_oklab(one), _oklab(two))


def test_every_ink_clears_the_text_floor_on_every_surface_it_is_drawn_on() -> None:
    """WCAG 1.4.3 at AA, enumerated rather than named.

    Inks are the tokens the stylesheet passes to ``color``; surfaces are the
    tokens it passes to ``background``, less the status swatches -- a state dot
    is an 8px circle with no text in it, and holding it to a TEXT floor would be
    asserting the wrong thing about the wrong element.
    """
    css = _app_css()
    tokens = _tokens(css)
    statuses = _statuses(css)
    inks = {t for t in _used_as(css, "color") if t in tokens}
    surfaces = {t for t in _used_as(css, "background") if t in tokens} - statuses

    # An enumeration that quietly matched nothing would pass forever.
    assert inks, "no ink tokens found -- the extraction has drifted from the CSS"
    assert surfaces, "no surface tokens found -- the extraction has drifted"

    for ink in sorted(inks):
        for surface in sorted(surfaces):
            ratio = _contrast(tokens[ink], tokens[surface])
            assert ratio >= 4.5, (
                f"{ink} {tokens[ink]} on {surface} {tokens[surface]} is {ratio:.2f}:1"
            )


def test_no_two_agent_states_are_painted_the_same_colour() -> None:
    """The assertion no contrast-against-ground check could ever make.

    ``--unknown`` was byte-identical to ``--ink-3`` and 1.32:1 from ``--idle``,
    and every ratio-against-the-background test in this file was green while it
    was true. Two states the user cannot tell apart is a lie the app tells about
    itself, and it is measured BETWEEN the colours or not at all.
    """
    css = _app_css()
    tokens = _tokens(css)
    statuses = sorted(t for t in _statuses(css) if t in tokens)

    assert len(statuses) >= 2, f"expected several state colours, found {statuses}"

    for one, two in itertools.combinations(statuses, 2):
        gap = _separation(tokens[one], tokens[two])
        assert gap >= STATUS_SEPARATION, (
            f"{one} {tokens[one]} and {two} {tokens[two]} are {gap:.4f} apart "
            f"in OKLab, under the {STATUS_SEPARATION} floor -- two agent states "
            f"that look the same"
        )


def _contrast(one: str, two: str) -> float:
    def luminance(value: str) -> float:
        channels = [int(value[i : i + 2], 16) / 255 for i in (1, 3, 5)]
        linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    high, low = sorted((luminance(one), luminance(two)), reverse=True)
    return (high + 0.05) / (low + 0.05)


# --------------------------------------------------------------------------
# Safety -- the rule that does not relax
# --------------------------------------------------------------------------


def test_osc_is_discarded_whole_including_a_hyperlink() -> None:
    """OSC 8 is a hyperlink. Passing it would let pane content inject a URL."""
    stream = "\x1b]8;;https://evil.example/steal\x07click me\x1b]8;;\x07 after"
    assert flat(stream) == ["click me after"]
    assert "evil.example" not in render_mirror(None, text=stream, session="s")


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
    assert "[" not in line and "?" not in line


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


#: Everything a run may be styled with: the sixteen quantised colour slots and
#: the four attributes. Every other class the mirror emits is written by the
#: template itself, and each test below names the ones it expects.
_ALLOWED_CLASSES = set(SLOT_CLASSES) | {"ansi-b", "ansi-d", "ansi-i", "ansi-u"}


@pytest.mark.parametrize("path", CAPTURES, ids=lambda p: p.stem)
def test_no_angle_bracket_from_the_stream_ever_reaches_the_output(path: Path) -> None:
    """The corpus test the colour decision asks for, over every captured pane plus a
    hostile one spliced into it.

    The assertion is deliberately blunt: take the rendered fragment, remove
    every tag this template is allowed to emit, and require that what is left
    contains no ``<`` at all. Anything the stream contributed is either an
    escaped entity or nothing.
    """
    # Spliced at the FRONT as well as the back. The tail of a
    # claude pane is its input box and its statusline, and those no longer
    # render inside the <pre> -- they are promoted to chrome. Hostile text
    # appended to the end therefore lands in the statusline, which the test
    # below covers; this one is about the viewport, so the viewport is where
    # the payload has to be.
    raw = HOSTILE_MARKUP + "\n\x1b[31m" + HOSTILE_MARKUP + "\n" + read(path) + "\n" + HOSTILE_MARKUP
    html = render_mirror(None, text=raw, session="s")
    viewport = html.split('<pre class="mirror"', 1)[1].split("</pre>", 1)[0]
    stripped = re.sub(r"</?span(?: class=\"[a-z0-9_ -]*\")?>", "", viewport)
    stripped = stripped.split(">", 1)[1]  # the <pre>'s own remaining attributes
    assert "<" not in stripped
    assert "&lt;script&gt;" in viewport
    assert "onerror" not in re.sub(r"&[a-z]+;", "", stripped) or "&" in viewport


@pytest.mark.parametrize("path", CAPTURES, ids=lambda p: p.stem)
def test_every_class_the_render_emits_comes_from_the_closed_set(path: Path) -> None:
    """The real guarantee behind the test above: nothing is interpolated.

    A stream cannot influence an attribute if the only attribute values that
    exist are these twenty names, plus the two the TEMPLATE writes for itself.

    ``mirror__row`` and ``mirror__row--rule`` are on that list because the clip work
    made every pane row a block element so one of them could clip. They are
    literals in ``mirror.html``, chosen by a boolean panetext computed, and no
    substring of the stream reaches either -- which is the property this test
    actually guards. The set is still closed; it is closed at twenty-two.
    """
    allowed = _ALLOWED_CLASSES | {"mirror__row", "mirror__row--rule"}
    html = render_mirror(None, text=read(path), session="s")
    viewport = html.split('<pre class="mirror"', 1)[1].split("</pre>", 1)[0]
    for value in re.findall(r'<span class="([^"]*)"', viewport):
        assert set(value.split()) <= allowed, value


@pytest.mark.parametrize("path", CAPTURES, ids=lambda p: p.stem)
def test_the_promoted_statusline_is_escaped_like_the_viewport(path: Path) -> None:
    """Terminal text moved OUT of the <pre>, so the guarantee moves with it.

    The statusline is matched by POSITION -- whatever sits under the input
    box's bottom border -- so it is exactly as attacker-influenceable as the
    viewport is: anything that can make an agent print eight lines can choose
    what lands there. Same rule, tested separately because it is a different
    element and an escaping miss in a new one is still RCE.
    """
    raw = read(path) + "\n" + HOSTILE_MARKUP
    html = render_mirror(None, text=raw, session="s")
    if 'class="panestatus"' not in html:
        pytest.skip("this capture has no input box to promote a statusline out of")
    block = html.split('class="panestatus"', 1)[1].split("</div>", 1)[0]
    stripped = re.sub(r"</?span(?: class=\"[a-z0-9_ -]*\")?>", "", block)
    stripped = stripped.split(">", 1)[1]
    assert "<" not in stripped
    for value in re.findall(r'<span class="([^"]*)"', block):
        assert set(value.split()) <= _ALLOWED_CLASSES | {"panestatus__row"}, value


def test_render_mirror_still_shows_plain_text_unchanged() -> None:
    """A caller that has not switched to the colour read loses nothing but colour.

    "Unchanged" is about STYLING, not about markup: now every row is
    wrapped in a block so one row can clip on its own, so the assertion is that
    no *styled* span exists -- an uncoloured pane emits no ``ansi-`` class at
    all, exactly as it did before colour.
    """
    html = render_mirror(None, text=HOSTILE_MARKUP, session="s")
    viewport = html.split('<pre class="mirror"', 1)[1].split("</pre>", 1)[0]
    assert "ansi-" not in viewport
    assert "&lt;script&gt;" in viewport


# --------------------------------------------------------------------------
# The source choice, on the wire
# --------------------------------------------------------------------------


class _Recorder:
    """The transport seam, answering reads and remembering their params.

    Same shape as ``tests/test_client.py``'s fake -- ``call(method, params)`` --
    because that is the interface ``HerdrClient`` actually issues against, and a
    recorder that agreed with the client but not with the wire would prove
    nothing about the flags that reach herdr.
    """

    def __init__(self, answers: list[str]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float | None = None,
        idempotent: bool = True,
    ) -> dict[str, Any]:
        self.calls.append((method, dict(params)))
        if method.endswith("snapshot"):
            return json.loads(SNAPSHOT_FIXTURE.read_text(encoding="utf-8"))
        text = self.answers.pop(0) if self.answers else ""
        return {
            "id": "x",
            "result": {
                "type": "pane_read",
                "read": {
                    "format": params.get("format", "text"),
                    "pane_id": "w1:p3",
                    "revision": 7,
                    "source": params["source"],
                    "tab_id": "w1:t1",
                    "text": text,
                    "truncated": False,
                    "workspace_id": "w1",
                },
            },
        }

    def close(self) -> None:
        return None

    @property
    def reads(self) -> list[dict[str, Any]]:
        return [params for method, params in self.calls if method.endswith("read")]


def _client(answers: list[str]) -> tuple[engine_client.HerdrClient, _Recorder]:
    settings = Settings(_env_file=None)  # type: ignore
    wire = _Recorder(answers)
    return engine_client.HerdrClient(settings, transport=wire), wire


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
    """The legibility hybrid, as decided."""
    engine, wire = _client(["Do you want to proceed?\n1. Yes\n2. No\n"])
    text = engine.mirror_read(P3, status="blocked")
    assert [call["source"] for call in wire.reads] == ["detection"]
    assert "Do you want to proceed?" in text


def test_a_blocked_agent_falls_back_when_detection_is_not_prose() -> None:
    """...and the guard on it, which is what today's herdr actually needs.

    Two round trips, both flat reads. The alternative -- trusting detection --
    renders the blocked pane with its box drawing intact and its colour gone.
    """
    engine, wire = _client([read(BLOCKED_DETECTION), read(BLOCKED)])
    text = engine.mirror_read(P3, status="blocked")
    assert [call["source"] for call in wire.reads] == ["detection", "visible"]
    assert "\x1b[" in text, "the fallback lost the colour it fell back for"


def test_a_blocked_agent_falls_back_when_detection_is_empty() -> None:
    """ "detection is only trustworthy where herdr detected something" -- the
    decision named this cost, so it is handled rather than hoped about."""
    engine, wire = _client(["", "visible text"])
    assert engine.mirror_read(P3, status="blocked") == "visible text"
    assert [call["source"] for call in wire.reads] == ["detection", "visible"]


def test_no_other_status_pays_for_the_detection_round_trip() -> None:
    for status in ("idle", "working", "done", "unknown", ""):
        engine, wire = _client(["x"])
        engine.mirror_read(P3, status=status)
        assert [call["source"] for call in wire.reads] == ["visible"], status


def test_the_module_stays_a_leaf() -> None:
    """``panetext`` is imported by the engine *and* by the render layer, so it
    must not import either -- the same rule ``protocol.py`` keeps, for the same
    reason."""
    source = Path(panetext.__file__).read_text(encoding="utf-8")
    imports = [
        line
        for line in source.splitlines()
        if line.startswith(("import ", "from ")) and not line.startswith("from __future__")
    ]
    assert imports and all("herdr_pwa" not in line for line in imports), imports


# --------------------------------------------------------------------------
# What the wave-9 adversarial pass found. Each of these was green before.
# --------------------------------------------------------------------------


def test_a_progress_bar_does_not_hang_the_pane():
    """``_TAIL_FRAME`` was ``(?:[WS]*[BOX]+)*[WS]*$`` -- catastrophic backtracking.

    A run of n box characters can be split across the outer ``*`` in 2^(n-1)
    ways and a failing match tries all of them. ``_INNER_RULE`` collapses long
    runs of line-drawing glyphs first, which hid it, but ``_BOX`` also covers
    the BLOCK ELEMENTS (``█ ░ ▒ ▓ ▁▂▃``) and those arrive uncollapsed.

    Measured on the original: 20 blocks 0.13s, 24 blocks 2.0s, 26 blocks 8.2s,
    a 28-cell progress bar over two minutes. The mirror re-polls every 2s, so
    each hung request holds a threadpool worker and the whole app -- feed,
    mirror, composer -- stops answering. Triggered by anything an agent prints:
    a uv/pip/docker progress bar, a sparkline, a file in the repo it is reading.
    """
    bar = "Building [" + "█" * 18 + "░" * 10 + "] 60%"
    start = time.perf_counter()
    pane_lines(bar)
    pane_lines("cpu ▁▂▃▄▅▆▇█" * 8 + " 74%")
    pane_lines("x" + "█" * 400 + "y")
    elapsed = time.perf_counter() - start
    assert elapsed < 1.0, f"pane text took {elapsed:.1f}s; the regex is backtracking"


def test_the_indentation_of_a_diff_survives_a_gutter():
    """The code being approved must still read as nested.

    ``_PADDING`` was ``{4,}`` and four spaces IS code indentation. Behind a
    gutter (``| 2 -     if ...``) the code's own indent is *interior* padding,
    so every line came out flush: a change removing an ``is_admin`` guard and
    de-nesting a ``return`` read as five unrelated statements at one level.
    That is the "approve what you cannot read" failure this module exists to
    prevent, produced by the module itself.
    """
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
def test_indentation_does_not_depend_on_where_a_colour_change_lands(line):
    """The indent is a property of the line, not of run 0.

    Computed per-run, an SGR inside the leading whitespace split it in two and
    only the first half was exempt -- so the same source line kept or lost its
    indent depending on where the colour changed. A diff renderer colours the
    changed span, which puts SGR mid-indent constantly.
    """
    assert plain_text(pane_lines(line)[0]) == "        return 1"


@pytest.mark.parametrize(
    "payload", ["\x1b[H\x1b[2J", "\x00\x00\x00", "\x1b[31m\x1b[0m", "\x1b[?25l"]
)
def test_text_that_renders_to_nothing_is_not_clean_prose(payload):
    """``str.strip()`` only removes whitespace, so control bytes read as content.

    Consequence before the fix: a blocked agent -- the one the user drilled in
    for -- rendered an empty viewport with a composer under it, no notice, and
    the ``visible`` read that would have worked was never attempted.
    """
    assert pane_lines(payload) == [] or not any(
        plain_text(line).strip() for line in pane_lines(payload)
    )
    assert is_clean_prose(payload) is False


def test_an_interior_rule_is_collapsed_and_the_words_around_it_survive():
    """``_INNER_RULE`` had no test at all -- deleting it left 948 tests green.

    It is a live, information-destroying transform: it is what flattens ``tree``
    output and what would eat a separator a human wrote in prose. Pin both
    directions so a change to it is a decision rather than an accident.
    """
    kept = plain_text(pane_lines("Note: ──────── means separator")[0])
    assert "Note:" in kept and "means separator" in kept
    assert "───" not in kept, "a long interior rule should collapse"
    short = plain_text(pane_lines("a ─ b")[0])
    assert "─" in short, "a lone box glyph is content, not a rule"


# --------------------------------------------------------------------------
# The agent's own input box comes off, its statusline is promoted
# --------------------------------------------------------------------------

#: A box border as claude actually draws it: U+2500 across the pane. Long
#: enough to be one under any threshold this module could reasonably hold.
BORDER = "─" * 158


def box(*, draft: str = "", status: tuple[str, ...] = ()) -> str:
    """A pane tail shaped exactly like claude's: rule, prompt line, rule, status."""
    rows = [BORDER, f"❯ {draft}".rstrip(), BORDER, *status]
    return "\n".join(rows)


def test_the_agents_input_box_is_stripped_off_the_tail() -> None:
    """The symptom: two input boxes on one screen, one of them a picture.

    Our composer sits directly under this fragment. Rendering the agent's own
    box above it means offering a control that does nothing, on the one screen
    where "which box does typing go into" has to be unambiguous.
    """
    view = pane_view("real output\nmore output\n\n\n" + box(status=("ctx: 16%",)))
    assert [plain_text(line.runs) for line in view.lines] == [
        "real output",
        "more output",
    ]


def test_all_the_statusline_rows_are_promoted_not_just_the_first() -> None:
    """A statusline is arbitrary command output and is routinely two or three rows.

    Surfacing only the first is the mistake Collie made and recorded: it
    silently dropped exactly the fields the mirror can no longer show, which is
    the whole justification for stripping the box in the first place.
    """
    rows = ("herdr-pwa [main] | ctx: 16% | 12m | Opus 5", "auto mode on", "x")
    view = pane_view("out\n" + box(status=rows))
    assert [plain_text(line) for line in view.status] == list(rows)


def test_the_statusline_is_matched_by_position_and_never_by_content() -> None:
    """A user-configured statusline is arbitrary. Nothing may be assumed about it."""
    view = pane_view("out\n" + box(status=("\U0001f984 whatever anybody configured",)))
    assert [plain_text(line) for line in view.status] == ["\U0001f984 whatever anybody configured"]


def test_a_pane_with_no_input_box_comes_back_untouched() -> None:
    """Conservative: unsure means unchanged, and the caller falls back to the raw.

    Every fixture in the corpus that is a BLOCKED pane goes through here -- a
    permission prompt replaces the input box, so there is no box to strip and
    nothing to promote, and a strip that guessed would be hiding the lines the
    user drilled in to read.
    """
    text = "a\nb\nc"
    view = pane_view(text)
    assert [plain_text(line.runs) for line in view.lines] == ["a", "b", "c"]
    assert view.status == [] and view.draft == []


@pytest.mark.parametrize(
    ("name", "tail"),
    [
        ("no bottom border", f"{BORDER}\n❯ hi\nnot a border"),
        ("no top border", f"plain line\n❯ hi\n{BORDER}\n st"),
        ("no prompt line", f"{BORDER}\njust text\n{BORDER}\n st"),
        ("nothing at all", "one\ntwo"),
    ],
)
def test_a_partial_shape_strips_nothing(name: str, tail: str) -> None:
    """Only the WHOLE shape matches. A half-match would hide a real line."""
    view = pane_view("keep me\n" + tail)
    assert "keep me" in [plain_text(line.runs) for line in view.lines]
    assert view.status == []


def test_nothing_above_the_top_border_is_ever_removed() -> None:
    """The strip works at the tail only -- there is no scan over the buffer."""
    body = "\n".join(f"line {n}" for n in range(40))
    view = pane_view(body + "\n" + box(draft="d", status=("s",)))
    assert [plain_text(line.runs) for line in view.lines] == body.split("\n")


# --------------------------------------------------------------------------
# Two questions that look like one, and must never be unified
# --------------------------------------------------------------------------


def test_a_heavy_or_double_rule_is_a_rule_but_not_an_input_box_border() -> None:
    """The narrow predicate is U+2500 only, and this is why.

    Collie records what happened the last time the two were shared: a
    spaced-out prose separator read as an input-box border and defeated their
    send guard from the inside. Deciding "may Enter be pressed" and deciding
    "does this row wrap" are different questions with different costs, so a
    glyph that is obviously a rule must still fail the border test.
    """
    for glyph in ("━", "═", "┄", "—", "="):
        rule = glyph * 158
        view = pane_view(f"keep me\n{rule}\n❯ draft\n{rule}\n st")
        assert view.draft == [], glyph
        assert view.status == [], glyph


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
# Column-faithful is a different SERVER render, not just a class
# --------------------------------------------------------------------------


def test_column_faithful_mode_keeps_the_padding_wrap_mode_collapses() -> None:
    """Which is the whole reason the mode has to reach the server.

    A TUI table and a piece of ASCII art mean nothing once a run of column
    padding has been squeezed to one space, and squeezing it is what makes the
    pane readable at 390px. Both are right; they cannot both be the default.
    """
    row = "name" + " " * 20 + "value"
    assert plain_text(pane_view(row).lines[0].runs) == "name value"
    assert plain_text(pane_view(row, strip=False).lines[0].runs) == row


def test_the_chrome_comes_off_in_column_faithful_mode_too() -> None:
    """`strip` is a question about LAYOUT. Where text goes is a different one.

    A column-faithful mode that re-grew a second, fake input box would undo
    the input-box fix one tap away from the tab that made it.
    """
    view = pane_view("out\n" + box(draft="hi", status=("s",)), strip=False)
    assert [plain_text(line.runs) for line in view.lines] == ["out"]
    assert [plain_text(line) for line in view.status] == ["s"]
    assert [plain_text(line) for line in view.draft] == ["hi"]


# --------------------------------------------------------------------------
# A draft stranded on the prompt line
# --------------------------------------------------------------------------


def test_a_recalled_draft_is_surfaced_with_take_over_offered() -> None:
    view = pane_view("out\n" + box(draft="fix the failing test", status=("s",)))
    assert [plain_text(line) for line in view.draft] == ["fix the failing test"]
    assert view.takeover is True


def test_a_multi_line_draft_keeps_its_lines_and_loses_the_gutter() -> None:
    """The TUI indents continuation rows under the text. That indent is chrome."""
    tail = f"{BORDER}\n❯ first line\n  second line\n{BORDER}\n st"
    view = pane_view("out\n" + tail)
    assert [plain_text(line) for line in view.draft] == ["first line", "second line"]


def test_an_input_placeholder_is_not_a_draft() -> None:
    """The TUI paints hint text on the prompt line when the box is EMPTY.

    Offering it as recoverable user text would put the app's own hint into
    somebody's prompt, which is worse than showing nothing -- it invents a
    message and attributes it to a person.
    """
    view = pane_view("out\n" + box(draft="Press up to edit queued messages"))
    assert view.draft == []
    assert view.takeover is False


def test_an_empty_prompt_line_is_not_a_draft() -> None:
    assert pane_view("out\n" + box()).draft == []


def test_take_over_is_withdrawn_for_an_opaque_paste_token() -> None:
    """Claude collapses a long paste to '[Pasted text #N +M lines]'.

    The draft is REAL and saying so is honest -- but the string is a label for
    content this app cannot see, so copying it in would make the literal string
    the message. Absent, not greyed: a disabled control that never explains
    itself is worse than no control.
    """
    view = pane_view("out\n" + box(draft="[Pasted text #3 +412 lines]"))
    assert [plain_text(line) for line in view.draft] == ["[Pasted text #3 +412 lines]"]
    assert view.takeover is False


def test_a_draft_that_merely_quotes_the_paste_token_still_offers_take_over() -> None:
    """Matched whole, never as a substring: this is somebody's real sentence."""
    view = pane_view("out\n" + box(draft="why does [Pasted text #3 +2 lines] appear?"))
    assert view.takeover is True


def test_a_draft_is_kept_verbatim_and_not_padding_collapsed() -> None:
    """It is a message somebody meant to send, not a pane to be made legible."""
    view = pane_view("out\n" + box(draft="a" + " " * 20 + "b"))
    assert [plain_text(line) for line in view.draft] == ["a" + " " * 20 + "b"]


# --------------------------------------------------------------------------
# The DoS shape, again. _TAIL_FRAME cost this app two minutes on a progress bar.
# --------------------------------------------------------------------------


def test_the_new_rule_predicates_are_linear_on_a_pathological_row() -> None:
    """Every regex added since must be checked for the ``(a+)*`` shape.

    ``_WIDE_RULE_RUN`` is one character class with a bounded quantifier and no
    group, so there is nothing to backtrack into -- but the last regex here
    that "obviously" could not backtrack hung the mirror route and exhausted
    the threadpool, so it is measured rather than reasoned about.
    """
    start = time.perf_counter()
    for size in (200, 2000, 20000):
        pane_view("█" * size + "x")
        pane_view("-" * size + " tail")
        pane_view(("─" * size) + "\n❯ d\n" + ("─" * size) + "\ns")
    assert time.perf_counter() - start < 2.0


# --------------------------------------------------------------------------
# Width is cells, not characters, and it is wrong in two directions
# --------------------------------------------------------------------------


#: Eight CJK characters. `len()` says 8, the terminal drew 16 -- so a character
#: count UNDERSTATES this label and overstates the rule beside it.
WIDE_LABEL = "テーブルの見出し"

#: Four letters carrying eight combining marks, the way Vietnamese and the IPA
#: stack them. `len()` says 12, the terminal drew 4 -- the opposite error.
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
    """Thirty dashes and eight CJK characters: 83% rule by len(), 65% by cells.

    The row carries words, so cropping it hides them. A character count is what
    crops it -- this is the direction Collie's #76 failed in.
    """
    row = "-" * 30 + WIDE_LABEL
    assert len("-" * 30) >= len(row) * 0.75, "len() would call this mostly rule"
    assert pane_view(row).lines[0].clip is False


def test_stacked_diacritics_do_not_hide_a_row_that_is_mostly_rule() -> None:
    """The same test, pushed the other way: len() overstates, so the clip is missed."""
    row = "-" * 30 + STACKED_LABEL
    assert len("-" * 30) < len(row) * 0.75, "len() would call this mostly label"
    assert pane_view(row).lines[0].clip is True


def test_the_input_box_border_floor_is_a_cell_count_by_construction() -> None:
    """len() is honest here only while the alphabet is one one-cell glyph.

    `_is_box_border` accepts a row made entirely of U+2500, and U+2500 is one
    cell, so the character count and the cell count are the same number. Widen
    that alphabet to a two-cell glyph and the twenty silently becomes a claim
    about characters -- which is the bug this ticket exists to prevent.
    """
    border = panetext._BORDER_GLYPH * panetext._BORDER_MIN_CELLS
    assert cells(border) == len(border) == panetext._BORDER_MIN_CELLS
    assert panetext._is_box_border(border) is True
    assert panetext._is_box_border(border[:-1]) is False


# --------------------------------------------------------------------------
# A label the terminal only printed half of.
# --------------------------------------------------------------------------

CORPUS_DIR = Path(__file__).parent / "fixtures"

#: Every source each capture was taken in. A label that only survives one of
#: them is not a label anybody should be reading off a phone.
CAPTURE_SOURCES = ("visible", "recent", "recent-unwrapped", "detection")

#: The captures with a real menu in them, and whether their labels are whole.
#: The two `-wrapped` entries are the wrapped-label fixtures, taken on a 58-column
#: pane; the rest predate it and were taken at 158 columns, where nothing wraps.
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
    from herdr_pwa.engine.model import BlockedPrompt

    raw = (CORPUS_DIR / name / f"{source}.txt").read_text(encoding="utf-8")
    return raw, [option.label for option in BlockedPrompt.parse(raw).options]


@pytest.mark.parametrize("source", CAPTURE_SOURCES)
@pytest.mark.parametrize("name", sorted(MENU_CAPTURES))
def test_the_whole_corpus_agrees_about_which_labels_are_whole(name: str, source: str) -> None:
    """The false-positive guard, and the reason this is parametrized over everything.

    Catching the two wrapped captures is the easy half. The hard half is NOT
    flagging `capture-blocked-askuserquestion-menu`, whose description rows sit
    at exactly the indent a soft-wrapped continuation would -- the first version
    of this rule failed on it. See docs/modules/panetext/printed_whole.md.
    """
    raw, labels = _menu(name, source)
    assert labels, f"{name}/{source}: no options left to check"
    whole = all(printed_whole(raw, label) for label in labels)
    assert whole is MENU_CAPTURES[name], (
        f"{name}/{source}: expected whole={MENU_CAPTURES[name]}, got {whole} "
        f"for {[label for label in labels if not printed_whole(raw, label)]}"
    )


def test_the_wrapped_permission_label_drops_the_host_it_would_allow() -> None:
    """WHY this is not cosmetics, pinned on the capture rather than described.

    The label a phone would render is a grammatical sentence ending in
    `curl -sS`. The host it is offering to stop asking about is on the next row
    of the terminal and in none of the parsed options at all.
    """
    raw, labels = _menu("capture-blocked-permission-prompt-wrapped", "detection")
    assert "Yes, and don\u2019t ask again for: curl -sS" in labels
    assert not any("example.com" in label for label in labels)
    assert "https://example.com/thing" in raw
    assert printed_whole(raw, "Yes, and don\u2019t ask again for: curl -sS") is False
    # The unwrapped sibling of the same dialog is untouched.
    wide, wide_labels = _menu("capture-blocked-permission-prompt", "detection")
    assert all(printed_whole(wide, label) for label in wide_labels)


def test_a_label_the_screen_never_printed_is_refused() -> None:
    """The merged/garbled half of the rule -- the shape the ticket reported.

    `3. Nohift+tab)` did not reproduce against herdr 0.8.0, so there is no
    capture of it. The clause that would catch it is still load-bearing and is
    asserted here directly: a label no row of the screen ends with cannot be
    vouched for, whatever produced it.
    """
    screen = "Do you want to proceed?\n 1. Yes\n 2. No\n"
    assert printed_whole(screen, "No") is True
    assert printed_whole(screen, "Nohift+tab)") is False
    assert printed_whole(screen, "Yes; No") is False


def test_an_odd_token_in_a_label_is_not_what_makes_it_suspicious() -> None:
    """The false positive the ticket named: a semicolon is not evidence of anything.

    This rule reads the SCREEN, never the label's characters, which is why a
    label full of punctuation is as vouchable as `Yes`.
    """
    screen = " 1. Run: ls; rm -rf ./tmp (careful)\n 2. No\n"
    assert printed_whole(screen, "Run: ls; rm -rf ./tmp (careful)") is True


def test_a_description_row_is_not_a_continuation_when_the_word_would_have_fit() -> None:
    """The width clause, isolated: same geometry, opposite answers.

    Both screens put an indented row under the label at the label's own column.
    In the first the next word could not have fitted, so the terminal wrapped;
    in the second there were ninety columns spare, so the row below is a
    deliberate second line and the label above it is complete.
    """
    narrow = " 1. I prefer vibrant blues that remind me of clear\n    skies today\n"
    assert printed_whole(narrow, "I prefer vibrant blues that remind me of clear") is False
    wide = (
        " 1. Force-push\n    Force-push the current branch, overwriting history.\n"
        + "-" * 120
        + "\n"
    )
    assert printed_whole(wide, "Force-push") is True
