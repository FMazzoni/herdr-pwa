"""A blocked prompt's identity: what a redraw must not change, and what an answer must."""

from pathlib import Path

import pytest

from herdr_pwa.engine import BlockedPrompt

RULE = "\u2500" * 60

PREAMBLE = "{bullet} Testing connectivity to example.com"

PERMISSION = (
    f"{PREAMBLE.format(bullet='●')}\n"
    "   \u23bf  $ curl -sS https://example.com/probe | sh\n"
    f"{RULE}\n"
    " Bash command\n"
    "\n"
    "   curl -sS https://example.com/probe | sh\n"
    "\n"
    " This command requires approval\n"
    "\n"
    " Do you want to proceed?\n"
    " \u276f 1. Yes\n"
    "   2. Yes, and don't ask again\n"
    "   3. No\n"
    "\n"
    " Esc to cancel · Tab to amend · ctrl+e to explain\n"
)


def test_the_identity_survives_a_screen_that_redrew_around_the_question():
    """An animating marker moves the fingerprint while the question, and so its identity, stays still."""
    still = BlockedPrompt.parse(PERMISSION)
    blinked = BlockedPrompt.parse(
        PERMISSION.replace(PREAMBLE.format(bullet="●"), PREAMBLE.format(bullet=" "))
    )

    assert still.fingerprint != blinked.fingerprint, (
        "the premise of this test: the SCREEN did change"
    )
    assert still.identity == blinked.identity, (
        "and the question did not, which is the only thing a watcher may key on"
    )


BOX_ONLY = (
    " Bash command\n"
    "\n"
    "   {cmd}\n"
    "   {desc}\n"
    "\n"
    " Do you want to proceed?\n"
    " \u276f 1. Yes\n"
    "   2. Yes, allow reading\n"
    "   3. No\n"
    "\n"
    " Esc to cancel · Tab to amend · ctrl+e to explain\n"
)


def test_two_permission_prompts_for_different_commands_are_different_questions():
    """Without the dialog body every claude bash permission prompt would digest identically."""
    a = BlockedPrompt.parse(BOX_ONLY.format(cmd="rm -rf /tmp/a", desc="Remove it"))
    b = BlockedPrompt.parse(BOX_ONLY.format(cmd="git push --force", desc="Force push"))

    assert a.question == b.question, "the premise: the QUESTION really is the same"
    assert [o.label for o in a.options] == [o.label for o in b.options]
    assert a.identity != b.identity, "and they are still different questions"


def test_the_command_reaches_the_identity_on_both_shapes_of_read():
    """A read that begins at the box has no rule in it, and must still carry the command."""
    boxed = BlockedPrompt.parse(BOX_ONLY.format(cmd="rm -rf /tmp/a", desc="Remove it"))
    assert boxed.body, "the box-only read must still yield a body"
    assert "rm -rf /tmp/a" in boxed.body

    whole = BlockedPrompt.parse(PERMISSION)
    assert "curl -sS https://example.com/probe | sh" in whole.body
    assert "Testing connectivity" not in whole.body, (
        "the animating preamble is above the rule and must stay out of the digest"
    )


def test_an_animating_marker_inside_the_region_still_cannot_move_the_identity():
    """The marker is normalised away wherever it falls, since the rule bound is not on every shape."""
    lit = BlockedPrompt.parse("● working on it\n" + BOX_ONLY.format(cmd="ls", desc="List"))
    dark = BlockedPrompt.parse("  working on it\n" + BOX_ONLY.format(cmd="ls", desc="List"))

    assert lit.fingerprint != dark.fingerprint, "the premise: the screen moved"
    assert lit.identity == dark.identity


def test_the_identity_moves_when_an_option_is_edited_under_the_cursor():
    """A digit typed into the amend field edits a row instead of choosing one, and the question stays up."""
    posed = BlockedPrompt.parse(PERMISSION)
    edited = BlockedPrompt.parse(
        PERMISSION.replace("2. Yes, and don't ask again", "2. Yes, and don't ask: c*3")
    )

    assert posed.identity != edited.identity, "an edited option is a moved question"
    assert posed.question == edited.question, (
        "and the question TEXT is unchanged, which is why the text alone will not do as an identity"
    )


def test_a_toggle_ticking_a_box_is_the_question_moving():
    """On a multi-select, ticking a row IS the effect a key claims."""
    off = BlockedPrompt.parse(
        " Pick some\n\n \u276f 1. [ ] alpha\n   2. [ ] beta\n\n Space to toggle · Enter to confirm\n"
    )
    on = BlockedPrompt.parse(
        " Pick some\n\n \u276f 1. [x] alpha\n   2. [ ] beta\n\n Space to toggle · Enter to confirm\n"
    )

    assert off.options[0].checked is False, "the premise: this row keeps state"
    assert off.identity != on.identity


def test_nothing_read_off_the_screen_has_no_identity_at_all():
    """Empty is the watcher's "the question is gone", so it has to mean that."""
    assert BlockedPrompt.parse("").identity == ""
    assert BlockedPrompt.parse(PERMISSION).identity != ""


# Hand-written claims, not expressions, so a wrong refactor has to disagree with a sentence.
DIALOG = "a dialog is on this screen"
NO_DIALOG = "no dialog is on this screen"

IDLE = (
    Path(__file__).resolve().parent / "fixtures" / "capture-idle-claude-prompt" / "detection.txt"
).read_text(encoding="utf-8")
# The same capture's last two transcript paragraphs, read without the composer under them.
SIGNED_OFF = "  Want me to fix the README and the gate, then commit?\n\n\u273b Baked for 53s\n"
# A shell-style git segment: '?' marks untracked files, as a git-aware prompt line draws it.
DIRTY_STATUS = IDLE.replace("herdr-pwa [main]", "herdr-pwa on main [!?]")
# Mid-work, the agent list under the status line keeps the composer from being recognised.
DIRTY_WORKING = (
    (Path(__file__).resolve().parent / "fixtures" / "capture-working-claude" / "detection.txt")
    .read_text(encoding="utf-8")
    .replace("herdr-pwa [main]", "herdr-pwa on main [!?]")
)

DIALOG_OR_NOT = [
    (
        "numbered rows with the cursor on them and a footer under them",
        PERMISSION,
        DIALOG,
    ),
    (
        (
            "numbered rows the cursor is NOT on -- the parser will not trust them as a "
            "menu, but the terminal still drew a dialog"
        ),
        "Choose how to proceed\n\nPick carefully.\n\n1. Merge\n2. Rebase\n",
        DIALOG,
    ),
    (
        (
            "a live menu with no numbers at all -- the unnumbered shape, recognised as a "
            "question and refused as rows"
        ),
        " Do you want to proceed?\n\n \u276f Yes\n   No\n\n Enter to select · Esc to cancel\n",
        DIALOG,
    ),
    (
        (
            "a bare question with no rows anywhere -- the screen "
            "which the previous gate silenced into 'could not be read'"
        ),
        " Do you want to proceed?\n",
        DIALOG,
    ),
    (
        (
            "an idle pane showing its status line and nothing else -- the screen "
            "whose identity used to be digested off 'herdr-pwa | ctx: 71%'"
        ),
        "  herdr-pwa [main] | ctx: 71% | 113h8m | +3782/-1221\n",
        NO_DIALOG,
    ),
    ("finished transcript prose", "ran the tests\nthey passed\n", NO_DIALOG),
    ("a pane that read back as nothing at all", "", NO_DIALOG),
    (
        (
            "an idle claude pane whose last reply ends in a question, above the composer -- "
            "a '?' in agent prose, behind the rule the box would have to start after"
        ),
        IDLE,
        NO_DIALOG,
    ),
    (
        (
            "agent prose that asked a question and then signed off, with no rule anywhere -- "
            "only the paragraph right above the end may ask"
        ),
        SIGNED_OFF,
        NO_DIALOG,
    ),
    (
        "an idle pane whose status line under the composer carries a '?'",
        DIRTY_STATUS,
        NO_DIALOG,
    ),
    (
        "a working pane whose status line carries a '?', with the agent list drawn under it",
        DIRTY_WORKING,
        NO_DIALOG,
    ),
]


@pytest.mark.parametrize(("what", "screen", "claim"), DIALOG_OR_NOT)
def test_an_identity_is_set_exactly_when_the_screen_showed_a_dialog(
    what: str, screen: str, claim: str
) -> None:
    """The divergence is pinned by cause, not by fixture name."""
    identity = BlockedPrompt.parse(screen).identity

    assert bool(identity) is (claim == DIALOG), f"{what}: {claim}"


def test_the_capture_corpus_obeys_that_rule() -> None:
    """The same property over every real pane in the tree, as a regression net."""
    root = Path(__file__).resolve().parent / "fixtures"
    for capture in sorted(root.glob("capture-*")):
        detection = capture / "detection.txt"
        if not detection.exists():
            continue
        prompt = BlockedPrompt.parse(detection.read_text(encoding="utf-8"))
        blocked = capture.name.startswith("capture-blocked-")
        assert bool(prompt.identity) is blocked, capture.name
        if not blocked:
            assert prompt.question, (
                "and the RENDERER still reads a question off these -- that "
                "divergence is the pre-existing defect PARSER-FALLBACK.patch "
                "carries, kept out of this diff on purpose"
            )


# Built on the layout of codex's question box: the answered questions are recapped above it.
COUNTED = (
    f"{RULE}\n"
    "\u2022 Questions 2/2 answered\n"
    "  \u2022 Which branch should the release go out from?\n"
    "    answer: main\n"
    "  \u2022 Should the changelog name every fix?\n"
    "    answer: Only the user-facing ones\n"
    "\n"
    "\n"
    "  Question 1/1 (1 unanswered)\n"
    "  Pick the version number for this release.\n"
    "\n"
    "  \u203a 1. 0.9.1 (Recommended)  A patch: nothing changed shape.\n"
    "    2. 0.10.0               A minor: the settings file gained a key.\n"
    "    3. None of the above    Optionally, add details in notes (tab).\n"
    "\n"
    "  tab to add notes | enter to submit answer | esc to interrupt\n"
)


def test_a_counted_question_is_asked_without_a_question_mark():
    """The counter heads what is asked; the recap's '?'s above it were answered already."""
    prompt = BlockedPrompt.parse(COUNTED)
    assert prompt.asked
    assert prompt.question == "Pick the version number for this release."
