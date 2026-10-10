"""The chat must never freeze silently, and must never cry wolf.

The bug this guards is a chat that reads a DEAD FILE. herdr reported session
``dde1047d``; the app read ``dde1047d.jsonl`` -- 19.2 MB, last written 00:55 --
while the conversation ran on in ``41faf851.jsonl``, which gained 277 KB in the
five minutes a sampler watched the named file gain nothing. The rendered chat
looked complete and had stopped ninety minutes earlier. It said nothing.

Every test here is a synthetic poll sequence -- a list of ``(size, status)``
pairs fed to the state machine with a fake clock -- because that is exactly what
the server hands it per tick, and it is the only way to test two minutes of
silence in a millisecond.

WHAT WOULD MAKE THIS WRONG is a note on a healthy agent, or a note printed over
one of the three honest empty states this app already renders. Both are worse
than the freeze: the first teaches the reader to ignore the note, and the second
contradicts a true sentence sitting directly above it.
"""

from __future__ import annotations

from datetime import datetime

from herdr_pwa.engine.transcript import HistoryState
from herdr_pwa.stall import QUIET_SECONDS, StallNote, StallWatch

SESSION = "00000000-0000-4000-8000-000000000141"
LOG = "00000000-0000-4000-8000-000000000141"
OTHER = "00000000-0000-4000-8000-000000000042"


class Clock:
    """A hand-wound clock, so two minutes of silence costs no wall time."""

    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now

    def tick(self, seconds: float) -> None:
        self.now += seconds


def run(
    watch: StallWatch,
    clock: Clock,
    polls: list[tuple[int, str]],
    *,
    every: float = 3.0,
    log: str = LOG,
    state: HistoryState = HistoryState.ok,
) -> StallNote | None:
    """Feed *polls* in at *every* seconds apart; answer the last verdict."""
    note: StallNote | None = None
    for size, status in polls:
        note = watch.observe(SESSION, log=log, size=size, status=status, state=state)
        clock.tick(every)
    return note


def watching() -> tuple[StallWatch, Clock]:
    clock = Clock()
    return StallWatch(clock=clock), clock


# ---------------------------------------------------------------------------
# It fires on the real shape
# ---------------------------------------------------------------------------


def test_two_completed_turns_over_a_frozen_record_say_the_record_may_have_moved() -> None:
    """The measured shape of the bug: ``WWWWDDDD`` with the byte count flat.

    A completed turn ALWAYS appends -- an agent cannot finish one without
    writing its assistant message -- so a completed turn with no growth is the
    wrong file rather than a slow one. Two of them, past the floor, is the rule.
    """
    watch, clock = watching()
    polls = [(19_236_145, "working")] * 4 + [(19_236_145, "done")] * 4
    polls += [(19_236_145, "working")] * 4 + [(19_236_145, "done")] * 4
    note = run(watch, clock, polls, every=10.0)
    assert note is not None
    assert note.turns == 2


def test_one_completed_turn_is_not_enough() -> None:
    """A single transition can be an artefact of sampling across a status flap."""
    watch, clock = watching()
    polls = [(500, "working")] * 4 + [(500, "done")] * 40
    assert run(watch, clock, polls, every=10.0) is None


def test_two_turns_inside_the_floor_do_not_fire() -> None:
    """Measured: the transcript lands in BURSTS.

    A turn can complete a beat before its bytes are visible, so the floor is
    what keeps a healthy agent from being accused at the moment it finishes.
    """
    watch, clock = watching()
    polls = [(500, "working")] * 3 + [(500, "done")]
    polls += [(500, "working")] * 3 + [(500, "done")]
    assert run(watch, clock, polls, every=1.0) is None, "eight seconds is not two minutes"


def test_a_quiet_agent_never_fires_however_long_it_is_quiet() -> None:
    """Six panes x 80 samples: zero completed turns, so zero opportunity to fire.

    This is the false positive that killed the pure-timer design. A healthy
    agent inside a twenty-minute build is silent for twenty minutes.
    """
    watch, clock = watching()
    assert run(watch, clock, [(500, "idle")] * 400, every=10.0) is None


def test_a_long_tool_call_with_no_completed_turn_never_fires() -> None:
    """Measured: 80 consecutive ``working`` across ~30 tool calls."""
    watch, clock = watching()
    assert run(watch, clock, [(500, "working")] * 400, every=10.0) is None


# ---------------------------------------------------------------------------
# A status FLAP is not a completed turn
# ---------------------------------------------------------------------------
#
# The invariant this detector rests on -- "a completed turn always appends" --
# is true of TURNS and was being read off herdr's STATUS FIELD, which is not
# The same thing. A measurement caught `docs-worker`, a healthy agent, going
# working -> idle -> working -> idle inside fifteen seconds, the second
# transition writing zero bytes. The flap fix's own gate, sampling the live herd at
# the app's 3 s working cadence, measured the same class of edge again.
#
# So an edge only counts once the agent has been SEEN working on
# WORKING_POLLS_BEFORE_TURN consecutive polls. Everything below is that rule,
# in the shapes it has to hold in.


def test_a_status_FLAP_is_not_a_completed_turn() -> None:
    """The measured false positive, in the shape it was measured in.

    Two flaps inside one quiet window is exactly what a healthy agent presents
    during a long tool call -- a build, a test run, an `rg` over a big tree --
    where the spinner stalls, herdr reads the pane as idle, and nothing is
    written because nothing has finished. Under the old rule that was two
    completed turns and a note accusing a working agent of having moved.
    """
    watch, clock = watching()
    polls = [(500, "working"), (500, "idle")] * 20
    assert run(watch, clock, polls, every=3.0) is None


def test_a_turn_that_ran_longer_than_the_sampler_still_counts() -> None:
    """The rule must not cost the note the case it exists for.

    The frozen-record incident is an agent finishing REAL turns, each of which
    runs for many polls, over a file that gains nothing.
    """
    watch, clock = watching()
    polls = [(500, "working")] * 3 + [(500, "done")]
    polls += [(500, "working")] * 3 + [(500, "done")]
    polls += [(500, "idle")] * 20
    note = run(watch, clock, polls, every=10.0)
    assert note is not None
    assert note.turns == 2


#: The measurement, replayed byte for byte. `docs-worker` was healthy and
#: working; the sampler ran at 5 s and read herdr's status field, and this is
#: what it recorded:
#:
#:     GREW            (t=501, working -> idle, +3787 bytes)
#:     !!NO GROWTH!!   (t=516, working -> idle,     0 bytes)
#:
#: Fifteen seconds, two transitions, the second writing nothing. Sizes are the
#: real deltas; the base is arbitrary because only the differences are read.
FLAP_BASE = 3_000_000
DOCS_WORKER_FLAP: list[tuple[int, str]] = (
    [(FLAP_BASE, "working")] * 4
    + [(FLAP_BASE + 3787, "idle")]  # t=501: a real turn, and it appended
    + [(FLAP_BASE + 3787, "working")]  # the spinner comes back
    + [(FLAP_BASE + 3787, "idle")]  # t=516: NOT a turn. Zero bytes.
)


def test_the_MEASURED_flap_produces_no_verdict_however_long_it_is_left() -> None:
    """The counterexample, pinned as a test rather than as a sample.

    This is the sequence that falsified "a completed turn always appends" --
    not because the invariant is wrong about turns, but because the signal
    being read was herdr's STATUS FIELD, and a flap is not a turn. Under the
    old rule the second transition counted, and two of these inside one quiet
    window is a note accusing a working agent of having moved its conversation.

    A sample expires; this does not. The window is held open well past
    QUIET_SECONDS so nothing here passes merely because the floor was pending.
    """
    watch, clock = watching()
    note = run(watch, clock, DOCS_WORKER_FLAP, every=5.0)
    assert note is None
    clock.tick(QUIET_SECONDS * 3)
    held = watch.observe(
        SESSION,
        log=LOG,
        size=FLAP_BASE + 3787,
        status="idle",
        state=HistoryState.ok,
    )
    assert held is None, "no amount of silence turns a flap into a completed turn"


def test_the_measured_flap_REPEATED_still_produces_no_verdict() -> None:
    """Two flaps in one quiet window is the exact false positive that mattered.

    `TURNS_BEFORE_NOTE = 2` and `QUIET_SECONDS = 120` do not close it on their
    own: a long tool call -- a build, a test run, an `rg` over a big tree --
    presents as a spinner that stalls, is read as idle, and writes nothing,
    twice. Under the old rule that was two completed turns and a note.
    """
    watch, clock = watching()
    flap = [(FLAP_BASE + 3787, "working"), (FLAP_BASE + 3787, "idle")]
    polls = DOCS_WORKER_FLAP + flap * 6 + [(FLAP_BASE + 3787, "idle")] * 40
    assert run(watch, clock, polls, every=5.0) is None


def test_the_SAME_window_still_fires_when_the_turns_are_real() -> None:
    """The control, so the two tests above cannot pass by being merely quiet.

    Same file, same base, same cadence, same silence -- and turns that actually
    ran, each seen working across more than one poll. This must fire, or the
    flap tests are proving nothing about flaps.
    """
    watch, clock = watching()
    polls = [(FLAP_BASE, "working")] * 4 + [(FLAP_BASE, "idle")]
    polls += [(FLAP_BASE, "working")] * 4 + [(FLAP_BASE, "idle")]
    polls += [(FLAP_BASE, "idle")] * 40
    note = run(watch, clock, polls, every=5.0)
    assert note is not None
    assert note.turns == 2


def test_the_working_run_is_CONSECUTIVE_and_does_not_accumulate() -> None:
    """Two separate single-poll flaps must not add up to one qualifying turn."""
    watch, clock = watching()
    polls = [(500, "working"), (500, "idle"), (500, "working"), (500, "done")]
    polls += [(500, "idle")] * 40
    assert run(watch, clock, polls, every=5.0) is None


def test_growth_does_not_manufacture_a_working_run_out_of_one_poll() -> None:
    """Growth resets `turns` and the clock; the working run is a different fact.

    `turns` and `grew_at` are evidence about the RECORD and a byte falsifies
    them. The working run is evidence about the AGENT, and it is carried across
    growth on purpose -- zeroing it there would make the note miss the first
    turn after every burst, which is the commonest turn there is. What it must
    not do is get one for free: one working poll is one working poll.
    """
    watch, clock = watching()
    polls = [(500, "working"), (600, "working"), (600, "idle")]
    polls += [(600, "idle")] * 40
    assert run(watch, clock, polls, every=5.0) is None


def test_a_working_run_SURVIVES_the_growth_it_was_earned_across() -> None:
    """The other half: a long real turn straddling a burst still counts.

    Four working polls, the file growing under two of them, then the record
    freezes and the agent finishes two turns over it. That is the incident.
    """
    watch, clock = watching()
    polls = [(500, "working"), (600, "working"), (700, "working"), (700, "done")]
    polls += [(700, "working")] * 3 + [(700, "done")]
    polls += [(700, "idle")] * 40
    note = run(watch, clock, polls, every=5.0)
    assert note is not None
    assert note.turns == 2


# ---------------------------------------------------------------------------
# It clears, unconditionally
# ---------------------------------------------------------------------------


def test_growth_clears_the_note_on_the_very_next_poll() -> None:
    """The note can never outlive its condition."""
    watch, clock = watching()
    polls = [(500, "working")] * 3 + [(500, "done")] * 2
    polls += [(500, "working")] * 3 + [(500, "done")] * 2
    assert run(watch, clock, polls, every=40.0) is not None
    cleared = watch.observe(SESSION, log=LOG, size=900, status="done", state=HistoryState.ok)
    assert cleared is None


def test_growth_resets_the_clock_as_well_as_the_count() -> None:
    """Not only the counter: a byte that lands restarts the two minutes too.

    Ten minutes of quiet, then growth, then two fast turns. Had the timer not
    restarted with the byte, those two turns would fire on the OLD silence.
    """
    watch, clock = watching()
    run(watch, clock, [(500, "working")] * 3 + [(500, "done")], every=300.0)
    watch.observe(SESSION, log=LOG, size=700, status="done", state=HistoryState.ok)
    quick = [(700, "working")] * 3 + [(700, "done")]
    quick += [(700, "working")] * 3 + [(700, "done")]
    assert run(watch, clock, quick, every=1.0) is None
    clock.tick(QUIET_SECONDS)
    late = watch.observe(SESSION, log=LOG, size=700, status="done", state=HistoryState.ok)
    assert late is not None, "the two turns still stand; only the floor was pending"


def test_a_record_that_SHRANK_is_something_happening_not_nothing() -> None:
    """A file smaller than it was was rewritten under us, which is activity."""
    watch, clock = watching()
    polls = [(9_000, "working")] * 3 + [(9_000, "done")] * 2
    polls += [(9_000, "working")] * 3 + [(9_000, "done")] * 2
    assert run(watch, clock, polls, every=40.0) is not None
    assert watch.observe(SESSION, log=LOG, size=40, status="done", state=HistoryState.ok) is None


def test_a_change_of_file_resets_every_counter() -> None:
    """After the resolution half, ``locate()`` may return a different, SMALLER file.

    Byte counts across two files mean nothing, so the watch is keyed on
    ``(session, log)`` and a change of log starts a fresh watch rather than
    reading the change as a loss of 15 MB.
    """
    watch, clock = watching()
    polls = [(19_000_000, "working")] * 3 + [(19_000_000, "done")] * 2
    polls += [(19_000_000, "working")] * 3 + [(19_000_000, "done")] * 2
    assert run(watch, clock, polls, every=40.0) is not None
    moved = watch.observe(SESSION, log=OTHER, size=4_000_000, status="done", state=HistoryState.ok)
    assert moved is None


# ---------------------------------------------------------------------------
# The SECOND cause, covered on purpose rather than by luck
# ---------------------------------------------------------------------------

PROBE = "aaaaaaaa-0001-4000-8000-000000000001"
"""A one-shot `claude -p` transcript: written once, never appended to again."""


def test_a_hijacked_pane_is_told_the_record_is_not_moving() -> None:
    """A nested `claude` can point herdr at a FOREIGN record, not an outgrown one.

    The hook reports the nested process's session against the inherited
    `HERDR_PANE_ID`. herdr 0.8.2 refuses that while the pane's agent lives, but
    it accepts it in the handover after that agent exits, and any box on 0.8.0
    accepts it always -- `docs/plans/nested-claude-hijack.md`.

    Nothing new is needed to notice: the reported session changes, the watch
    restarts against the new pair, and a twenty-kilobyte probe transcript that
    nobody will ever append to cannot grow across a completed turn. This pins
    that sequence under its own name so a state machine narrowed to background
    handoffs fails here rather than passing quietly.
    """
    watch, clock = watching()
    for size in (18_000, 19_000, 20_000):
        watch.observe(SESSION, log=LOG, size=size, status="working", state=HistoryState.ok)
        clock.tick(10.0)

    note = None
    polls = [(20_732, "working")] * 4 + [(20_732, "done")] * 4
    polls += [(20_732, "working")] * 4 + [(20_732, "done")] * 4
    for size, status in polls:
        note = watch.observe(PROBE, log=PROBE, size=size, status=status, state=HistoryState.ok)
        clock.tick(10.0)

    assert note is not None, "a working agent over a dead probe record says nothing"
    assert note.turns == 2


def test_the_hijack_is_not_announced_before_it_has_earned_it() -> None:
    """The same swap is what a legitimate RESTART in the pane looks like.

    An agent restarted in place also arrives as a new session over a new, tiny
    transcript, so the swap alone must buy no note at all -- only turns over a
    file that will not grow do. A review rejected "tiny and brand new while the
    pane has hours of history" for exactly this reason.
    """
    watch, clock = watching()
    run(watch, clock, [(19_000_000, "working")] * 3, every=10.0)
    swapped = watch.observe(PROBE, log=PROBE, size=20_732, status="working", state=HistoryState.ok)
    assert swapped is None
    clock.tick(3 * QUIET_SECONDS)
    assert watch.peek(PROBE, state=HistoryState.ok) is None


def test_a_hijack_into_another_project_root_stays_the_KNOWN_gap() -> None:
    """The residue, asserted rather than discovered later.

    A nested `claude` run from a different cwd writes its transcript under
    another project root, so the app resolves nothing and the state is
    ``absent`` -- which is SUPPRESSED, because every agent started by another
    agent inherits ``CLAUDE_CODE_CHILD_SESSION`` and would otherwise be accused
    forever. In that one case the chat's "nothing is lost from here" is wrong,
    and un-suppressing is the worse trade. Reachable only through the handover
    window in `docs/plans/nested-claude-hijack.md`.
    """
    watch, clock = watching()
    for _ in range(20):
        assert (
            watch.observe(PROBE, log="", size=0, status="working", state=HistoryState.absent)
            is None
        )
        clock.tick(60.0)


# ---------------------------------------------------------------------------
# The three honest empty states. A note here is a NEW lie.
# ---------------------------------------------------------------------------


def test_an_agent_with_no_transcript_at_all_is_never_accused() -> None:
    """Every agent started by another agent inherits ``CLAUDE_CODE_CHILD_SESSION``.

    Claude Code writes no transcript for those, the chat already says so, and
    "This conversation may have moved" printed under "Nothing is broken and
    nothing is lost from here" is exactly the kind of lie this ticket removes.
    A pane in the herd was live in this state while it was measured: 21
    consecutive samples returning no file at all.
    """
    watch, clock = watching()
    polls = [(0, "working")] * 3 + [(0, "done")] * 2
    polls += [(0, "working")] * 3 + [(0, "done")] * 2
    assert run(watch, clock, polls, every=40.0, state=HistoryState.absent) is None


def test_a_transient_read_failure_is_not_a_frozen_record() -> None:
    """A stat or open that failed reads as "no growth" and is not evidence."""
    watch, clock = watching()
    polls = [(0, "working")] * 3 + [(0, "done")] * 2
    polls += [(0, "working")] * 3 + [(0, "done")] * 2
    assert run(watch, clock, polls, every=40.0, state=HistoryState.unreadable) is None


def test_a_provider_with_no_reader_is_not_a_frozen_record() -> None:
    watch, clock = watching()
    polls = [(0, "working")] * 3 + [(0, "done")] * 2
    polls += [(0, "working")] * 3 + [(0, "done")] * 2
    assert run(watch, clock, polls, every=40.0, state=HistoryState.unsupported) is None


def test_a_suppressed_state_FORGETS_rather_than_merely_staying_quiet() -> None:
    """A transcript that appears later must not inherit counters from before it.

    Two turns are counted while the file is unreadable; the moment it becomes
    readable the watch must start from that file, not fire on what it counted
    while there was nothing to count.
    """
    watch, clock = watching()
    polls = [(0, "working")] * 3 + [(0, "done")] * 2
    polls += [(0, "working")] * 3 + [(0, "done")] * 2
    run(watch, clock, polls, every=40.0, state=HistoryState.unreadable)
    clock.tick(600)
    assert watch.observe(SESSION, log=LOG, size=0, status="done", state=HistoryState.ok) is None


# ---------------------------------------------------------------------------
# Looking is not polling
# ---------------------------------------------------------------------------


def test_a_look_reports_the_verdict_without_changing_it() -> None:
    """Opening the tab must show the note; it must not restart the measurement.

    A page load resolves its file by FOLLOWING the lineage and the live element
    does not, so folding a page load's size in would alternate between two
    files and clear the note every time somebody opened the chat.
    """
    watch, clock = watching()
    polls = [(500, "working")] * 3 + [(500, "done")] * 2
    polls += [(500, "working")] * 3 + [(500, "done")] * 2
    assert run(watch, clock, polls, every=40.0) is not None
    assert watch.peek(SESSION, state=HistoryState.ok) is not None
    assert watch.peek(SESSION, state=HistoryState.ok) is not None


def test_a_look_at_an_agent_with_no_transcript_forgets_it() -> None:
    watch, clock = watching()
    polls = [(500, "working")] * 3 + [(500, "done")] * 2
    polls += [(500, "working")] * 3 + [(500, "done")] * 2
    assert run(watch, clock, polls, every=40.0) is not None
    assert watch.peek(SESSION, state=HistoryState.absent) is None
    assert watch.peek(SESSION, state=HistoryState.ok) is None


def test_a_session_nobody_has_polled_says_nothing() -> None:
    watch, _ = watching()
    assert watch.peek("never-seen", state=HistoryState.ok) is None


def test_the_note_names_when_the_record_last_grew() -> None:
    """The reader gets a time, because "it stopped" without one is unactionable."""
    watch, clock = watching()
    started = clock.now
    polls = [(500, "working")] * 3 + [(500, "done")] * 2
    polls += [(500, "working")] * 3 + [(500, "done")] * 2
    note = run(watch, clock, polls, every=40.0)
    assert note is not None
    assert datetime.fromisoformat(note.since) == datetime.fromtimestamp(started)


def test_the_ledger_is_bounded() -> None:
    """Nothing here may grow without limit on a machine running a big herd."""
    from herdr_pwa.stall import MAX_ENTRIES

    watch = StallWatch(clock=Clock())
    for index in range(MAX_ENTRIES + 50):
        watch.observe(f"s{index}", log="l", size=1, status="idle", state=HistoryState.ok)
    assert len(watch._watched) == MAX_ENTRIES  # noqa: SLF001 - the point of the test


# ---------------------------------------------------------------------------
# What the reader actually sees
# ---------------------------------------------------------------------------


def test_the_note_is_pinned_at_the_end_of_the_log_and_points_at_the_pane() -> None:
    """Rendered, not merely decided. The end of the log is where the eye stops.

    It rides on the LIVE fragment, so the server re-decides it every tick and a
    record that resumes growing drops it on the next poll with no client state.
    """
    from herdr_pwa.engine.transcript import TranscriptPage
    from herdr_pwa.render import render_history

    page = TranscriptPage(state=HistoryState.empty, size=19_236_145, log=LOG)
    body = render_history(
        page,
        session=SESSION,
        target="w1:p3",
        stall=StallNote(since="2026-08-22T23:33:00", turns=2),
    )

    assert "This conversation may have moved." in body
    assert "23:33" in body
    assert "Pane tab" in body
    # It is INSIDE the live element rather than beside it, which is the
    # only place a note can be put and later be REMOVED -- `hx-swap="outerHTML"`
    # on `hx-target="this"` replaces that element and nothing else, so a note
    # drawn as a sibling stacked one copy per poll, forever. It is still the
    # last thing in the log, because the live element is.
    live = body.index('id="chat-live"')
    assert live < body.index("chat__note--stall")
    assert body.rindex("</li>") > body.index("chat__note--stall")
    assert "</li>" not in body[live : body.index("chat__note--stall")]
    # The count is the evidence the note stands on, so the note states it.
    assert "finished 2 turns" in body


def test_no_note_is_rendered_when_there_is_nothing_to_say() -> None:
    from herdr_pwa.engine.transcript import TranscriptPage
    from herdr_pwa.render import render_history

    body = render_history(
        TranscriptPage(state=HistoryState.ok, log=LOG), session=SESSION, target="w1:p3"
    )

    assert "may have moved" not in body
    assert "chat__note--stall" not in body


def test_the_note_never_appears_on_the_OLDER_history_fragment() -> None:
    """Scrolling back is not the end of the log, and the sentinel there is not live."""
    from herdr_pwa.engine.transcript import TranscriptPage
    from herdr_pwa.render import render_history

    body = render_history(
        TranscriptPage(state=HistoryState.ok, has_more=True, log=LOG),
        session=SESSION,
        target="w1:p3",
        direction="older",
        stall=StallNote(since="2026-08-22T23:33:00", turns=2),
    )

    assert "chat__note--stall" not in body


def test_a_gone_agent_is_never_accused_of_having_moved() -> None:
    """Nothing will be appended anywhere, so this is an end rather than a freeze."""
    from herdr_pwa.engine.transcript import TranscriptPage
    from herdr_pwa.render import render_agent_view

    body = render_agent_view(
        None,
        session=SESSION,
        gone=True,
        history=TranscriptPage(state=HistoryState.ok, log=LOG),
        stall=StallNote(since="2026-08-22T23:33:00", turns=2),
    )

    assert "may have moved" not in body
