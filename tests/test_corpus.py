"""The interpret corpus: every real capture in ``tests/fixtures/capture-*`` run
through :meth:`BlockedPrompt.parse`, asserting the *right* outcome for each.

Why this file exists
---------------------------
``BlockedPrompt.parse`` decides whether a blocked agent's numbered options
become **tap targets on a phone**, and tapping one sends that keystroke into a
live terminal. It has already been wrong once in a way that passed five
hand-written tests: it turned a finished transcript's numbered list into
buttons while dropping the real question. That was fixed against exactly *one*
real capture. One capture is not a corpus.

Every fixture here is a byte-faithful capture of real ``herdr agent read``
output taken on 2026-08-18 against herdr 0.8.0 / snapshot protocol 19 with
detection manifest ``claude`` ``2026.08.13.1`` -- except the ones listed in
``MANIFEST_OVERRIDES``, which the capture store filed later and which carry the
manifest they were really taken under. Each capture directory holds
all four ``--source`` variants (``visible``, ``recent``, ``recent-unwrapped``,
``detection``), the matching ``herdr agent explain`` output (which names the
rule, its region and priority, and the evidence), ``herdr agent get`` and a
``meta.json`` recording provenance. See ``tests/fixtures/README.md``.

The negative cases are the point. A numbered list in finished transcript must
never become a button.

The convention for a defect this corpus finds
--------------------------------------------
A defect is recorded as ``xfail(strict=True)`` on the assertion the parser
*should* satisfy -- never as a passing assertion pinning the wrong answer. The
difference matters at the moment somebody fixes ``model.py``: a strict xfail
turns red and says "your fix worked, delete this marker", while a pin turns red
and invites the reader to flip the assertion back to the bug.

Two defects were recorded that way until they were fixed: a dashed U+254C rule
that ``_DIVIDER_RE`` did not know, which turned a file diff into the question,
and a trust dialog whose question resolved to the label of a hyperlink. Both are
now ordinary regression pins.

A third was closed by the recap fix:
``test_a_transcript_ending_on_a_numbered_list_must_not_become_tap_targets`` --
a viewport that ENDS on a numbered list became three tap targets under a
"question" that was really the human's own echoed prompt. It is now an ordinary
regression pin. The fix was not a better numbered-run heuristic: the run is
required to be CORROBORATED as live (a key-hint footer, or a selection marker)
before it may become options at all, and the echoed prompt has neither.

What the corpus asserts, beyond ``parse``
-----------------------------------------
A stub survey showed three protections the corpus was not
actually testing: agent *status* (a constant classifier passed everything),
``_OPTION_RE``'s separator, and the safety-critical "a menu must END the
viewport" anchor -- which was being carried by an accident of glyphs rather than
by the anchor. See the section at the bottom of this file; each test there was
verified by removing the protection from a scratch copy of ``src/``.
"""

import json
import re
from pathlib import Path

import pytest

from herdr_pwa.engine.model import (
    _OPTION_RE,
    Agent,
    AgentStatus,
    BlockedPrompt,
    MenuShape,
    as_pane_id,
    as_session_id,
)

FIXTURES = Path(__file__).parent / "fixtures"

#: Every capture is stored in all four of these, one file each.
SOURCES = ("visible", "recent", "recent-unwrapped", "detection")

#: Provenance every capture was taken under. Asserted, not documented: when a
#: manifest update changes detection behaviour, a re-capture that silently lands
#: on a different manifest would otherwise look like a parser regression.
#:
#: This is the version the captured BYTES were produced under, and it does not
#: move when herdr ships a new manifest -- ``tests/test_drift.py`` is the half
#: that watches the live one and replays this corpus through it.
MANIFEST_VERSION = "2026.08.13.1"
HERDR_VERSION = "0.8.0"
PROTOCOL = 19

#: The captures NOT taken in the 2026-08-18 batch, and the manifest each one
#: really carries. Declared per capture rather than relaxed to "any version":
#: the point of asserting provenance is that a re-capture landing on a different
#: manifest is a decision somebody made, visible in this diff, and not a silent
#: change that will later read as a parser regression.
#:
#: `capture-blocked-multiselect-checklist` is here because the shape it carries
#: did not exist in the corpus at all and the capture store filed it on
#: 2026-08-23, several manifests later. Re-taking it under 2026.08.13.1 is not
#: possible and would not be worth anything if it were: what it is evidence of
#: is what a checklist looks like NOW.
MANIFEST_OVERRIDES: dict[str, str] = {
    "capture-blocked-multiselect-checklist": "2026.08.21.1",
    "capture-blocked-plan-approval": "2026.08.21.1",
}

#: The same argument as :data:`MANIFEST_OVERRIDES`, for the other two halves of
#: provenance. ``capture-blocked-plan-approval`` was taken on 2026-08-29 against
#: a herdr that had moved twice and a snapshot protocol that had moved once
#: since the 2026-08-18 batch, and recording 0.8.0/19 for it would be recording
#: a machine that never read this pane -- exactly the lie this table exists to
#: prevent. So the numbers are the ones the capture really carries, per capture.
HERDR_OVERRIDES: dict[str, str] = {
    "capture-blocked-plan-approval": "0.8.2",
}

PROTOCOL_OVERRIDES: dict[str, int] = {
    "capture-blocked-plan-approval": 20,
}


def capture_dirs() -> list[Path]:
    return sorted(p for p in FIXTURES.glob("capture-*") if p.is_dir())


def read(name: str, source: str) -> str:
    return (FIXTURES / name / f"{source}.txt").read_text(encoding="utf-8")


def meta(name: str) -> dict:
    return json.loads((FIXTURES / name / "meta.json").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# Corpus shape and provenance
# --------------------------------------------------------------------------

ALL_CAPTURES = [p.name for p in capture_dirs()]


def test_corpus_is_not_empty() -> None:
    assert ALL_CAPTURES, "no capture-* fixtures found; the corpus is the artifact"


@pytest.mark.parametrize("name", ALL_CAPTURES)
def test_capture_is_complete(name: str) -> None:
    """A capture without all four sources plus its explain output is not evidence."""
    for source in SOURCES:
        path = FIXTURES / name / f"{source}.txt"
        assert path.is_file(), f"{name}: missing {source}.txt"
        assert path.read_text(encoding="utf-8").strip(), (
            f"{name}: {source}.txt is empty"
        )
    for extra in ("explain.txt", "explain.json", "agent-get.json", "meta.json"):
        assert (FIXTURES / name / extra).is_file(), f"{name}: missing {extra}"


@pytest.mark.parametrize("name", ALL_CAPTURES)
def test_capture_provenance(name: str) -> None:
    """The manifest/herdr/protocol the capture was taken under is recorded."""
    m = meta(name)
    manifest = MANIFEST_OVERRIDES.get(name, MANIFEST_VERSION)
    assert m["detection_manifest"]["version"] == manifest
    assert m["herdr_version"] == HERDR_OVERRIDES.get(name, HERDR_VERSION)
    assert m["protocol"] == PROTOCOL_OVERRIDES.get(name, PROTOCOL)
    explain = (FIXTURES / name / "explain.txt").read_text(encoding="utf-8")
    assert manifest in explain
    assert f"state: {m['agent_status']}" in explain


@pytest.mark.parametrize("name", ALL_CAPTURES)
def test_detection_equals_visible(name: str) -> None:
    """``--source detection`` == ``--source visible``, on every manifest so far.

    Byte-identical in all eight captures, including the one taken several
    manifests later than the rest. Not documented anywhere by herdr, so
    this is a tripwire: if a manifest update makes detection a narrower region,
    this fails and the parse expectations below need re-reading, because
    ``detection`` is the source the dashboard reads a blocked prompt from.
    """
    assert read(name, "detection") == read(name, "visible")


# --------------------------------------------------------------------------
# The parse expectations
# --------------------------------------------------------------------------

#: name -> (expected question, expected options as ((number, label), ...))
#: An empty options tuple means: this capture must NOT become tap targets.
MENUS: dict[str, tuple[str, tuple[tuple[int, str], ...]]] = {
    # AskUserQuestion. The highest-value capture: a real live menu.
    # Note options 4 and 5 are Claude Code's own affordances, not answers the
    # agent offered -- see test_askuserquestion_trailing_affordances.
    "capture-blocked-askuserquestion-menu": (
        "Should I force-push to main?",
        (
            (1, "Force-push"),
            (2, "Open a PR instead"),
            (3, "Do nothing"),
            (4, "Type something."),
            (5, "Chat about this"),
        ),
    ),
    # A Bash permission prompt: a different grammar (rule
    # generic_permission_prompt, region after_last_horizontal_rule, priority
    # 840) that nonetheless renders as a 1/2/3 menu. The parser handles it.
    "capture-blocked-permission-prompt": (
        "Do you want to proceed?",
        (
            (1, "Yes"),
            (
                2,
                "Yes, and don’t ask again for: "
                "curl -sS https://example.com/demo-permission-probe",
            ),
            (3, "No"),
        ),
    ),
    # The trust dialog, before any agent session exists. Its question sits two
    # chrome paragraphs above the options (a capability hint, then a bare
    # "Security guide" link label), which is the case the chrome fix covered: taking the
    # adjacent paragraph put "Security guide" above "Yes, I trust this folder".
    "capture-blocked-trust-folder": (
        "Quick safety check: Is this a project you created or one you trust? "
        "(Like your own code, a well-known open source project, or work from "
        "your team). If\nnot, take a moment to review what's in this folder "
        "first.",
        ((1, "Yes, I trust this folder"), (2, "No, exit")),
    ),
    # ExitPlanMode, and the reason it is here is that every one of
    # these three rows used to be invisible to us: the whole screen parsed
    # shape=none, so the phone said an agent was blocked and that there was no
    # question on it. Two lines did that, and EITHER ALONE WAS ENOUGH -- the
    # `shift+tab to approve with this feedback` sub-line under the last option,
    # and the `ctrl+g to edit in Vim · <path>` footer. Both are on THIS capture,
    # taken on Claude Code 2.1.251, which is what makes it evidence rather than
    # a memory of collie's older tree.
    "capture-blocked-plan-approval": (
        "Claude has written up a plan and is ready to execute. "
        "Would you like to proceed?",
        (
            (1, "Yes, auto-accept edits"),
            (2, "Yes, manually approve edits"),
            (3, "Tell Claude what to change"),
        ),
    ),
}

#: Captures that must parse to NO options at all -- the whole point of the
#: corpus. Two of them contain a numbered list that is not a menu; the third
#: contains no numbered list at all and is here for a different reason (see
#: below), so "every negative contains a numbered list" is NOT true and must
#: not be asserted. :data:`NUMBERED_NEGATIVES` names the ones that do.
NEGATIVES = (
    # Idle agent whose last output is a 1./2./3. recap followed by a question
    # in prose ("Want me to fix the README and the gate, then commit?").
    "capture-idle-claude-prompt",
    # Idle agent whose transcript ENDS on a 1./2./3. list. The closest a real
    # capture gets to the documented residual; the empty prompt box and the
    # status bar that follow are what saves it.
    "capture-idle-numbered-recap",
    # An agent mid-work. There is NO numbered list anywhere in this capture --
    # it is a wall of prose, a background-agent table and a status bar. Its job
    # in the corpus is the other direction: a busy viewport full of glyphs,
    # box-drawing rules and "◯ general-purpose ..." rows must not manufacture
    # options out of chrome. Nothing is being asked, so nothing is tappable.
    "capture-working-claude",
)

#: The negatives that really do contain a clean numbered run. Split out from
#: :data:`NEGATIVES` because ``capture-working-claude`` does not, and a blanket
#: claim that they all do would be false -- and would make the guard below
#: vacuous for the one capture it is checking.
NUMBERED_NEGATIVES = (
    "capture-idle-claude-prompt",
    "capture-idle-numbered-recap",
)


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("name", sorted(MENUS))
def test_menu_captures_parse_to_options(name: str, source: str) -> None:
    """A real live menu yields ``is_choice`` and the right options, from any source.

    Parametrized over all four sources deliberately: the dashboard may read any
    of them, and an option list that only survives one source is a latent bug.
    """
    question, options = MENUS[name]
    prompt = BlockedPrompt.parse(
        read(name, source), target=as_pane_id("w1:pX"), session=as_session_id("s")
    )
    assert prompt.is_choice is True
    assert prompt.shape is MenuShape.SINGLE_CHOICE
    assert prompt.corroborated is True
    assert [(o.number, o.label) for o in prompt.options] == list(options)
    # An option is a LABEL, never a keystroke. ``PromptOption.key`` used to
    # return the digit and was deleted: a digit synthesised from a
    # numbered row is exactly what Collie's ADR 0009 banned, and a corpus test
    # asserting the digit was the corpus pinning the banned behaviour.
    assert not any(hasattr(o, "key") for o in prompt.options)
    assert not any(h.key.isdigit() for h in prompt.hints)
    assert prompt.question == question
    assert prompt.raw == read(name, source)
    assert prompt.target == "w1:pX"


#: name -> the rows a CHECKLIST parses to, as ((number, label, checked), ...).
#: ``None`` for checked means the terminal drew no box beside that row, so it is
#: not a toggle -- which is a third answer and not a synonym for "unticked".
CHECKLISTS: dict[str, tuple[str, tuple[tuple[int, str, bool | None], ...]]] = {
    # A checklist, and the corpus's first non-single-choice blocked shape. A
    # multiSelect AskUserQuestion, with a description line between every pair of
    # rows and one row ALREADY TICKED -- which is what makes it evidence. A
    # parser that assumed every box was empty would pass on an untouched
    # capture and be wrong on the phone.
    "capture-blocked-multiselect-checklist": (
        "Which seasons?",
        (
            (1, "spring", False),
            (2, "summer", False),
            (3, "autumn", True),
            (4, "winter", False),
            (5, "Type something", False),
            (6, "Chat about this", None),
        ),
    ),
}


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("name", sorted(CHECKLISTS))
def test_checklist_captures_parse_to_rows_with_their_tick_state(
    name: str, source: str
) -> None:
    """A real checklist yields its rows AND what the terminal drew on each.

    The description line between every pair of rows is the thing that was
    supposed to be defeating the grammar. Measured against these bytes, it never
    was: ``_is_interleave`` already allows an indented continuation line, and the
    run comes out complete from all four sources. What was missing was keeping
    the rows once they were found, and reading the marker off them.
    """
    question, rows = CHECKLISTS[name]
    prompt = BlockedPrompt.parse(read(name, source))
    assert prompt.shape is MenuShape.MULTI_SELECT
    assert prompt.corroborated is True
    assert [(o.number, o.label, o.checked) for o in prompt.options] == list(rows)
    assert prompt.question == question
    # The label is what a reader reads: the marker belongs to `checked` and must
    # not be left glued to the front of it, or the row renders as "[ ] spring".
    assert not any(o.label.startswith("[") for o in prompt.options)


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("name", NEGATIVES)
def test_non_menu_captures_yield_no_tap_targets(name: str, source: str) -> None:
    """No numbered list in finished transcript may become a tap target.

    This is the failure the corpus exists to prevent: a wrong tap target is
    worse than none, because the consequence is a keystroke in a live terminal.
    """
    prompt = BlockedPrompt.parse(read(name, source))
    assert prompt.options == ()
    assert prompt.is_choice is False


@pytest.mark.parametrize("source", SOURCES)
def test_numbered_recap_is_refused_even_though_it_ends_the_transcript(
    source: str,
) -> None:
    """The recap capture really does contain a clean 1./2./3. run.

    Guards the negative above from decaying into a vacuous assertion: if a
    re-capture lost the numbered list, the negative test would still pass while
    testing nothing.
    """
    text = read("capture-idle-numbered-recap", source)
    assert "1. README.md" in text
    assert "2. pyproject.toml" in text
    assert "3. src/<package>/__init__.py" in text


#: A numbered list line as a *reader* sees it, independent of ``_OPTION_RE``:
#: optional indent, an optional response/list marker (including the ``●`` the
#: parser refuses), a number, a separator, a label. Deliberately laxer than the
#: parser's own grammar -- this guards the fixtures, not the parser, and must
#: keep matching even for the lines ``_OPTION_RE`` rejects.
_LIST_LINE_RE = re.compile(r"^\s*[●>❯*•\-]?\s*(\d{1,2})[.):]\s+\S")


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("name", NUMBERED_NEGATIVES)
def test_the_numbered_negatives_still_carry_a_numbered_list(
    name: str, source: str
) -> None:
    """Every capture :data:`NUMBERED_NEGATIVES` names must really carry a 1..n run.

    Makes that constant load-bearing instead of documentary, and extends to
    ``capture-idle-claude-prompt`` the vacuity guard the recap capture already
    had: if a re-capture scrolled its numbered list out of the viewport, the
    negative above would keep passing while testing nothing at all.
    """
    lines = read(name, source).splitlines()
    numbers = [int(m.group(1)) for line in lines if (m := _LIST_LINE_RE.match(line))]
    assert numbers[:3] == [1, 2, 3], f"{name}/{source}: no 1./2./3. run left"


def test_askuserquestion_trailing_affordances_are_not_answers() -> None:
    """Recorded gap: options 4 and 5 are UI affordances, not the agent's answers.

    The agent offered three answers. Claude Code appends "Type something." and
    "Chat about this" -- and the parser cannot tell them apart, so a phone shows
    five buttons for a three-way question. Tapping 4 or 5 is not dangerous (both
    open a text field), so this is a fidelity gap, not a safety one, and it is
    recorded rather than worked around: the parser only sees the terminal, and
    the terminal genuinely lists five numbered choices.
    """
    prompt = BlockedPrompt.parse(
        read("capture-blocked-askuserquestion-menu", "detection")
    )
    assert [o.label for o in prompt.options][3:] == [
        "Type something.",
        "Chat about this",
    ]


def test_permission_prompt_question_omits_the_command_being_approved() -> None:
    """Recorded gap: the question is "Do you want to proceed?" and nothing else.

    The command under approval (``curl -sS https://example.com/...``) sits in an
    earlier paragraph of the same box and is dropped. Option 2 happens to quote
    it, which is the only reason the command is recoverable at all here -- a
    file-edit prompt (capture-blocked-permission-write) has no such luck.

    Not a defect: this IS what ``parse`` promises (the question paragraph
    before the options -- and "Do you want to proceed?" *is* the question; the
    command is a separate paragraph and separate paragraphs are never merged).
    It is recorded so that whoever builds the blocked-prompt view
    knows ``question`` alone is not enough to approve a command from a phone,
    and must render ``raw``.
    """
    prompt = BlockedPrompt.parse(read("capture-blocked-permission-prompt", "detection"))
    assert prompt.question == "Do you want to proceed?"
    assert "curl" not in prompt.question
    assert "curl -sS https://example.com/demo-permission-probe" in prompt.raw


@pytest.mark.parametrize("source", SOURCES)
def test_write_permission_prompt_options(source: str) -> None:
    """The file-write permission prompt: options parse, decoys are refused.

    The diff body carries a gutter line number (``  1 hello``) that a laxer
    option regex would happily promote to "option 1". It must not appear.
    """
    prompt = BlockedPrompt.parse(read("capture-blocked-permission-write", source))
    assert [(o.number, o.label) for o in prompt.options] == [
        (1, "Yes"),
        (2, "Yes, allow all edits during this session (shift+tab)"),
        (3, "No"),
    ]
    assert "hello" not in " ".join(o.label for o in prompt.options)


@pytest.mark.parametrize("source", SOURCES)
def test_write_permission_prompt_question(source: str) -> None:
    """The question for a file-write approval is the question, not the diff.

    Regression pin for the first chrome defect: the diff body sits between two dashed
    U+254C rules, and until ``_DIVIDER_RE`` learned that glyph the whole diff
    plus both 150-character rules became the "question" shown above the
    Yes/No buttons.
    """
    prompt = BlockedPrompt.parse(read("capture-blocked-permission-write", source))
    assert prompt.question == "Do you want to create notes.txt?"


def test_write_permission_prompt_question_excludes_the_diff_body() -> None:
    """Pins the fix precisely: no dashed rule, and no diff text, in the question.

    The diff is still recoverable from ``raw`` -- it is dropped from the
    question, not from the prompt.
    """
    prompt = BlockedPrompt.parse(read("capture-blocked-permission-write", "detection"))
    assert prompt.question == "Do you want to create notes.txt?"
    assert "\u254c" not in prompt.question
    assert "hello" not in prompt.question
    assert "\u254c" * 20 in prompt.raw
    assert "1 hello" in prompt.raw


@pytest.mark.parametrize("source", SOURCES)
def test_trust_dialog_asks_the_safety_question_not_the_link_label(source: str) -> None:
    """'Yes, I trust this folder' must not be offered under the wrong question.

    Regression pin for the second chrome defect. The dialog prints two chrome paragraphs
    between its question and its options, and the last of them is the bare
    label of a hyperlink ("Security guide"), which used to end up above two
    live tap targets on a security-consequential prompt.
    """
    prompt = BlockedPrompt.parse(read("capture-blocked-trust-folder", source))
    assert "Quick safety check" in prompt.question
    assert prompt.question != "Security guide"


def test_trust_dialog_chrome_is_skipped_but_not_swallowed() -> None:
    """The skipped chrome is dropped from the question, never merged into it.

    The rule only ever *selects* one whole paragraph, so a hint line cannot be
    glued onto the question -- and both are still recoverable from ``raw``.
    """
    prompt = BlockedPrompt.parse(read("capture-blocked-trust-folder", "detection"))
    assert "Security guide" not in prompt.question
    assert "read, edit, and execute files here" not in prompt.question
    assert "Security guide" in prompt.raw
    assert (
        "Quick safety check: Is this a project you created or one you trust?"
        in prompt.raw
    )


# --------------------------------------------------------------------------
# The documented residual, on real bytes
# --------------------------------------------------------------------------


def test_real_recap_survives_truncation_by_accident_not_by_design() -> None:
    """Slicing the terminal chrome off the recap capture does NOT reopen the residual.

    ``capture-idle-numbered-recap`` is refused for two independent reasons, and
    only one of them is the documented rule. Cut the prompt box and status bar
    off the end -- exactly what a caller that strips terminal furniture before
    parsing would do -- and the list *still* fails to become a menu, because
    Claude Code prefixes the first line of a response with ``\u25cf`` (BLACK
    CIRCLE), which ``_OPTION_RE``'s marker class does not accept (it has
    ``\u2022`` BULLET, not ``\u25cf``). Option 1 never matches, so the ``1..n``
    run cannot be built.

    That is luck, not design, and it is worth pinning: the day the marker class
    grows ``\u25cf`` -- a reasonable-looking tidy-up -- this stops protecting
    anything and the residual below is what is left.
    """
    text = read("capture-idle-numbered-recap", "detection")
    marker = "3. src/<package>/__init__.py"
    truncated = text[: text.index(marker) + len(marker)] + "\n"

    assert "\u25cf 1. README.md" in truncated
    prompt = BlockedPrompt.parse(truncated)
    assert prompt.is_choice is False


def residual_shape() -> str:
    """The documented residual, built from real bytes.

    Two edits to ``capture-idle-numbered-recap``, each of which a real terminal
    produces on its own: the trailing prompt box is cut (a caller that strips
    chrome before parsing), and the ``\u25cf`` response marker on the first item
    is replaced with the plain indent every *subsequent* line already has (a
    list that starts anywhere but the first line of a response). Nothing else
    is touched -- the option text is the agent's own.

    A live capture of this exact shape was hunted for and not found: under
    manifest 2026.08.13.1 every idle Claude viewport ends in the prompt box.
    """
    text = read("capture-idle-numbered-recap", "detection")
    marker = "3. src/<package>/__init__.py"
    truncated = text[: text.index(marker) + len(marker)] + "\n"
    return truncated.replace("\u25cf 1. README.md", "  1. README.md")


def test_the_residual_shape_is_built_the_way_it_claims_to_be() -> None:
    """Guards the xfail below from decaying into a vacuously-failing test.

    ``xfail(strict=True)`` goes green the moment the *fixture text* stops being
    the residual shape -- a re-capture that renamed a file would do it -- and
    the strictness would then report "fixed" when nothing was. So the inputs
    are asserted separately, in a test that is expected to pass.
    """
    text = residual_shape()
    assert "  1. README.md" in text
    assert "\u25cf" not in text.split("README.md")[0].rsplit("\n", 1)[-1]
    assert "2. pyproject.toml" in text
    assert "3. src/<package>/__init__.py" in text
    assert "\u276f Reply with EXACTLY a numbered list" in text
    assert text.rstrip().endswith("3. src/<package>/__init__.py")


def test_a_transcript_ending_on_a_numbered_list_must_not_become_tap_targets() -> None:
    """A finished answer must never render as buttons. It no longer does.

    Recorded as ``xfail(strict=True)`` by a stub survey and CLOSED by
    The shape has no key-hint footer and no selection marker, so
    it is not corroborated as a live menu and gets no options. The strict xfail
    did its job -- it went XPASS the moment the parser was fixed, which is the
    notification the convention exists to produce, and the marker is deleted
    rather than the assertion flipped.
    """
    prompt = BlockedPrompt.parse(residual_shape())
    assert prompt.options == ()
    assert prompt.is_choice is False
    assert prompt.corroborated is False
    assert prompt.shape is MenuShape.NONE


def test_a_lone_trailing_number_is_never_a_menu() -> None:
    """_MIN_MENU_OPTIONS, exercised against the real menu's own preamble."""
    prompt = BlockedPrompt.parse("Deploying now.\n\n1. Done\n")
    assert prompt.is_choice is False


# --------------------------------------------------------------------------
# Holes a stub survey found
# --------------------------------------------------------------------------
#
# A reviewer stubbed pieces of the parser out and measured which tests died.
# Three protections turned out to be untested, or tested only by accident:
#
#   1. agent *status*. A classifier hardcoded to one value passed all 69 tests,
#      even though every capture records a status in meta.json and names the
#      matching rule in explain.txt.
#   2. ``_OPTION_RE``'s separator. Making it optional -- so a bare "1 hello"
#      counts as an option -- broke only one unrelated test, despite a decoy
#      capture (the file-diff gutter) captured expressly for that risk.
#   3. the "a menu must END the viewport" anchor, which is the safety-critical
#      one. Deleting it reddened only the capture-idle-claude-prompt cases,
#      because capture-idle-numbered-recap's option 1 is prefixed ``●`` and so
#      never matches _OPTION_RE at all -- the negative was carried by that
#      accident, not by the anchor it exists to protect.
#
# Each test below was verified by removing the protection in a scratch
# copy of src/ -- run with PYTHONPATH pointing at the copy, because the venv's
# editable install otherwise silently wins and every result is meaningless --
# and confirming that the right tests, specifically, go red:
#
#   AgentStatus.coerce -> a constant           8 corpus tests red (0 before)
#   _OPTION_RE separator made optional         2 corpus tests red (0 before)
#   end-of-viewport anchor deleted             9 corpus tests red (4 before)
#
# The status mutation reddens no parse test and the parse mutations redden no
# status test, so each group is measuring its own protection and not a
# neighbour's.


# ---- 1. status ------------------------------------------------------------

#: Which status each capture was really in, from its own ``meta.json``. Pulled
#: into a literal on purpose: reading it out of the fixture on both sides of
#: the assertion would compare a file with itself.
EXPECTED_STATUS: dict[str, AgentStatus] = {
    "capture-blocked-askuserquestion-menu": AgentStatus.BLOCKED,
    "capture-blocked-askuserquestion-wrapped": AgentStatus.BLOCKED,
    "capture-blocked-permission-prompt": AgentStatus.BLOCKED,
    "capture-blocked-permission-prompt-wrapped": AgentStatus.BLOCKED,
    "capture-blocked-multiselect-checklist": AgentStatus.BLOCKED,
    "capture-blocked-permission-write": AgentStatus.BLOCKED,
    "capture-blocked-plan-approval": AgentStatus.BLOCKED,
    "capture-blocked-trust-folder": AgentStatus.BLOCKED,
    "capture-idle-claude-prompt": AgentStatus.IDLE,
    "capture-idle-numbered-recap": AgentStatus.IDLE,
    "capture-working-claude": AgentStatus.WORKING,
}


def agent_get(name: str) -> dict:
    """The ``agents[]``-shaped payload from a capture's ``herdr agent get``."""
    raw = json.loads((FIXTURES / name / "agent-get.json").read_text(encoding="utf-8"))
    return raw["result"]["agent"]


def test_every_capture_has_a_declared_status() -> None:
    """The table above must cover the corpus, or the parametrized tests lie."""
    assert sorted(EXPECTED_STATUS) == sorted(ALL_CAPTURES)


@pytest.mark.parametrize("name", ALL_CAPTURES)
def test_capture_status_survives_the_model(name: str) -> None:
    """``Agent.from_raw`` must reproduce the status herdr actually reported.

    The corpus asserted nothing about status until a stub survey: a classifier that
    returned one constant for everything passed the whole suite. It cannot pass
    this, because the corpus spans three distinct statuses (see below) and each
    capture's ``agent-get.json`` is real bytes off the socket.
    """
    agent = Agent.from_raw(agent_get(name))
    assert agent.status is EXPECTED_STATUS[name]
    assert agent.status is not AgentStatus.UNKNOWN


@pytest.mark.parametrize("name", ALL_CAPTURES)
def test_capture_status_agrees_with_the_rule_that_matched(name: str) -> None:
    """Three independent records of the same fact must not have drifted apart.

    ``meta.json``'s ``agent_status``, ``explain.json``'s ``matched_rule.state``
    and ``explain.txt``'s ``state:`` line all describe one observation. If a
    re-capture updates one and not the others, the corpus is quietly asserting
    against a status that was never observed.
    """
    expected = EXPECTED_STATUS[name]
    explain = json.loads((FIXTURES / name / "explain.json").read_text(encoding="utf-8"))
    assert AgentStatus.coerce(meta(name)["agent_status"]) is expected
    assert AgentStatus.coerce(explain["state"]) is expected
    assert AgentStatus.coerce(explain["matched_rule"]["state"]) is expected
    assert AgentStatus.coerce(agent_get(name)["agent_status"]) is expected
    text = (FIXTURES / name / "explain.txt").read_text(encoding="utf-8")
    assert f"state: {expected.value}" in text
    assert f"rule: {explain['matched_rule']['id']} " in text


def test_the_corpus_spans_enough_statuses_to_kill_a_constant_classifier() -> None:
    """A constant answer must be wrong for at least two captures, whatever it is.

    This is what makes the per-capture assertions above a real test of the
    classifier rather than of one lucky default. Three distinct statuses over
    seven captures: no single constant can score better than 4/7.
    """
    observed = {Agent.from_raw(agent_get(name)).status for name in ALL_CAPTURES}
    assert observed == {AgentStatus.BLOCKED, AgentStatus.IDLE, AgentStatus.WORKING}
    for constant in AgentStatus:
        wrong = [n for n in ALL_CAPTURES if EXPECTED_STATUS[n] is not constant]
        assert wrong, f"a classifier stuck on {constant} would pass the corpus"


def test_status_drives_the_urgency_order_the_feed_is_built_on() -> None:
    """Status is not decoration: ``rank`` is what puts blocked agents on top.

    A classifier that collapsed the corpus onto one status would also collapse
    the feed into an arbitrary order, which is the user-visible consequence and
    is worth pinning next to the classification itself.
    """
    ranks = {name: Agent.from_raw(agent_get(name)).rank for name in ALL_CAPTURES}
    blocked = [n for n in ALL_CAPTURES if EXPECTED_STATUS[n] is AgentStatus.BLOCKED]
    idle = [n for n in ALL_CAPTURES if EXPECTED_STATUS[n] is AgentStatus.IDLE]
    working = [n for n in ALL_CAPTURES if EXPECTED_STATUS[n] is AgentStatus.WORKING]
    assert max(ranks[n] for n in blocked) < min(ranks[n] for n in working)
    assert max(ranks[n] for n in working) < min(ranks[n] for n in idle)


# ---- 2. _OPTION_RE's separator --------------------------------------------
#
# The regex requires punctuation between the number and the label: "1. Yes",
# "2) Yes", "3: Yes". Dropping that requirement -- an easy-looking tidy-up, and
# the thing capture-blocked-permission-write was captured to guard against --
# turns every gutter line number, every table row and every ordinal in prose
# into a tap target.

#: The dashed rule Claude Code draws around a file-diff body, taken from the
#: real capture rather than retyped, so this decoy has the framing the parser
#: really sees.
DIFF_RULE = "\u254c" * 156


def test_a_diff_gutter_is_not_a_menu() -> None:
    """Three gutter line numbers ending the viewport must not become buttons.

    ``capture-blocked-permission-write`` has exactly one line of diff body
    (``  1 hello``), which is below :data:`_MIN_MENU_OPTIONS` -- so the real
    capture cannot exercise the separator rule on its own, and laxening the
    separator broke nothing in the corpus. This is the same shape with a
    three-line file, which is what the next capture of a real multi-line write
    would look like.

    Everything about it is a menu except the punctuation: contiguous, starting
    at 1, ending the viewport.
    """
    text = (
        " Do you want to create notes.txt?\n"
        f"{DIFF_RULE}\n"
        "  1 hello\n"
        "  2 world\n"
        "  3 goodbye\n"
        f"{DIFF_RULE}\n"
    )
    prompt = BlockedPrompt.parse(text)
    assert prompt.options == ()
    assert prompt.is_choice is False


def test_the_real_capture_carries_the_gutter_decoy_it_claims_to() -> None:
    """The decoy above is derived from real bytes; check the bytes are still there."""
    raw = read("capture-blocked-permission-write", "detection")
    assert "\n  1 hello\n" in raw
    assert DIFF_RULE in raw


def test_numbered_prose_at_the_end_of_a_viewport_is_not_a_menu() -> None:
    """The other half of the same hole: ordinals in a sentence, no punctuation.

    "step 1 do this" is not an option, and a viewport can easily end on three
    such lines. Only the separator requirement stands between this and three
    live tap targets.
    """
    text = "Here is the plan.\n\n1 clone the repo\n2 install deps\n3 run tests\n"
    assert BlockedPrompt.parse(text).is_choice is False


@pytest.mark.parametrize("separator", [".", ")", ":"])
def test_the_separators_a_real_menu_uses_are_accepted(separator: str) -> None:
    """The complement: the rule must stay lax enough for the shapes that exist.

    Pinned so that "fix" #2 is not to narrow the class down to ``.`` alone --
    which would make the test above pass while breaking real menus.
    """
    text = (
        f"Proceed?\n\n❯ 1{separator} Yes\n  2{separator} No\n\n"
        "Esc to cancel \u00b7 Enter to select\n"
    )
    prompt = BlockedPrompt.parse(text)
    assert [(o.number, o.label) for o in prompt.options] == [(1, "Yes"), (2, "No")]


def test_a_separator_needs_whitespace_before_the_label() -> None:
    """ "1.5" and "2.0" are numbers, not options one and two.

    A version list or a table of measurements ending a viewport is common; the
    required space after the separator is the only thing that stops it.
    """
    text = "Latency by build:\n\n1.5\n2.0\n3.1\n"
    assert BlockedPrompt.parse(text).is_choice is False


# ---- 3. the "a menu must END the viewport" anchor -------------------------


@pytest.mark.parametrize("source", SOURCES)
def test_a_numbered_recap_with_the_terminal_below_it_is_refused(source: str) -> None:
    """The anchor, tested on its own rather than through a lucky glyph.

    ``capture-idle-numbered-recap`` is refused for two independent reasons: the
    list does not end the viewport (the anchor), and option 1 is prefixed
    ``\u25cf``, which ``_OPTION_RE``'s marker class does not accept, so the run
    is never even built. The second reason masks the first -- deleting the
    anchor entirely left this capture green.

    So the ``\u25cf`` is replaced with the plain indent every *following* line
    already carries, and nothing else is touched. All three options now match
    the regex, they are contiguous, and they start at 1. The **only** thing
    refusing them is that the prompt box, the "Cooked for 1s" line and the
    status bar sit below.
    """
    text = read("capture-idle-numbered-recap", source).replace(
        "\u25cf 1. README.md", "  1. README.md"
    )
    assert "  1. README.md" in text
    assert "  2. pyproject.toml" in text
    assert "  3. src/<package>/__init__.py" in text
    prompt = BlockedPrompt.parse(text)
    assert prompt.options == ()
    assert prompt.is_choice is False


def test_the_anchor_is_the_only_thing_refusing_that_shape() -> None:
    """Proves the test above is not resting on a second, accidental reason.

    Reaches into ``_OPTION_RE`` deliberately -- it is the *precondition* that
    has to be true for the anchor to be the thing under test. All three lines
    match the option grammar, they are contiguous and they start at 1, so the
    run is fully buildable and only the trailing chrome stops it. Asserted
    against the regex rather than against ``parse``'s output so that this stays
    true after the residual defect is fixed.
    """
    text = read("capture-idle-numbered-recap", "detection").replace(
        "\u25cf 1. README.md", "  1. README.md"
    )
    numbers = [
        int(m.group(1)) for line in text.splitlines() if (m := _OPTION_RE.match(line))
    ]
    assert numbers == [1, 2, 3], (
        "the option run is no longer buildable from this capture, so "
        "test_a_numbered_recap_with_the_terminal_below_it_is_refused would "
        "pass without exercising the end-of-viewport anchor"
    )
    assert text.splitlines()[-1].strip(), "chrome really does follow the last option"


def test_a_menu_with_ordinary_prose_below_it_is_refused() -> None:
    """The anchor's rule in one sentence, with no fixture to hide behind."""
    text = (
        "Which one?\n\n"
        "1. Force-push\n"
        "2. Open a PR instead\n"
        "3. Do nothing\n\n"
        "I went with option 2 and opened the PR.\n"
    )
    assert BlockedPrompt.parse(text).is_choice is False


def test_only_blanks_rules_and_key_hints_may_follow_a_real_menu() -> None:
    """The complement, from the real menu: the anchor must not be tightened.

    ``capture-blocked-permission-write`` ends with a blank line, a key-hint
    footer ("Esc to cancel \u00b7 Tab to amend") and a trailing newline after its
    last option, and it is a genuine live menu. An anchor that demanded the
    last option be the literal last line would refuse every real prompt.
    """
    raw = read("capture-blocked-permission-write", "detection")
    assert "Esc to cancel" in raw.split("3. No", 1)[1]
    assert BlockedPrompt.parse(raw).is_choice is True


# --------------------------------------------------------------------------
# What a CARD shows
# --------------------------------------------------------------------------

BLOCKED_CAPTURES = [
    p.name for p in capture_dirs() if p.name.startswith("capture-blocked-")
]


@pytest.mark.parametrize("name", BLOCKED_CAPTURES)
def test_context_is_verbatim_screen_text_and_never_the_question(name: str) -> None:
    """Every line a blocked CARD shows was on the screen, exactly as drawn.

    The card replaced its pane excerpt with ``prompt.context``. Escaping is the
    constraint the replacement has to survive: the app may never claim more than
    the transcript says, so each line here must appear in ``raw`` character for
    character rather than being a rendering of it -- and the question is never
    repeated, because the card prints that on its own line above.

    Empty is a legitimate answer and three captures give it: a plan approval, a
    file-write prompt and a multi-select whose only line above the options is
    its own navigation strip. Those cards show the question and the options and
    say nothing else, which is the honest reading of a screen that carries
    nothing else.
    """
    prompt = BlockedPrompt.parse(read(name, "detection"))
    for line in prompt.context:
        assert line in prompt.raw, f"{name}: {line!r} was never on the screen"
    for line in prompt.question.splitlines():
        assert line not in prompt.context


@pytest.mark.parametrize("name", BLOCKED_CAPTURES)
def test_context_is_short_enough_for_a_card(name: str) -> None:
    """The measurement behind card.html's six-line cap.

    Six is not a taste: it is the longest ``context`` in this corpus, so on
    every real capture we hold the card truncates nothing. If a new capture
    lands above it, this fails and the cap is a decision to take again with the
    new screen in front of you -- rather than a card that has quietly started
    hiding the tail of a command.
    """
    assert len(BlockedPrompt.parse(read(name, "detection")).context) <= 6
