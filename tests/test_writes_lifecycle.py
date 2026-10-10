"""Starting and ending agents, workspaces, and the one failure table."""

from pathlib import Path

import pytest

from herdr_pwa.engine.transport import (
    EngineError,
    EngineMisconfigured,
    EngineTimeout,
    EngineUnavailable,
    ProtocolMismatch,
    RequestTooLarge,
    TargetNotFound,
)
from herdr_pwa.writes.lifecycle import (
    OPENCODE_ASK,
    AgentEnded,
    AgentStarted,
    EndAsk,
    StartRequest,
    agent_name,
    end_agent,
    start_agent,
)
from herdr_pwa.writes.refusal import FAILURES, Refusal, Refused, refusal_for
from herdr_pwa.writes.workspaces import (
    WorkspaceCloseAsk,
    WorkspaceDone,
    close_workspace,
    create_workspace,
    rename_workspace,
)
from tests._fake_herd import P3, S1, S2, FakeHerd, Rig, rig

NEW = "a1b2c3d4-0000-4000-8000-000000000001"


def everywhere(_name: str) -> str:
    return "/usr/bin/x"


def start_rig(tmp_path: Path) -> Rig:
    herd = FakeHerd()
    herd.add_workspace("w1")
    herd.started_session = NEW
    return rig(tmp_path, herd)


def ask(tmp_path: Path, kind: str = "claude", model: str = "", **extra: str) -> StartRequest:
    fields = {"name": "Probe One", "kind": kind, "workspace_id": "w1", "cwd": str(tmp_path)}
    fields.update(extra)
    return StartRequest(model=model, **fields)


def refused(result: object) -> Refused:
    assert isinstance(result, Refused), result
    return result


def test_claude_starts_with_its_model_in_a_new_tab(tmp_path: Path) -> None:
    r = start_rig(tmp_path)
    result = start_agent(r.writer, ask(tmp_path, model="haiku"), which=everywhere)
    assert isinstance(result, AgentStarted)
    assert result.session == NEW
    [tab] = r.herd.writes("tab.create")
    assert tab["label"] == "probe-one"
    assert "env" not in tab
    [start] = r.herd.writes("agent.start")
    assert (start["kind"], start["args"]) == ("claude", ["--model", "haiku"])
    outcome = r.audit_records[-1]
    assert outcome["result"] == "started:w1:p9"


def test_opencode_is_started_with_bash_set_to_ask(tmp_path: Path) -> None:
    r = start_rig(tmp_path)
    assert isinstance(
        start_agent(r.writer, ask(tmp_path, "opencode"), which=everywhere), AgentStarted
    )
    [tab] = r.herd.writes("tab.create")
    assert tab["env"] == {"OPENCODE_CONFIG_CONTENT": OPENCODE_ASK}
    assert '"bash":"ask"' in OPENCODE_ASK


def test_hermes_starts_with_no_session_yet(tmp_path: Path) -> None:
    r = start_rig(tmp_path)
    r.herd.started_session = ""
    result = start_agent(r.writer, ask(tmp_path, "hermes"), which=everywhere)
    assert isinstance(result, AgentStarted)
    assert result.session == ""
    assert r.clock.slept, "it waited a grace for an identity rather than sleeping for real"


@pytest.mark.parametrize(
    ("request_fields", "refusal"),
    [
        ({"name": "!!!"}, Refusal.BAD_NAME),
        ({"kind": "cursor"}, Refusal.BAD_KIND),
        ({"kind": "opencode", "model": "gpt-5"}, Refusal.BAD_MODEL),
        ({"model": "haiku; rm -rf /"}, Refusal.BAD_MODEL),
        ({"cwd": "relative/dir"}, Refusal.BAD_CWD),
        ({"cwd": "/no/such/dir/anywhere"}, Refusal.BAD_CWD),
        ({"workspace_id": "w404"}, Refusal.NO_WORKSPACE),
    ],
)
def test_an_unusable_form_starts_nothing(
    tmp_path: Path, request_fields: dict[str, str], refusal: Refusal
) -> None:
    r = start_rig(tmp_path)
    fields = {"kind": "claude", "model": "", **request_fields}
    result = refused(start_agent(r.writer, ask(tmp_path, **fields), which=everywhere))
    assert result.refusal is refusal
    assert r.herd.writes("tab.create") == []


def test_a_kind_not_on_this_machine_is_not_offered(tmp_path: Path) -> None:
    r = start_rig(tmp_path)
    result = refused(start_agent(r.writer, ask(tmp_path), which=lambda _name: None))
    assert result.refusal is Refusal.BAD_KIND


def test_a_start_that_fails_closes_the_pane_it_made(tmp_path: Path) -> None:
    r = start_rig(tmp_path)
    r.herd.fail["agent.start"] = EngineError("nope", code="spawn_failed")
    result = refused(start_agent(r.writer, ask(tmp_path), which=everywhere))
    assert result.refusal is Refusal.RUNNER_REFUSED
    assert r.herd.writes("pane.close") == [{"pane_id": "w1:p9"}]


def end_rig(tmp_path: Path, status: str = "idle") -> Rig:
    herd = FakeHerd()
    herd.put(P3, S1, status=status)
    return rig(tmp_path, herd)


def test_a_close_needs_a_confirm_the_library_enforces(tmp_path: Path) -> None:
    r = end_rig(tmp_path)
    assert refused(end_agent(r.writer, EndAsk(S1))).refusal is Refusal.CONFIRM
    assert r.herd.writes("pane.close") == []
    result = end_agent(r.writer, EndAsk(S1, confirmed=True))
    assert isinstance(result, AgentEnded)
    assert result.verified
    assert r.herd.writes("pane.close") == [{"pane_id": "w1:p3"}]


def test_a_working_agent_needs_a_second_different_acknowledgement(tmp_path: Path) -> None:
    r = end_rig(tmp_path, status="working")
    held = refused(end_agent(r.writer, EndAsk(S1, confirmed=True)))
    assert held.refusal is Refusal.ACKNOWLEDGE
    assert isinstance(
        end_agent(r.writer, EndAsk(S1, confirmed=True, acknowledged=True)), AgentEnded
    )


def test_a_close_into_a_recycled_pane_is_refused_on_the_wire(tmp_path: Path) -> None:
    r = end_rig(tmp_path)
    impostor = dict(r.herd.agents[P3])
    impostor["agent_session"] = {"agent": "claude", "kind": "id", "source": "x", "value": S2}
    r.herd.holders[P3] = impostor
    result = refused(end_agent(r.writer, EndAsk(S1, confirmed=True)))
    assert result.refusal is Refusal.SESSION_MOVED
    assert r.herd.writes("pane.close") == []


def test_workspace_create_and_rename_read_back(tmp_path: Path) -> None:
    r = rig(tmp_path)
    made = create_workspace(r.writer.client, "scratch")
    assert made == WorkspaceDone("w7", verified=True)
    renamed = rename_workspace(r.writer.client, "w7", "renamed")
    assert renamed == WorkspaceDone("w7", verified=True)
    assert refused(rename_workspace(r.writer.client, "w7", "")).refusal is Refusal.BAD_LABEL
    assert refused(rename_workspace(r.writer.client, "w404", "x")).refusal is Refusal.NO_WORKSPACE
    assert refused(create_workspace(r.writer.client, "a\nb")).refusal is Refusal.BAD_LABEL


def test_an_empty_workspace_closes_on_one_confirm(tmp_path: Path) -> None:
    r = rig(tmp_path)
    r.herd.add_workspace("w2")
    assert refused(close_workspace(r.writer, WorkspaceCloseAsk("w2"))).refusal is Refusal.CONFIRM
    done = close_workspace(r.writer, WorkspaceCloseAsk("w2", confirmed=True))
    assert done == WorkspaceDone("w2", verified=True)


def test_a_workspace_with_agents_needs_the_armed_count_it_was_shown(tmp_path: Path) -> None:
    r = end_rig(tmp_path)
    first = refused(close_workspace(r.writer, WorkspaceCloseAsk("w1", confirmed=True)))
    assert first.refusal is Refusal.CONFIRM
    assert first.screen == "1"
    assert "ends 1 agent" in first.reason
    r.herd.put("w1:p4", S2)
    moved = refused(
        close_workspace(r.writer, WorkspaceCloseAsk("w1", confirmed=True, armed=True, saw=1))
    )
    assert (moved.refusal, moved.status) == (Refusal.COUNT_CHANGED, 409)
    assert r.herd.writes("workspace.close") == []
    done = close_workspace(r.writer, WorkspaceCloseAsk("w1", confirmed=True, armed=True, saw=2))
    assert done == WorkspaceDone("w1", verified=True)
    assert r.audit_records[0]["endpoint"] == "close"


def test_every_refusal_has_one_row_in_the_failure_table() -> None:
    assert set(FAILURES) == set(Refusal)
    for failure in FAILURES.values():
        assert 200 <= failure.status < 600
        assert "{verb}" in failure.says


@pytest.mark.parametrize(
    ("exc", "refusal"),
    [
        (EngineUnavailable("x"), Refusal.UNREACHABLE),
        (EngineTimeout("x"), Refusal.TIMED_OUT),
        (EngineMisconfigured("x"), Refusal.MISCONFIGURED),
        (ProtocolMismatch("x"), Refusal.PROTOCOL_DRIFT),
        (RequestTooLarge("x", size=2, limit=1), Refusal.TOO_LONG),
        (TargetNotFound("x"), Refusal.GONE),
        (TargetNotFound("x", code="session_moved"), Refusal.SESSION_MOVED),
        (EngineError("x", code="agent_not_ready"), Refusal.NOT_READY),
        (EngineError("x"), Refusal.RUNNER_REFUSED),
    ],
)
def test_engine_failures_map_onto_refusals(exc: EngineError, refusal: Refusal) -> None:
    assert refusal_for(exc) is refusal


def test_names_are_folded_to_what_the_runner_takes() -> None:
    assert agent_name("  My Probe  ") == "my-probe"
    assert agent_name("123") == ""
