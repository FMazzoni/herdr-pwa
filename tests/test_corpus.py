"""The interpret corpus: every real capture run through ``BlockedPrompt.parse``, asserting the right outcome."""

import re
from pathlib import Path

import pytest

from herdr_pwa.engine import (
    Agent,
    AgentStatus,
    BlockedPrompt,
    MenuShape,
    as_pane_id,
    as_session_id,
)
from herdr_pwa.jsontypes import JsonObject, as_object, loads
from tests.engine_seam import OPTION_RE

FIXTURES = Path(__file__).parent / "fixtures"

# Every capture is stored in all four of these, one file each.
SOURCES = ("visible", "recent", "recent-unwrapped", "detection")

# Provenance asserted, so a re-capture on a different manifest cannot pass as a parser regression.
MANIFEST_VERSION = "2026.08.13.1"
HERDR_VERSION = "0.8.0"
PROTOCOL = 19

# Captures from later batches, each with the manifest it really carries, declared rather than relaxed.
MANIFEST_OVERRIDES: dict[str, str] = {
    "capture-blocked-multiselect-checklist": "2026.08.21.1",
    "capture-blocked-plan-approval": "2026.08.21.1",
    # opencode defers screen detection to its own lifecycle hook, so herdr records
    # no manifest version for its panes; the explain output says "manifest: none unknown".
    "capture-blocked-opencode-permission": "unknown",
    "capture-blocked-opencode-question": "unknown",
    "capture-blocked-opencode-multiselect": "unknown",
    "capture-blocked-opencode-questions-tabs": "unknown",
}

# The same, for herdr version and protocol: recording 0.8.0/19 here would name a machine that never read the pane.
HERDR_OVERRIDES: dict[str, str] = {
    "capture-blocked-plan-approval": "0.8.2",
    "capture-blocked-opencode-permission": "0.9.0",
    "capture-blocked-opencode-question": "0.9.0",
    "capture-blocked-opencode-multiselect": "0.9.0",
    "capture-blocked-opencode-questions-tabs": "0.9.0",
}

PROTOCOL_OVERRIDES: dict[str, int] = {
    "capture-blocked-plan-approval": 20,
    "capture-blocked-opencode-permission": 22,
    "capture-blocked-opencode-question": 22,
    "capture-blocked-opencode-multiselect": 22,
    "capture-blocked-opencode-questions-tabs": 22,
}


def capture_dirs() -> list[Path]:
    return sorted(p for p in FIXTURES.glob("capture-*") if p.is_dir())


def read(name: str, source: str) -> str:
    return (FIXTURES / name / f"{source}.txt").read_text(encoding="utf-8")


def read_ansi(name: str) -> str:
    """The capture's colour half; the four text sources share one ANSI read."""
    return (FIXTURES / name / "visible.ansi").read_text(encoding="utf-8")


def _obj(value: object) -> JsonObject:
    found = as_object(value)
    assert found is not None
    return found


def meta(name: str) -> JsonObject:
    return _obj(loads((FIXTURES / name / "meta.json").read_text(encoding="utf-8")))


# Corpus shape and provenance

ALL_CAPTURES = [p.name for p in capture_dirs()]


def test_corpus_is_not_empty() -> None:
    assert ALL_CAPTURES, "no capture-* fixtures found; the corpus is the artifact"


@pytest.mark.parametrize("name", ALL_CAPTURES)
def test_capture_is_complete(name: str) -> None:
    """A capture without all four sources plus its explain output is not evidence."""
    for source in SOURCES:
        path = FIXTURES / name / f"{source}.txt"
        assert path.is_file(), f"{name}: missing {source}.txt"
        assert path.read_text(encoding="utf-8").strip(), f"{name}: {source}.txt is empty"
    for extra in ("explain.txt", "explain.json", "agent-get.json", "meta.json"):
        assert (FIXTURES / name / extra).is_file(), f"{name}: missing {extra}"


@pytest.mark.parametrize("name", ALL_CAPTURES)
def test_capture_provenance(name: str) -> None:
    """The manifest/herdr/protocol the capture was taken under is recorded."""
    m = meta(name)
    manifest = MANIFEST_OVERRIDES.get(name, MANIFEST_VERSION)
    assert _obj(m["detection_manifest"])["version"] == manifest
    assert m["herdr_version"] == HERDR_OVERRIDES.get(name, HERDR_VERSION)
    assert m["protocol"] == PROTOCOL_OVERRIDES.get(name, PROTOCOL)
    explain = (FIXTURES / name / "explain.txt").read_text(encoding="utf-8")
    assert manifest in explain
    assert f"state: {m['agent_status']}" in explain


@pytest.mark.parametrize("name", ALL_CAPTURES)
def test_detection_equals_visible(name: str) -> None:
    """``--source detection`` == ``--source visible``, on every manifest so far."""
    assert read(name, "detection") == read(name, "visible")


# The parse expectations

# name -> (question, ((number, label), ...)); empty options means it must NOT become tap targets.
MENUS: dict[str, tuple[str, tuple[tuple[int, str], ...]]] = {
    # A real live menu; options 4 and 5 are Claude Code's own affordances, not the agent's answers.
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
    # A Bash permission prompt: a different detection grammar that still renders as a 1/2/3 menu.
    "capture-blocked-permission-prompt": (
        "Do you want to proceed?",
        (
            (1, "Yes"),
            (
                2,
                "Yes, and don\u2019t ask again for: curl -sS https://example.com/demo-permission-probe",
            ),
            (3, "No"),
        ),
    ),
    # The trust dialog: its question sits two chrome paragraphs above the options.
    "capture-blocked-trust-folder": (
        (
            "Quick safety check: Is this a project you created or one you trust? "
            "(Like your own code, a well-known open source project, or work from "
            "your team). If\nnot, take a moment to review what's in this folder "
            "first."
        ),
        ((1, "Yes, I trust this folder"), (2, "No, exit")),
    ),
    # ExitPlanMode: its sub-line hint and its editor footer each alone used to make the screen parse as nothing.
    "capture-blocked-plan-approval": (
        "Claude has written up a plan and is ready to execute. Would you like to proceed?",
        (
            (1, "Yes, auto-accept edits"),
            (2, "Yes, manually approve edits"),
            (3, "Tell Claude what to change"),
        ),
    ),
}

# Captures that must parse to NO options; not all of them contain a numbered list.
NEGATIVES = (
    # An idle recap list followed by a question in prose.
    "capture-idle-claude-prompt",
    # A transcript ending on a numbered list; the prompt box and status bar after it are what save it.
    "capture-idle-numbered-recap",
    # A busy viewport with no numbered list: chrome and glyphs must not manufacture options.
    "capture-working-claude",
)

# The negatives that really contain a clean numbered run, so the guard below is not vacuous.
NUMBERED_NEGATIVES = (
    "capture-idle-claude-prompt",
    "capture-idle-numbered-recap",
)


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("name", sorted(MENUS))
def test_menu_captures_parse_to_options(name: str, source: str) -> None:
    """A real live menu yields ``is_choice`` and the right options, from any source."""
    question, options = MENUS[name]
    prompt = BlockedPrompt.parse(
        read(name, source), target=as_pane_id("w1:pX"), session=as_session_id("s")
    )
    assert prompt.is_choice is True
    assert prompt.shape is MenuShape.SINGLE_CHOICE
    assert prompt.corroborated is True
    assert [(o.number, o.label) for o in prompt.options] == list(options)
    # An option is a label, never a keystroke: a digit synthesised from a row is banned behaviour.
    assert not any(hasattr(o, "key") for o in prompt.options)
    assert not any(h.key.isdigit() for h in prompt.hints)
    assert prompt.question == question
    assert prompt.raw == read(name, source)
    assert prompt.target == "w1:pX"


# name -> ((number, label, checked), ...); checked None means no box was drawn, which is not "unticked".
CHECKLISTS: dict[str, tuple[str, tuple[tuple[int, str, bool | None], ...]]] = {
    # A multi-select with one row already ticked, which is what makes it evidence.
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
def test_checklist_captures_parse_to_rows_with_their_tick_state(name: str, source: str) -> None:
    """A real checklist yields its rows AND what the terminal drew on each."""
    question, rows = CHECKLISTS[name]
    prompt = BlockedPrompt.parse(read(name, source))
    assert prompt.shape is MenuShape.MULTI_SELECT
    assert prompt.corroborated is True
    assert [(o.number, o.label, o.checked) for o in prompt.options] == list(rows)
    assert prompt.question == question
    # The box belongs to `checked`, not glued to the front of the label.
    assert not any(o.label.startswith("[") for o in prompt.options)


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("name", NEGATIVES)
def test_non_menu_captures_yield_no_tap_targets(name: str, source: str) -> None:
    """No numbered list in finished transcript may become a tap target."""
    prompt = BlockedPrompt.parse(read(name, source))
    assert prompt.options == ()
    assert prompt.is_choice is False


@pytest.mark.parametrize("source", SOURCES)
def test_numbered_recap_is_refused_even_though_it_ends_the_transcript(
    source: str,
) -> None:
    """The recap capture really does contain a clean 1./2./3. run."""
    text = read("capture-idle-numbered-recap", source)
    assert "1. README.md" in text
    assert "2. pyproject.toml" in text
    assert "3. src/<package>/__init__.py" in text


# A numbered line as a reader sees it: deliberately laxer than OPTION_RE, to guard the fixtures.
_LIST_LINE_RE = re.compile(r"^\s*[●>\u276f*•\-]?\s*(\d{1,2})[.):]\s+\S")


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("name", NUMBERED_NEGATIVES)
def test_the_numbered_negatives_still_carry_a_numbered_list(name: str, source: str) -> None:
    """Every capture :data:`NUMBERED_NEGATIVES` names must really carry a 1..n run."""
    lines = read(name, source).splitlines()
    numbers = [int(m.group(1)) for line in lines if (m := _LIST_LINE_RE.match(line))]
    assert numbers[:3] == [1, 2, 3], f"{name}/{source}: no 1./2./3. run left"


def test_askuserquestion_trailing_affordances_are_not_answers() -> None:
    """Recorded gap: options 4 and 5 are UI affordances, not the agent's answers."""
    prompt = BlockedPrompt.parse(read("capture-blocked-askuserquestion-menu", "detection"))
    assert [o.label for o in prompt.options][3:] == [
        "Type something.",
        "Chat about this",
    ]


def test_permission_prompt_question_omits_the_command_being_approved() -> None:
    """Recorded gap: the question is "Do you want to proceed?" and nothing else."""
    prompt = BlockedPrompt.parse(read("capture-blocked-permission-prompt", "detection"))
    assert prompt.question == "Do you want to proceed?"
    assert "curl" not in prompt.question
    assert "curl -sS https://example.com/demo-permission-probe" in prompt.raw


@pytest.mark.parametrize("source", SOURCES)
def test_write_permission_prompt_options(source: str) -> None:
    """The file-write permission prompt: options parse, decoys are refused."""
    prompt = BlockedPrompt.parse(read("capture-blocked-permission-write", source))
    assert [(o.number, o.label) for o in prompt.options] == [
        (1, "Yes"),
        (2, "Yes, allow all edits during this session (shift+tab)"),
        (3, "No"),
    ]
    assert "hello" not in " ".join(o.label for o in prompt.options)


@pytest.mark.parametrize("source", SOURCES)
def test_write_permission_prompt_question(source: str) -> None:
    """The question for a file-write approval is the question, not the diff."""
    prompt = BlockedPrompt.parse(read("capture-blocked-permission-write", source))
    assert prompt.question == "Do you want to create notes.txt?"


def test_write_permission_prompt_question_excludes_the_diff_body() -> None:
    """Pins the fix precisely: no dashed rule, and no diff text, in the question."""
    prompt = BlockedPrompt.parse(read("capture-blocked-permission-write", "detection"))
    assert prompt.question == "Do you want to create notes.txt?"
    assert "\u254c" not in prompt.question
    assert "hello" not in prompt.question
    assert "\u254c" * 20 in prompt.raw
    assert "1 hello" in prompt.raw


@pytest.mark.parametrize("source", SOURCES)
def test_trust_dialog_asks_the_safety_question_not_the_link_label(source: str) -> None:
    """'Yes, I trust this folder' must not be offered under the wrong question."""
    prompt = BlockedPrompt.parse(read("capture-blocked-trust-folder", source))
    assert "Quick safety check" in prompt.question
    assert prompt.question != "Security guide"


def test_trust_dialog_chrome_is_skipped_but_not_swallowed() -> None:
    """The skipped chrome is dropped from the question, never merged into it."""
    prompt = BlockedPrompt.parse(read("capture-blocked-trust-folder", "detection"))
    assert "Security guide" not in prompt.question
    assert "read, edit, and execute files here" not in prompt.question
    assert "Security guide" in prompt.raw
    assert "Quick safety check: Is this a project you created or one you trust?" in prompt.raw


# The documented residual, on real bytes


def test_real_recap_survives_truncation_by_accident_not_by_design() -> None:
    """Slicing the terminal chrome off the recap capture does NOT reopen the residual."""
    text = read("capture-idle-numbered-recap", "detection")
    marker = "3. src/<package>/__init__.py"
    truncated = text[: text.index(marker) + len(marker)] + "\n"

    assert "\u25cf 1. README.md" in truncated
    prompt = BlockedPrompt.parse(truncated)
    assert prompt.is_choice is False


def residual_shape() -> str:
    """The documented residual, built from real bytes."""
    text = read("capture-idle-numbered-recap", "detection")
    marker = "3. src/<package>/__init__.py"
    truncated = text[: text.index(marker) + len(marker)] + "\n"
    return truncated.replace("\u25cf 1. README.md", "  1. README.md")


def test_the_residual_shape_is_built_the_way_it_claims_to_be() -> None:
    """Guards the test below from passing vacuously on a shape that no longer has the trap in it."""
    text = residual_shape()
    assert "  1. README.md" in text
    assert "\u25cf" not in text.split("README.md")[0].rsplit("\n", 1)[-1]
    assert "2. pyproject.toml" in text
    assert "3. src/<package>/__init__.py" in text
    assert "\u276f Reply with EXACTLY a numbered list" in text
    assert text.rstrip().endswith("3. src/<package>/__init__.py")


def test_a_transcript_ending_on_a_numbered_list_must_not_become_tap_targets() -> None:
    """A finished answer must never render as buttons."""
    prompt = BlockedPrompt.parse(residual_shape())
    assert prompt.options == ()
    assert prompt.is_choice is False
    assert prompt.corroborated is False
    assert prompt.shape is MenuShape.NONE


def test_a_lone_trailing_number_is_never_a_menu() -> None:
    """The minimum menu size, exercised against the real menu's own preamble."""
    prompt = BlockedPrompt.parse("Deploying now.\n\n1. Done\n")
    assert prompt.is_choice is False


# Holes a stub survey found: status, the option separator and the end-of-viewport anchor.

# Each group below was verified by deleting its protection and watching only its own tests go red.


# Status

# A literal on purpose: reading meta.json on both sides would compare a file with itself.
EXPECTED_STATUS: dict[str, AgentStatus] = {
    "capture-blocked-askuserquestion-menu": AgentStatus.BLOCKED,
    "capture-blocked-askuserquestion-wrapped": AgentStatus.BLOCKED,
    "capture-blocked-permission-prompt": AgentStatus.BLOCKED,
    "capture-blocked-permission-prompt-wrapped": AgentStatus.BLOCKED,
    "capture-blocked-multiselect-checklist": AgentStatus.BLOCKED,
    "capture-blocked-permission-write": AgentStatus.BLOCKED,
    "capture-blocked-plan-approval": AgentStatus.BLOCKED,
    "capture-blocked-trust-folder": AgentStatus.BLOCKED,
    "capture-blocked-opencode-permission": AgentStatus.BLOCKED,
    "capture-blocked-opencode-question": AgentStatus.BLOCKED,
    "capture-blocked-opencode-multiselect": AgentStatus.BLOCKED,
    "capture-blocked-opencode-questions-tabs": AgentStatus.BLOCKED,
    "capture-idle-claude-prompt": AgentStatus.IDLE,
    "capture-idle-numbered-recap": AgentStatus.IDLE,
    "capture-working-claude": AgentStatus.WORKING,
}


def agent_get(name: str) -> JsonObject:
    """The ``agents[]``-shaped payload from a capture's ``herdr agent get``."""
    raw = _obj(loads((FIXTURES / name / "agent-get.json").read_text(encoding="utf-8")))
    return _obj(_obj(raw["result"])["agent"])


def test_every_capture_has_a_declared_status() -> None:
    """The table above must cover the corpus, or the parametrized tests lie."""
    assert sorted(EXPECTED_STATUS) == sorted(ALL_CAPTURES)


@pytest.mark.parametrize("name", ALL_CAPTURES)
def test_capture_status_survives_the_model(name: str) -> None:
    """``Agent.from_raw`` must reproduce the status herdr actually reported."""
    agent = Agent.from_raw(agent_get(name))
    assert agent.status is EXPECTED_STATUS[name]
    assert agent.status is not AgentStatus.UNKNOWN


@pytest.mark.parametrize("name", ALL_CAPTURES)
def test_capture_status_agrees_with_the_rule_that_matched(name: str) -> None:
    """Three independent records of the same fact must not have drifted apart."""
    expected = EXPECTED_STATUS[name]
    explain = _obj(loads((FIXTURES / name / "explain.json").read_text(encoding="utf-8")))
    assert AgentStatus.coerce(meta(name)["agent_status"]) is expected
    assert AgentStatus.coerce(explain["state"]) is expected
    assert AgentStatus.coerce(agent_get(name)["agent_status"]) is expected
    text = (FIXTURES / name / "explain.txt").read_text(encoding="utf-8")
    assert f"state: {expected.value}" in text
    if name in OPENCODE_CAPTURES:
        # opencode defers detection to its own lifecycle hook: no rule matched, and
        # herdr records the deferral instead of pretending to have read the screen.
        assert explain["matched_rule"] is None
        assert explain["screen_detection_skipped"] is True
        assert "rule: none" in text
    else:
        assert AgentStatus.coerce(_obj(explain["matched_rule"])["state"]) is expected
        assert f"rule: {_obj(explain['matched_rule'])['id']} " in text


def test_the_corpus_spans_enough_statuses_to_kill_a_constant_classifier() -> None:
    """A constant answer must be wrong for at least two captures, whatever it is."""
    observed = {Agent.from_raw(agent_get(name)).status for name in ALL_CAPTURES}
    assert observed == {AgentStatus.BLOCKED, AgentStatus.IDLE, AgentStatus.WORKING}
    for constant in AgentStatus:
        wrong = [n for n in ALL_CAPTURES if EXPECTED_STATUS[n] is not constant]
        assert wrong, f"a classifier stuck on {constant} would pass the corpus"


def test_status_drives_the_urgency_order_the_feed_is_built_on() -> None:
    """Status is not decoration: ``rank`` is what puts blocked agents on top."""
    ranks = {name: Agent.from_raw(agent_get(name)).rank for name in ALL_CAPTURES}
    blocked = [n for n in ALL_CAPTURES if EXPECTED_STATUS[n] is AgentStatus.BLOCKED]
    idle = [n for n in ALL_CAPTURES if EXPECTED_STATUS[n] is AgentStatus.IDLE]
    working = [n for n in ALL_CAPTURES if EXPECTED_STATUS[n] is AgentStatus.WORKING]
    assert max(ranks[n] for n in blocked) < min(ranks[n] for n in working)
    assert max(ranks[n] for n in working) < min(ranks[n] for n in idle)


# OPTION_RE's separator: without it every gutter number and ordinal in prose becomes a tap target.

# The dashed rule around a file-diff body, taken from the real capture rather than retyped.
DIFF_RULE = "\u254c" * 156


def test_a_diff_gutter_is_not_a_menu() -> None:
    """Three gutter line numbers ending the viewport must not become buttons."""
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
    """The other half of the same hole: ordinals in a sentence, no punctuation."""
    text = "Here is the plan.\n\n1 clone the repo\n2 install deps\n3 run tests\n"
    assert BlockedPrompt.parse(text).is_choice is False


@pytest.mark.parametrize("separator", [".", ")", ":"])
def test_the_separators_a_real_menu_uses_are_accepted(separator: str) -> None:
    """The complement: the rule must stay lax enough for the shapes that exist."""
    text = (
        f"Proceed?\n\n\u276f 1{separator} Yes\n  2{separator} No\n\n"
        "Esc to cancel \u00b7 Enter to select\n"
    )
    prompt = BlockedPrompt.parse(text)
    assert [(o.number, o.label) for o in prompt.options] == [(1, "Yes"), (2, "No")]


def test_a_separator_needs_whitespace_before_the_label() -> None:
    """ "1.5" and "2.0" are numbers, not options one and two."""
    text = "Latency by build:\n\n1.5\n2.0\n3.1\n"
    assert BlockedPrompt.parse(text).is_choice is False


# The "a menu must END the viewport" anchor
@pytest.mark.parametrize("source", SOURCES)
def test_a_numbered_recap_with_the_terminal_below_it_is_refused(source: str) -> None:
    """The anchor, tested on its own rather than through a lucky glyph."""
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
    """Proves the test above is not resting on a second, accidental reason."""
    text = read("capture-idle-numbered-recap", "detection").replace(
        "\u25cf 1. README.md", "  1. README.md"
    )
    numbers = [int(m.group(1)) for line in text.splitlines() if (m := OPTION_RE.match(line))]
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
    """The complement, from the real menu: the anchor must not be tightened."""
    raw = read("capture-blocked-permission-write", "detection")
    assert "Esc to cancel" in raw.split("3. No", 1)[1]
    assert BlockedPrompt.parse(raw).is_choice is True


# What a card shows

BLOCKED_CAPTURES = [p.name for p in capture_dirs() if p.name.startswith("capture-blocked-")]


@pytest.mark.parametrize("name", BLOCKED_CAPTURES)
def test_context_is_verbatim_screen_text_and_never_the_question(name: str) -> None:
    """Every line a blocked CARD shows was on the screen, exactly as drawn."""
    prompt = BlockedPrompt.parse(read(name, "detection"))
    for line in prompt.context:
        assert line in prompt.raw, f"{name}: {line!r} was never on the screen"
    for line in prompt.question.splitlines():
        assert line not in prompt.context


@pytest.mark.parametrize("name", BLOCKED_CAPTURES)
def test_context_is_short_enough_for_a_card(name: str) -> None:
    """The measurement behind card.html's six-line cap."""
    assert len(BlockedPrompt.parse(read(name, "detection")).context) <= 6


# The opencode dialog captures. Their focus is drawn only as colour, so the
# answer path reads the pane twice: stripped text for the grammar, ANSI for the
# cursor. Text alone parses to what the ledger pipeline sees -- a refused run is
# an honest degraded state, not a defect -- and the same text WITH the ANSI
# parses to what the phone drives.

OPENCODE_CAPTURES = (
    "capture-blocked-opencode-permission",
    "capture-blocked-opencode-question",
    "capture-blocked-opencode-multiselect",
    "capture-blocked-opencode-questions-tabs",
)


@pytest.mark.parametrize("name", OPENCODE_CAPTURES)
def test_opencode_capture_carries_its_ansi_read(name: str) -> None:
    """The colour half of the answer path is part of the capture, or it is not evidence."""
    ansi = FIXTURES / name / "visible.ansi"
    assert ansi.is_file()
    assert ansi.read_text().strip()
    # The four sources are asserted byte-identical above, so one ANSI read serves them.
    assert "\x1b[48;2;" in ansi.read_text(), f"{name}: its ANSI read carries no background runs"


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("name", OPENCODE_CAPTURES)
def test_opencode_text_parse_is_honest_without_colour(name: str, source: str) -> None:
    """Without the ANSI read, opencode's dialogs degrade instead of guessing."""
    prompt = BlockedPrompt.parse(read(name, source))
    if name == "capture-blocked-opencode-multiselect":
        # The tick boxes are printable, so the checklist is claimed even colourless;
        # the cursor is not, so no row becomes a tap target.
        assert prompt.shape is MenuShape.MULTI_SELECT
        assert prompt.options == ()
    elif name == "capture-blocked-opencode-permission":
        # The buttons are earned from the border and the screen's own hints; only
        # WHICH one is in play needs the colour read.
        assert prompt.shape is MenuShape.BUTTONS
        assert [o.label for o in prompt.options] == ["Allow once", "Allow always", "Reject"]
        assert prompt.focus is None


PERMISSION = ("permission", ((1, "Allow once"), (2, "Allow always"), (3, "Reject")))


@pytest.mark.parametrize("source", SOURCES)
def test_opencode_permission_dialog_is_buttons_with_a_colour_focus(source: str) -> None:
    """The permission dialog: unnumbered buttons, arrows the offer, focus in colour."""
    name = "capture-blocked-opencode-permission"
    prompt = BlockedPrompt.parse(read(name, source), ansi=read_ansi(name))
    assert prompt.shape is MenuShape.BUTTONS
    assert [(o.number, o.label) for o in prompt.options] == list(PERMISSION[1])
    # The arrows are the address: no row earns a digit, and the hint verbs are the screen's own.
    assert prompt.focus == 0
    assert [h.key for h in prompt.hints] == ["left", "right", "enter"]
    assert [h.verb for h in prompt.hints] == ["select", "select", "confirm"]


@pytest.mark.parametrize("source", SOURCES)
def test_opencode_question_dialog_is_numbered_rows_answerable_by_digit(source: str) -> None:
    """opencode binds digits 1..9 to move-and-select; the rows earn their digits."""
    name = "capture-blocked-opencode-question"
    prompt = BlockedPrompt.parse(read(name, source), ansi=read_ansi(name))
    assert prompt.shape is MenuShape.SINGLE_CHOICE
    assert prompt.question == "Which database?"
    assert [(o.number, o.label) for o in prompt.options] == [
        (1, "PostgreSQL"),
        (2, "SQLite"),
        (3, "DuckDB"),
        (4, "Type your own answer"),
    ]
    assert prompt.focus == 0
    assert [h.verb for h in prompt.hints] == ["select", "select", "submit", "dismiss"]


def test_opencode_multiselect_rows_carry_their_ticks() -> None:
    """The checklist parses with the tick state the screen drew, cursor colour-only."""
    name = "capture-blocked-opencode-multiselect"
    prompt = BlockedPrompt.parse(read(name, "visible"), ansi=read_ansi(name))
    assert prompt.shape is MenuShape.MULTI_SELECT
    assert prompt.question == "Which databases do you want? (select all that apply)"
    # Row 1 was toggled on the rig before the capture, and the screen says so.
    assert [(o.number, o.checked) for o in prompt.options] == [
        (1, True),
        (2, False),
        (3, False),
        (4, False),
    ]
    assert prompt.focus == 0
    # A multi-question set names its tabs; one question plus Confirm is one step.
    assert prompt.stepper is not None
    assert prompt.stepper.steps == ("Databases",)


def test_opencode_tabs_strip_says_the_question_is_one_of_several() -> None:
    """The tabs strip lifts to the stepper: a row tap answers a step, not the set."""
    name = "capture-blocked-opencode-questions-tabs"
    prompt = BlockedPrompt.parse(read(name, "visible"), ansi=read_ansi(name))
    assert prompt.stepper is not None
    assert prompt.stepper.steps == ("Vibe", "Budget")
    assert prompt.stepper.answered == 0
    assert prompt.stepper.total == 2
