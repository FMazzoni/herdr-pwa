"""Four outcomes, where "sent" was two.

The defect this file guards: the app reported a send it could not know had
landed, and the audit log — the only record of what was actually attempted —
agreed with it. ``sent`` meant *herdr took the bytes* and nothing more.

Two halves are tested here. The **watcher**, in isolation, with an injected
clock so a five-second budget costs nothing; and the **premise it stands on**,
which is that a question has an identity that survives its own screen being
redrawn. The premise was checked live before any of this was written — see
``docs/modules/engine/model/BlockedPrompt.identity.md`` for the measurement that
disqualified ``fingerprint`` — and what is pinned here is the shape of it, on
the same permission capture the rest of the suite uses.
"""

import itertools
from pathlib import Path

import pytest

from herdr_pwa import outcome as outcome_module
from herdr_pwa.engine import BlockedPrompt
from herdr_pwa.outcome import (
    ANSWER_BUDGET,
    ANSWER_INTERVAL,
    GONE_POLLS,
    MOVE_BUDGET,
    SAYS,
    SAYS_NOTHING,
    UNREADABLE,
    Evidence,
    Outcome,
    audit_result,
    says,
    watch,
)
from herdr_pwa.render import _read_anything

RULE = "\u2500" * 60
"""The horizontal rule claude draws above a dialog box.

It is load-bearing here rather than decoration: `_dialog_start` anchors
``BlockedPrompt.body`` on it, and with no rule anywhere above the options there
is no box, so the body is empty and the command never enters the identity. A
fixture without it is not the screen this app actually reads.
"""

PREAMBLE = "{bullet} Testing connectivity to example.com"
"""The assistant line that OWNS the pending tool call, and the thing that
animates: measured live, its leading marker alternates on a 1.20s square wave.
It sits ABOVE the rule, which is exactly why the body is anchored on the rule.
"""

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
    " ❯ 1. Yes\n"
    "   2. Yes, and don't ask again\n"
    "   3. No\n"
    "\n"
    " Esc to cancel · Tab to amend · ctrl+e to explain\n"
)


class Clock:
    """A monotonic clock that only moves when something sleeps on it."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def read(self) -> float:
        return self.now


def reads(*screens: object):
    """A watcher reader that hands back one thing per poll, then repeats the last."""
    queue = list(screens)

    def read() -> object:
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return read


# --------------------------------------------------------------------------
# The premise: what a redraw must not change, and what an answer must
# --------------------------------------------------------------------------


def test_the_identity_survives_a_screen_that_redrew_around_the_question():
    """Measured live: when the assistant message that owns a pending tool call is
    still animating its ``●``, ``fingerprint`` alternates on a 1.20s square wave
    while the question sits perfectly still, and a watcher keyed on it would call
    every write confirmed within one poll whatever the key did.

    HOW OFTEN that happens is unmeasured and deliberately not asserted here --
    ``scripts/flap_check.py`` reproduces both a screen that flaps and one that
    does not, and this project has already overstated the frequency once. What
    this test pins is the property that makes the frequency irrelevant: a redraw
    ANYWHERE outside the question must not move the identity, whatever caused it
    and however rarely it happens.
    """
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
    " ❯ 1. Yes\n"
    "   2. Yes, allow reading\n"
    "   3. No\n"
    "\n"
    " Esc to cancel · Tab to amend · ctrl+e to explain\n"
)
"""The OTHER shape a detection read comes in: the box alone, with no rule at all.

herdr's detection rules are priority-ordered and differ in region, so a read is
sometimes `whole_recent` (transcript, the animating line, and a rule in the
middle) and sometimes just the dialog. Both are real; both are measured; a body
rule that only handles one of them silently un-fixes the collision on the other,
which is what driving it live caught.
"""


def test_two_permission_prompts_for_different_commands_are_different_questions():
    """The review's finding 2, and it is not an exotic case: without the dialog
    body EVERY claude bash permission prompt digests identically, because the
    command sits above the dialog and the question is always "Do you want to
    proceed?" with the same three rows. A watcher that cannot tell one from the
    next reads a chained prompt as the SAME question for its whole budget.
    """
    a = BlockedPrompt.parse(BOX_ONLY.format(cmd="rm -rf /tmp/a", desc="Remove it"))
    b = BlockedPrompt.parse(BOX_ONLY.format(cmd="git push --force", desc="Force push"))

    assert a.question == b.question, "the premise: the QUESTION really is the same"
    assert [o.label for o in a.options] == [o.label for o in b.options]
    assert a.identity != b.identity, "and they are still different questions"


def test_the_command_reaches_the_identity_on_both_shapes_of_read():
    """A read that begins at the box has no rule in it, so a body bounded only
    by the rule is empty -- and every prompt collides again. Caught live.
    """
    boxed = BlockedPrompt.parse(BOX_ONLY.format(cmd="rm -rf /tmp/a", desc="Remove it"))
    assert boxed.body, "the box-only read must still yield a body"
    assert "rm -rf /tmp/a" in boxed.body

    whole = BlockedPrompt.parse(PERMISSION)
    assert "curl -sS https://example.com/probe | sh" in whole.body
    assert "Testing connectivity" not in whole.body, (
        "the animating preamble is above the rule and must stay out of the digest"
    )


def test_an_animating_marker_inside_the_region_still_cannot_move_the_identity():
    """Belt and braces, because the rule bound is not available on every shape:
    the marker is normalised away wherever it falls.
    """
    lit = BlockedPrompt.parse("● working on it\n" + BOX_ONLY.format(cmd="ls", desc="List"))
    dark = BlockedPrompt.parse("  working on it\n" + BOX_ONLY.format(cmd="ls", desc="List"))

    assert lit.fingerprint != dark.fingerprint, "the premise: the screen moved"
    assert lit.identity == dark.identity


def test_the_identity_moves_when_an_option_is_edited_under_the_cursor():
    """The live failure this whole ticket is named for. ``3`` meaning "No" was
    typed into a permission prompt whose focus had wandered into the amend
    field; it appended the digit to the pattern instead of choosing a row, and
    the question stayed up. Herdr reported the key delivered either way.
    """
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
        " Pick some\n\n ❯ 1. [ ] alpha\n   2. [ ] beta\n\n Space to toggle · Enter to confirm\n"
    )
    on = BlockedPrompt.parse(
        " Pick some\n\n ❯ 1. [x] alpha\n   2. [ ] beta\n\n Space to toggle · Enter to confirm\n"
    )

    assert off.options[0].checked is False, "the premise: this row keeps state"
    assert off.identity != on.identity


def test_nothing_read_off_the_screen_has_no_identity_at_all():
    """Empty is the watcher's "the question is gone", so it has to mean that."""
    assert BlockedPrompt.parse("").identity == ""
    assert BlockedPrompt.parse(PERMISSION).identity != ""


@pytest.mark.parametrize(
    "screen",
    [
        pytest.param("", id="nothing at all"),
        pytest.param(PERMISSION, id="a live permission prompt"),
        pytest.param(BOX_ONLY.format(cmd="ls", desc="List"), id="a box-only read"),
    ],
)
def test_an_identity_is_only_ever_set_on_a_screen_the_parser_read(screen: str):
    """One direction still holds absolutely: an identity implies a parse.

    THE CONVERSE NO LONGER HOLDS, deliberately, and that is round 2's finding 1.
    ``render._read_anything`` is the RENDERER's question -- did the parse come
    back with anything at all -- and it is True on a dialog-less claude pane,
    because ``_question``'s fallback promotes the status line there. ``identity``
    is the WATCHER's question -- is there a dialog to key on -- and it is now
    empty on exactly those screens.

    Round 1 pinned the two together as one predicate. They are not one predicate,
    and pretending they were is what let the watcher report "this agent is now
    waiting on a different question" off a pane with nothing on it. The renderer
    keeping its behaviour is a KNOWN pre-existing defect (`_question` has always
    promoted furniture); PARSER-FALLBACK.patch carries the fix and the reason it
    did not land inside this diff.
    """
    prompt = BlockedPrompt.parse(screen)
    if prompt.identity:
        assert _read_anything(prompt), "an identity over a parse that read nothing"


#: ``(what this screen IS, the screen, the claim being made about it)``.
#:
#: The third column is a SENTENCE, not a boolean and not an expression. A
#: cause-based test that computes its own expectation from the code under test
#: proves nothing -- it is a tautology that survives any refactor, including a
#: wrong one. These are hand-written claims about seven screens; the assertion
#: reads the claim, and someone changing the gate has to disagree with a
#: sentence rather than watch an expression follow them.
DIALOG = "a dialog is on this screen"
NO_DIALOG = "no dialog is on this screen"

DIALOG_OR_NOT = [
    (
        "numbered rows with the cursor on them and a footer under them",
        PERMISSION,
        DIALOG,
    ),
    (
        "numbered rows the cursor is NOT on -- the earned-shape rule will not trust them as a "
        "menu, but the terminal still drew a dialog",
        "Choose how to proceed\n\nPick carefully.\n\n1. Merge\n2. Rebase\n",
        DIALOG,
    ),
    (
        "a live menu with no numbers at all -- the unnumbered shape, recognised as a "
        "question and refused as rows",
        " Do you want to proceed?\n\n ❯ Yes\n   No\n\n Enter to select · Esc to cancel\n",
        DIALOG,
    ),
    (
        "a bare question with no rows anywhere -- the screen round 3 found, "
        "which the previous gate silenced into 'could not be read'",
        " Do you want to proceed?\n",
        DIALOG,
    ),
    (
        "an idle pane showing its status line and nothing else -- the screen "
        "whose identity used to be digested off 'herdr-pwa | ctx: 71%'",
        "  herdr-pwa [main] | ctx: 71% | 113h8m | +3782/-1221\n",
        NO_DIALOG,
    ),
    ("finished transcript prose", "ran the tests\nthey passed\n", NO_DIALOG),
    ("a pane that read back as nothing at all", "", NO_DIALOG),
]


@pytest.mark.parametrize(("what", "screen", "claim"), DIALOG_OR_NOT)
def test_an_identity_is_set_exactly_when_the_screen_showed_a_dialog(
    what: str, screen: str, claim: str
) -> None:
    """Round-3 finding 3: the divergence is pinned BY CAUSE, not by fixture name.

    `identity` answers "is there a dialog here to key a watcher on". Three
    independent things say yes -- the terminal drew numbered rows (trusted or
    not), the parser recognised a menu, or the dialog-box resolver found a
    question -- and a screen with none of them has nothing to key on.

    The round-2 gate had only the first two, which silenced a bare question with
    no rows: an answering key on that pane reported "the pane could not be read
    afterwards" about a pane that read perfectly well. Naming three captures
    covered three captures; this covers screens nobody has captured yet.
    """
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


# --------------------------------------------------------------------------
# The watcher
# --------------------------------------------------------------------------


def test_one_not_waiting_frame_is_not_enough_to_confirm_anything():
    """THE DEFECT THE REVIEW FOUND, and the reason ``GONE_POLLS`` exists.

    ``_watch_key`` decides "the question is over" from ``agent_status``, which is
    not a different kind of signal from reading the pane -- it is a
    priority-ordered TEXT MATCH over the same screen this ticket measured moving
    on its own. That field was measured flapping working->idle->working->idle
    in 15s with zero bytes written. One frame of it used to print ``confirmed``
    on the phone and into the audit log with the question still standing, which is
    strictly worse than the two-state model this replaces: it converts an honest
    ``unconfirmed`` into a false ``confirmed``.
    """
    clock = Clock()
    posed = BlockedPrompt.parse(PERMISSION)

    seen = watch(
        reads(None, posed, posed),
        aimed=posed.identity,
        ends=True,
        sleep=clock.sleep,
        clock=clock.read,
    )

    assert seen.outcome is not Outcome.CONFIRMED, "one inferred frame confirms nothing"
    # Round 2 changed what the non-confirmed answer IS. It used to be
    # `accepted:flicker` -- which is not held, so the clock swept the warning
    # away. The run saw the same question standing still for the rest of the
    # budget, so that is what it reports. @doc: docs/modules/outcome/_spent.md
    assert seen.outcome is Outcome.UNCONFIRMED
    assert seen.evidence is Evidence.UNMOVED


def test_a_question_that_is_gone_twice_running_confirms_an_answer():
    clock = Clock()
    posed = BlockedPrompt.parse(PERMISSION)

    seen = watch(
        reads(None),
        aimed=posed.identity,
        ends=True,
        sleep=clock.sleep,
        clock=clock.read,
    )

    assert seen.outcome is Outcome.CONFIRMED
    assert seen.evidence is Evidence.GONE
    assert seen.polls == GONE_POLLS, "earned twice, and no more than twice"
    assert seen.prompt is None, "nothing dead may come back tappable"
    assert clock.now <= ANSWER_INTERVAL * GONE_POLLS, (
        "corroboration costs one interval, not a budget"
    )


def test_a_blocked_agent_whose_screen_says_nothing_is_unread_not_gone():
    """The review's finding 1b. The honest state calls this "could not read the question";
    the log may not call the same condition ``confirmed``.
    """
    clock = Clock()
    posed = BlockedPrompt.parse(PERMISSION)
    blank = BlockedPrompt.parse("")

    assert blank.identity == "", "the premise: this parse read nothing"

    seen = watch(
        reads(blank),
        aimed=posed.identity,
        ends=True,
        sleep=clock.sleep,
        clock=clock.read,
    )

    assert seen.outcome is Outcome.ACCEPTED
    assert seen.evidence is Evidence.UNREAD
    assert seen.prompt is None


def test_an_answering_key_whose_question_is_still_up_is_unconfirmed():
    """Not an error and not a success: the third state, and the point of the outcome model."""
    clock = Clock()
    posed = BlockedPrompt.parse(PERMISSION)

    seen = watch(
        reads(posed),
        aimed=posed.identity,
        ends=True,
        sleep=clock.sleep,
        clock=clock.read,
    )

    assert seen.outcome is Outcome.UNCONFIRMED
    assert seen.evidence is Evidence.UNMOVED
    assert seen.prompt is posed, "the controls still belong to the question that stayed"
    assert sum(clock.slept) >= ANSWER_BUDGET - ANSWER_INTERVAL
    assert set(clock.slept) == {ANSWER_INTERVAL}


def test_an_answering_key_stops_the_moment_its_question_is_not_the_one_on_screen():
    """The review's 5.25s stall. A chained permission prompt reads MOVED for the
    whole budget, so waiting it out cost five seconds on a write that had landed.

    Once the question the key was aimed at is demonstrably not the question on
    screen, more polling cannot say more -- but it is still not CONFIRMED,
    because a keystroke swallowed into an amend field moves a question without
    answering it and nothing on the screen tells the two apart.
    """
    clock = Clock()
    posed = BlockedPrompt.parse(PERMISSION)
    other = BlockedPrompt.parse(PERMISSION.replace("curl -sS", "rm -rf ./build"))

    seen = watch(
        reads(other),
        aimed=posed.identity,
        ends=True,
        sleep=clock.sleep,
        clock=clock.read,
    )

    assert seen.outcome is Outcome.UNCONFIRMED
    assert seen.evidence is Evidence.MOVED
    assert clock.now == 0, "and it does not spend five seconds finding that out"


CHECKLIST = " Pick some\n\n ❯ 1. [ ] alpha\n   2. [ ] beta\n\n Space to toggle · Enter to confirm\n"


def test_a_navigating_key_is_confirmed_by_the_screen_moving_at_all():
    """It claims only that the screen responded, so movement is the whole claim.

    A toggle is the case where that is visible: the checklist reader reads the boxes, so a
    ticked row is a question this app can watch move without ever knowing where
    the highlight is.
    """
    clock = Clock()
    posed = BlockedPrompt.parse(CHECKLIST)
    ticked = BlockedPrompt.parse(CHECKLIST.replace("1. [ ]", "1. [x]"))

    seen = watch(
        reads(ticked),
        aimed=posed.identity,
        ends=False,
        sleep=clock.sleep,
        clock=clock.read,
    )

    assert seen.outcome is Outcome.CONFIRMED
    assert seen.evidence is Evidence.MOVED
    assert clock.slept == []


def test_a_highlight_this_app_cannot_see_moving_is_only_accepted():
    """`Down` on a single-choice list spends its whole budget by construction,
    and that is not a failure -- the app made no claim about that key.

    The marker really does move here and the identity really does not, because
    the parser strips it: prior-art pass 2 §4.2 found the same blindness across
    several harnesses, where focus is an ANSI background colour we throw away.
    Verified live too: twenty reads after a real ``down``, one identity
    throughout, while the screen digest changed underneath it.
    """
    clock = Clock()
    posed = BlockedPrompt.parse(PERMISSION)
    elsewhere = BlockedPrompt.parse(
        PERMISSION.replace(" ❯ 1. Yes", "   1. Yes").replace("   3. No", " ❯ 3. No")
    )

    assert elsewhere.fingerprint != posed.fingerprint, "the premise: the screen moved"

    seen = watch(
        reads(elsewhere),
        aimed=posed.identity,
        ends=False,
        sleep=clock.sleep,
        clock=clock.read,
    )

    assert seen.outcome is Outcome.ACCEPTED
    assert seen.evidence is Evidence.UNMOVED
    assert clock.now <= MOVE_BUDGET, "a key that claims nothing must not cost 5s"


def test_a_pane_that_cannot_be_read_establishes_nothing():
    """The conflation that would have re-created the original defect: a failed
    read arriving as ``None`` and being counted as a question that went away.
    """
    clock = Clock()
    posed = BlockedPrompt.parse(PERMISSION)

    seen = watch(
        reads(UNREADABLE),
        aimed=posed.identity,
        ends=True,
        sleep=clock.sleep,
        clock=clock.read,
    )

    assert seen.outcome is Outcome.ACCEPTED, "delivered, and nothing more"
    assert seen.evidence is Evidence.UNREAD
    assert seen.prompt is None, "nothing established, so nothing fresh to draw from"


# --------------------------------------------------------------------------
# What it is worth saying, and what it is worth logging
# --------------------------------------------------------------------------


#: Every ``(outcome, evidence)`` pair the KEYSTROKE path can produce. Kept as a
#: literal rather than derived, so that adding a road through ``watch`` has to
#: be argued for here and given a sentence rather than inheriting one.
#: ``(ACCEPTED, FLICKER)`` was here until round 3: the extra poll on an
#: uncorroborated gone frame made it unreachable, which
#: ``test_no_run_can_end_on_an_uncorroborated_gone_frame`` now asserts.
FROM_A_KEYSTROKE = frozenset(
    {
        (Outcome.CONFIRMED, Evidence.GONE),
        (Outcome.CONFIRMED, Evidence.MOVED),
        (Outcome.ACCEPTED, Evidence.UNMOVED),
        (Outcome.ACCEPTED, Evidence.UNREAD),
        (Outcome.UNCONFIRMED, Evidence.UNMOVED),
        (Outcome.UNCONFIRMED, Evidence.MOVED),
    }
)


def test_the_log_records_the_observation_beside_the_outcome():
    """One word, one meaning. The review found ``confirmed`` naming three."""
    assert audit_result(Outcome.CONFIRMED, Evidence.GONE) == "confirmed:gone"
    assert audit_result(Outcome.CONFIRMED, Evidence.MOVED) == "confirmed:moved"
    assert audit_result(Outcome.CONFIRMED, Evidence.ECHO) == "confirmed:echo"
    assert audit_result(Outcome.ACCEPTED, Evidence.UNREAD) == "accepted:unread"
    assert audit_result(Outcome.UNCONFIRMED, Evidence.MOVED) == "unconfirmed:moved"
    assert (
        audit_result(Outcome.PARTIALLY_APPLIED, Evidence.PARTIAL, done=3, total=5)
        == "partially_applied:3of5"
    )


def test_no_two_roads_to_one_state_write_the_same_token():
    """The whole of the review's finding 6, as an assertion rather than a habit."""
    tokens = [audit_result(o, e) for o, e in FROM_A_KEYSTROKE]
    assert len(set(tokens)) == len(tokens)


def test_an_unanchored_grep_for_confirmed_would_count_unconfirmed_too():
    """Why audit_result.md tells the reader to anchor on `"result":"`.

    This is not a defect to fix -- ``unconfirmed`` is the right word -- it is a
    trap to write down, because a vocabulary change whose whole point is
    countable evidence must not quietly double its own counts.
    """
    assert "confirmed" in audit_result(Outcome.UNCONFIRMED, Evidence.UNMOVED)
    assert '"result":"confirmed' not in '"result":"unconfirmed:unmoved"'


def test_sent_is_no_longer_a_word_this_vocabulary_knows():
    """It is the word that lied, and it meant two different things at once."""
    assert "sent" not in {outcome.value for outcome in Outcome}
    assert "sent" not in {evidence.value for evidence in Evidence}


@pytest.mark.parametrize("pair", sorted(FROM_A_KEYSTROKE))
def test_every_keystroke_outcome_has_a_sentence_a_person_can_act_on(pair):
    said = says(*pair, key="3")
    assert "{" not in said, "an unfilled placeholder reached the phone"
    assert said.startswith("Sent 3."), f"{pair} does not name the key"
    assert said.endswith("."), f"{pair} does not finish its sentence"


def test_the_only_states_without_a_sentence_are_the_ones_that_never_render():
    """``SAYS`` covers what a reader can see, and nothing is silently missing."""
    assert set(SAYS) >= FROM_A_KEYSTROKE, "a keystroke outcome with no sentence"
    assert (Outcome.PARTIALLY_APPLIED, Evidence.PARTIAL) in SAYS
    assert not (SAYS_NOTHING & set(SAYS)), (
        "a log-only pair must not also carry a sentence -- if it renders, it is not log-only"
    )


def test_the_unconfirmed_sentence_reads_as_neither_failure_nor_success():
    """The one that matters. It must not say the answer landed and it must not
    say it failed, because neither is known -- and the act it names is READ the
    pane, not re-send, because a blind re-send is how the live failure got worse.
    """
    said = says(Outcome.UNCONFIRMED, Evidence.UNMOVED, key="3")

    assert "still waiting on the same question" in said
    assert "nothing here can say it was answered" in said
    assert "Pane tab before sending it again" in said
    assert "failed" not in said and "could not be sent" not in said
    assert f"{int(ANSWER_BUDGET)} seconds" in said, (
        "the sentence quotes the budget, so the two cannot drift"
    )


def test_the_moved_sentence_admits_it_cannot_tell_the_two_cases_apart():
    """Answered-and-replaced and edited-in-place look identical on a screen.
    Relay can tell them apart because the harness hands it an event id.
    """
    said = says(Outcome.UNCONFIRMED, Evidence.MOVED, key="3")

    assert "waiting on a different question" in said
    assert "cannot be told apart" in said


def test_the_nav_key_confirmation_does_not_claim_the_question_ended():
    """The review's finding 6, on the phone rather than in the log: ticking a
    checkbox used to be reported as "no longer the one it is waiting on".
    """
    said = says(Outcome.CONFIRMED, Evidence.MOVED, key="1")

    assert "changed something" in said
    assert "no longer" not in said, "it is the same question with a box ticked"


def test_the_confirmed_sentence_says_the_check_was_made_twice():
    """``GONE_POLLS`` is the reason the word can be trusted, so it is said."""
    assert "twice" in says(Outcome.CONFIRMED, Evidence.GONE, key="1")


def test_the_accepted_sentences_never_read_as_success():
    for evidence in (Evidence.UNMOVED, Evidence.UNREAD, Evidence.FLICKER):
        said = says(Outcome.ACCEPTED, evidence, key="enter")
        assert "landed" not in said
        assert "delivered" in said.lower()


def test_the_unread_sentence_does_not_claim_a_read_that_did_not_happen():
    """The review's finding 5, and the exact class of lie this ticket removes:
    the sentence used to say "the pane was read back" on the ONE road where, by
    definition, it could not be.
    """
    said = says(Outcome.ACCEPTED, Evidence.UNREAD, key="1")

    assert "could not be read" in said
    assert "was read back" not in said

    other = says(Outcome.ACCEPTED, Evidence.UNMOVED, key="1")
    assert "read back" in other, "the road where it WAS read still says so"


def test_a_partial_application_leads_with_the_count_and_never_claims_a_clean_failure():
    said = says(Outcome.PARTIALLY_APPLIED, Evidence.PARTIAL, key="1", done=3, total=5)

    assert said.startswith("3 of 5 keys had already been delivered")
    assert "nothing here can put it back" in said


def test_the_sentences_are_not_the_audit_tokens():
    """A log grepped years from now and a sentence read on a phone are different
    artefacts; sharing one string would eventually cost one of them.
    """
    for pair, sentence in SAYS.items():
        assert sentence != audit_result(*pair)


# --------------------------------------------------------------------------
# Round 2: what a stray frame is allowed to do to a run's conclusion.
# @doc: docs/modules/outcome/_spent.md
# --------------------------------------------------------------------------


def test_one_stray_gone_frame_does_not_delete_the_warning():
    """Round-2 finding 2. `flickered` was sticky and `_spent` consulted it
    first, so a single not-waiting frame outranked every frame after it: an
    answering key whose question stood still for the whole budget came back
    `accepted:flicker`, which is NOT held, which the clock sweeps in one
    cadence. The two newest pieces of code worked against each other --
    GONE_POLLS refuses to TRUST a lone gone frame, and this let that same
    untrusted frame VETO the outcome the run actually observed.
    """
    clock = Clock()
    posed = BlockedPrompt.parse(PERMISSION)

    seen = watch(
        reads(None, posed),
        aimed=posed.identity,
        ends=True,
        sleep=clock.sleep,
        clock=clock.read,
    )

    assert seen.outcome is Outcome.UNCONFIRMED, "the question stood still; say so"
    assert seen.evidence is Evidence.UNMOVED
    assert seen.outcome in outcome_module.HOLDS, "and the warning must be held"


def test_a_deadline_landing_on_a_gone_frame_takes_one_more_poll():
    """Round-3 finding 1. Requiring a TRANSITION for `flickered` (round-2
    finding 3) did not kill that branch, it MOVED it: a budget expiring on a
    lone trailing gone frame fell through to the second branch and reported
    ``unconfirmed:unmoved`` -- "still waiting on the same question" -- when the
    last thing the herd said was that it is not. UNCONFIRMED is held, so that
    froze on screen.

    The answer is not a third way to settle on one frame; it is to spend one
    more poll, which is the principle GONE_POLLS already establishes.
    """
    posed = BlockedPrompt.parse(PERMISSION)
    full = int(ANSWER_BUDGET / ANSWER_INTERVAL)

    clock = Clock()
    waiting = watch(
        reads(*([posed] * full), None, posed),
        aimed=posed.identity,
        ends=True,
        sleep=clock.sleep,
        clock=clock.read,
    )
    assert waiting.outcome is Outcome.UNCONFIRMED, "the question really is still up"
    assert waiting.evidence is Evidence.UNMOVED, "and now that sentence is TRUE"

    clock = Clock()
    gone = watch(
        reads(*([posed] * full), None, None),
        aimed=posed.identity,
        ends=True,
        sleep=clock.sleep,
        clock=clock.read,
    )
    assert gone.outcome is Outcome.CONFIRMED, "corroborated, so it may say so"
    assert gone.evidence is Evidence.GONE
    assert gone.polls == waiting.polls, "both endings cost the same one extra poll"


def test_no_run_can_end_on_an_uncorroborated_gone_frame():
    """What the extra poll buys, as the property rather than the case.

    `_spent` is now only ever reached with `gone == 0`, so no watch concludes
    anything from a not-waiting frame it did not check twice. This guards
    against re-introducing a one-frame settle by a different door -- which is
    exactly how round 2's fix produced round 3's finding 1.
    """
    posed = BlockedPrompt.parse(PERMISSION)
    moved = BlockedPrompt.parse(PERMISSION.replace("curl -sS", "rm -rf ./build"))
    reached = set()
    for length in range(1, 6):
        for seq in itertools.product([None, posed, moved, UNREADABLE], repeat=length):
            for ends in (True, False):
                clock = Clock()
                reached.add(
                    watch(
                        reads(*seq),
                        aimed=posed.identity,
                        ends=ends,
                        sleep=clock.sleep,
                        clock=clock.read,
                    ).evidence
                )

    assert Evidence.FLICKER not in reached, (
        "FLICKER is unreachable BY CONSTRUCTION since round 3: every "
        "uncorroborated gone frame is resolved by the extra poll into `gone` or "
        "back into a question, so no run ends on a contradiction. It is kept as "
        "vocabulary, and this asserts nothing produces it again by accident."
    )
    assert reached == {
        Evidence.GONE,
        Evidence.MOVED,
        Evidence.UNMOVED,
        Evidence.UNREAD,
    }
