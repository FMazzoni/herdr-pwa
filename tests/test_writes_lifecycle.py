"""Starting and ending agents, workspaces, and the one failure table."""

import dataclasses
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest
from pydantic import JsonValue

from herdr_pwa.audit import PROMPT, AuditUnavailableError, Entry, Write
from herdr_pwa.engine import (
    EngineError,
    EngineMisconfiguredError,
    EngineTimeoutError,
    EngineUnavailableError,
    InvalidResponseError,
    ProtocolMismatchError,
    RequestTooLargeError,
    TargetNotFoundError,
    WorkspaceId,
    as_pane_id,
    harness_kinds,
)
from herdr_pwa.settings import Settings
from herdr_pwa.writes.guard import MAX_PROMPT_CHARS
from herdr_pwa.writes.lifecycle import (
    AgentEnded,
    AgentStarted,
    EndAsk,
    StartRequest,
    agent_name,
    availability,
    declared_models,
    end_agent,
    launch_of,
    offered_kinds,
    on_path,
    start_agent,
    stranded_kinds,
)
from herdr_pwa.writes.prompt import (
    SETTLED_LOOKS,
    FirstPromptAsk,
    PromptAsk,
    PromptSent,
    send_first_prompt,
    send_prompt,
)
from herdr_pwa.writes.refusal import FAILURES, Refusal, Refused, failed_result, refusal_for
from herdr_pwa.writes.resolve import AuditTrail, Writer
from herdr_pwa.writes.workspaces import (
    WorkspaceCloseAsk,
    WorkspaceDone,
    close_workspace,
    create_workspace,
    rename_workspace,
)
from tests._fake_herd import P3, S1, S2, SETTLED, FakeHerd, Rig, capture, echoed, rig
from tests.engine_seam import OPENCODE_ASK

NEW = "a1b2c3d4-0000-4000-8000-000000000001"


def everywhere(_name: str) -> str:
    return "/usr/bin/x"


def start_rig(tmp_path: Path) -> Rig:
    herd = FakeHerd()
    herd.add_workspace("w1")
    herd.started_session = NEW
    return rig(tmp_path, herd)


def ask(tmp_path: Path, kind: str = "claude", model: str = "", **extra: str) -> StartRequest:
    base = StartRequest(name="Probe One", kind=kind, workspace_id="w1", cwd=str(tmp_path))
    return dataclasses.replace(base, model=model, **extra)


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
    assert outcome["phase"] == "outcome"
    assert outcome["result"] == "started:w1:p9"


def test_opencode_is_started_with_bash_set_to_ask(tmp_path: Path) -> None:
    r = start_rig(tmp_path)
    assert isinstance(
        start_agent(r.writer, ask(tmp_path, "opencode"), which=everywhere), AgentStarted
    )
    [tab] = r.herd.writes("tab.create")
    assert tab["env"] == {"OPENCODE_CONFIG_CONTENT": OPENCODE_ASK}
    assert '"bash":"ask"' in OPENCODE_ASK


def _stand_in(where: Path, name: str) -> Path:
    where.mkdir(parents=True, exist_ok=True)
    binary = where / name
    _ = binary.write_text("#!/bin/sh\n")
    binary.chmod(0o700)
    return binary


def test_a_harness_installed_for_the_user_but_off_the_path_is_stranded_not_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    mine = _stand_in(tmp_path / "home" / ".local" / "bin", "claude")
    service = tmp_path / "service-bin"
    _ = _stand_in(service, "opencode")
    monkeypatch.setenv("PATH", str(service))
    assert offered_kinds(on_path) == ("opencode",)
    [stranded] = stranded_kinds()
    assert (stranded.kind, stranded.stranded, stranded.offered) == ("claude", str(mine), False)
    # Once the service can see it, it is offered and no longer stranded.
    on_both = f"{service}:{mine.parent}"
    assert availability("claude", on_both).offered
    monkeypatch.setenv("PATH", on_both)
    assert stranded_kinds() == ()


def test_a_kind_herdr_runs_with_no_harness_here_is_never_offered(tmp_path: Path) -> None:
    found = availability("codex", str(_stand_in(tmp_path, "codex").parent))
    assert (found.known, found.offered) == (False, False)
    assert found.found.endswith("/codex")


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
    made = create_workspace(r.writer.client, "scratch", memory=r.writer.memory)
    assert made == WorkspaceDone(WorkspaceId("w7"), verified=True)
    renamed = rename_workspace(r.writer.client, "w7", "renamed", memory=r.writer.memory)
    assert renamed == WorkspaceDone(WorkspaceId("w7"), verified=True)
    assert (
        refused(rename_workspace(r.writer.client, "w7", "", memory=r.writer.memory)).refusal
        is Refusal.BAD_LABEL
    )
    assert (
        refused(rename_workspace(r.writer.client, "w404", "x", memory=r.writer.memory)).refusal
        is Refusal.NO_WORKSPACE
    )
    assert (
        refused(create_workspace(r.writer.client, "a\nb", memory=r.writer.memory)).refusal
        is Refusal.BAD_LABEL
    )


def test_an_empty_workspace_closes_on_one_confirm(tmp_path: Path) -> None:
    r = rig(tmp_path)
    r.herd.add_workspace("w2")
    assert refused(close_workspace(r.writer, WorkspaceCloseAsk("w2"))).refusal is Refusal.CONFIRM
    done = close_workspace(r.writer, WorkspaceCloseAsk("w2", confirmed=True))
    assert done == WorkspaceDone(WorkspaceId("w2"), verified=True)


def test_a_workspace_with_agents_needs_the_armed_count_it_was_shown(tmp_path: Path) -> None:
    r = end_rig(tmp_path)
    first = refused(close_workspace(r.writer, WorkspaceCloseAsk("w1", confirmed=True)))
    assert first.refusal is Refusal.CONFIRM
    assert first.screen
    assert "ends 1 agent" in first.reason
    r.herd.put("w1:p4", S2)
    moved = refused(_second_tap(r, first.screen))
    assert (moved.refusal, moved.status) == (Refusal.COUNT_CHANGED, 409)
    assert r.herd.writes("workspace.close") == []
    done = _second_tap(r, moved.screen)
    assert done == WorkspaceDone(WorkspaceId("w1"), verified=True)
    attempt = r.audit_records[0]
    assert attempt["phase"] == "attempt"
    assert attempt["endpoint"] == "close"


def _armed(r: Rig, workspace: str = "w1") -> str:
    first = refused(close_workspace(r.writer, WorkspaceCloseAsk(workspace, confirmed=True)))
    assert first.refusal is Refusal.CONFIRM
    return first.screen


def _second_tap(r: Rig, saw: str) -> WorkspaceDone | Refused:
    return close_workspace(r.writer, WorkspaceCloseAsk("w1", confirmed=True, armed=True, saw=saw))


def test_an_agent_swapped_under_the_same_count_refuses_the_armed_close(tmp_path: Path) -> None:
    r = end_rig(tmp_path)
    saw = _armed(r)
    # Same pane, same count of one: the idle agent shown was replaced by a working one.
    r.herd.put(P3, S2, status="working")
    swapped = refused(_second_tap(r, saw))
    assert (swapped.refusal, swapped.status) == (Refusal.COUNT_CHANGED, 409)
    assert r.herd.writes("workspace.close") == []
    assert isinstance(_second_tap(r, swapped.screen), WorkspaceDone)


def test_an_agent_that_started_working_since_the_confirm_refuses_it(tmp_path: Path) -> None:
    r = end_rig(tmp_path)
    saw = _armed(r)
    r.herd.put(P3, S1, status="working")
    assert refused(_second_tap(r, saw)).refusal is Refusal.COUNT_CHANGED
    assert r.herd.writes("workspace.close") == []


def test_the_armed_close_goes_through_when_nothing_moved(tmp_path: Path) -> None:
    r = end_rig(tmp_path)
    saw = _armed(r)
    assert _second_tap(r, saw) == WorkspaceDone(WorkspaceId("w1"), verified=True)


def test_every_refusal_has_one_row_in_the_failure_table() -> None:
    assert set(FAILURES) == set(Refusal)
    for failure in FAILURES.values():
        assert 200 <= failure.status < 600
        assert "{verb}" in failure.says


@pytest.mark.parametrize(
    ("exc", "refusal"),
    [
        (EngineUnavailableError("x"), Refusal.UNREACHABLE),
        (EngineTimeoutError("x"), Refusal.TIMED_OUT),
        (EngineMisconfiguredError("x"), Refusal.MISCONFIGURED),
        (ProtocolMismatchError("x"), Refusal.PROTOCOL_DRIFT),
        (RequestTooLargeError("x", size=2, limit=1), Refusal.TOO_LONG),
        (TargetNotFoundError("x"), Refusal.GONE),
        (TargetNotFoundError("x", code="session_moved"), Refusal.SESSION_MOVED),
        (EngineError("x", code="agent_not_ready"), Refusal.NOT_READY),
        (EngineError("x"), Refusal.RUNNER_REFUSED),
    ],
)
def test_engine_failures_map_onto_refusals(exc: EngineError, refusal: Refusal) -> None:
    assert refusal_for(exc) is refusal


def _sent(exc: EngineError) -> EngineError:
    exc.after_send = True
    return exc


@pytest.mark.parametrize(
    ("exc", "refusal"),
    [
        (_sent(EngineUnavailableError("reset")), Refusal.ANSWER_LOST),
        (_sent(InvalidResponseError("garbled")), Refusal.ANSWER_LOST),
        (_sent(EngineError("x", code="agent_not_ready")), Refusal.ANSWER_LOST),
        (_sent(EngineTimeoutError("slow")), Refusal.TIMED_OUT),
    ],
)
def test_a_failure_after_the_send_never_says_nothing_was_sent(
    exc: EngineError, refusal: Refusal
) -> None:
    said = Refused(refusal_for(exc), "sent")
    assert said.refusal is refusal
    assert said.unsure
    assert "Nothing was" not in said.says
    assert "may or may not have been sent" in said.says
    assert failed_result(exc) == f"unknown:{type(exc).__name__}"


def test_a_failure_before_the_send_says_nothing_was_sent() -> None:
    exc = EngineUnavailableError("no socket")
    said = Refused(refusal_for(exc), "sent")
    assert not said.unsure
    assert said.says.startswith("Nothing was sent")
    assert failed_result(exc) == "refused:EngineUnavailableError"


def test_names_are_folded_to_what_the_runner_takes() -> None:
    assert agent_name("  My Probe  ") == "my-probe"
    assert agent_name("123") == ""


# The new-agent form's first prompt.

NEW_PANE = "w1:p9"


def listening_rig(tmp_path: Path, *, session: str = NEW) -> Rig:
    r = start_rig(tmp_path)
    r.herd.started_session = session

    def listening(herd: FakeHerd, _params: dict[str, JsonValue]) -> None:
        herd.agents[NEW_PANE].update(interactive_ready=True, launch_pending=False)

    def typed(herd: FakeHerd, params: dict[str, JsonValue]) -> None:
        herd.screens[NEW_PANE] = [echoed(f"> {params['text']}")]

    r.herd.after["agent.start"] = listening
    r.herd.after["agent.prompt"] = typed
    r.herd.screens[NEW_PANE] = ["drawing", SETTLED]
    return r


def test_a_first_prompt_is_typed_once_the_new_agent_is_listening(tmp_path: Path) -> None:
    r = listening_rig(tmp_path)
    result = start_agent(r.writer, ask(tmp_path, prompt="fix the build"), which=everywhere)
    assert isinstance(result, AgentStarted)
    assert isinstance(result.first, PromptSent)
    assert result.first.verified
    [typed] = r.herd.writes("agent.prompt")
    assert typed["text"] == "fix the build"
    # Typed only after three identical reads of a ready pane, never at the half-drawn one.
    reads = [p for name, p in r.herd.calls if name == "agent.read"]
    assert len(reads) >= SETTLED_LOOKS


def test_a_late_identity_agent_is_prompted_by_address(tmp_path: Path) -> None:
    r = listening_rig(tmp_path, session="")
    result = start_agent(r.writer, ask(tmp_path, "hermes", prompt="hi"), which=everywhere)
    assert isinstance(result, AgentStarted)
    assert isinstance(result.first, PromptSent)
    [typed] = r.herd.writes("agent.prompt")
    assert "session" not in typed or not typed["session"]


def test_a_blank_first_prompt_sends_nothing(tmp_path: Path) -> None:
    r = listening_rig(tmp_path)
    result = start_agent(r.writer, ask(tmp_path, prompt="   "), which=everywhere)
    assert isinstance(result, AgentStarted)
    assert result.first is None
    assert r.herd.writes("agent.prompt") == []


def test_a_shell_command_first_prompt_starts_on_the_first_tap(tmp_path: Path) -> None:
    r = listening_rig(tmp_path)
    result = start_agent(r.writer, ask(tmp_path, prompt="rm -rf build"), which=everywhere)
    assert isinstance(result, AgentStarted)
    assert isinstance(result.first, PromptSent)


def test_an_oversized_first_prompt_creates_nothing(tmp_path: Path) -> None:
    r = listening_rig(tmp_path)
    text = "x" * (MAX_PROMPT_CHARS + 1)
    result = refused(start_agent(r.writer, ask(tmp_path, prompt=text), which=everywhere))
    assert result.refusal is Refusal.TOO_LONG
    assert r.herd.writes("tab.create") == []


def test_a_first_prompt_at_a_question_is_not_typed_but_the_agent_stays(tmp_path: Path) -> None:
    r = listening_rig(tmp_path)
    r.herd.screens[NEW_PANE] = [capture("capture-blocked-trust-folder", "visible")]
    result = start_agent(r.writer, ask(tmp_path, prompt="hello"), which=everywhere)
    assert isinstance(result, AgentStarted)
    assert isinstance(result.first, Refused)
    assert result.first.refusal is Refusal.QUESTION_SHOWING
    assert r.herd.writes("agent.prompt") == []
    assert r.herd.writes("pane.close") == []


SNAPSHOT = "session.snapshot"


# Every write takes the one-at-a-time lock: a second tab mid-write is refused, never sent twice.


def during[T](r: Rig, method: str, second: Callable[[], T]) -> list[T]:
    """Run ``second`` once, inside the first write, right after it calls ``method``."""
    seen: list[T] = []

    def other_tab(herd: FakeHerd, params: dict[str, JsonValue]) -> None:
        del herd.after[method], params
        seen.append(second())

    r.herd.after[method] = other_tab
    return seen


def in_flight(results: Sequence[object]) -> None:
    [late] = results
    assert (refused(late).refusal, refused(late).status) == (Refusal.IN_FLIGHT, 409)


def test_a_second_end_mid_end_is_refused_and_so_is_a_prompt(tmp_path: Path) -> None:
    r = end_rig(tmp_path)
    late = during(r, SNAPSHOT, lambda: end_agent(r.writer, EndAsk(S1, confirmed=True)))
    assert isinstance(end_agent(r.writer, EndAsk(S1, confirmed=True)), AgentEnded)
    in_flight(late)
    assert r.herd.writes("pane.close") == [{"pane_id": "w1:p3"}]
    r = end_rig(tmp_path)
    typed = during(r, SNAPSHOT, lambda: send_prompt(r.writer, PromptAsk(S1, "hello")))
    _ = end_agent(r.writer, EndAsk(S1, confirmed=True))
    in_flight(typed)


def test_a_second_start_into_the_same_workspace_is_refused(tmp_path: Path) -> None:
    r = start_rig(tmp_path)
    late = during(r, SNAPSHOT, lambda: start_agent(r.writer, ask(tmp_path), which=everywhere))
    assert isinstance(start_agent(r.writer, ask(tmp_path), which=everywhere), AgentStarted)
    in_flight(late)
    assert len(r.herd.writes("tab.create")) == 1


def test_a_workspace_cannot_be_closed_while_an_agent_starts_in_it(tmp_path: Path) -> None:
    r = start_rig(tmp_path)
    close = WorkspaceCloseAsk("w1", confirmed=True)
    late = during(r, "tab.create", lambda: close_workspace(r.writer, close))
    assert isinstance(start_agent(r.writer, ask(tmp_path), which=everywhere), AgentStarted)
    in_flight(late)
    assert r.herd.writes("workspace.close") == []


def test_the_opening_prompt_holds_the_new_pane(tmp_path: Path) -> None:
    r = listening_rig(tmp_path, session="")
    late = during(
        r,
        "agent.read",
        lambda: send_first_prompt(r.writer, FirstPromptAsk(as_pane_id(NEW_PANE), "x")),
    )
    _ = start_agent(r.writer, ask(tmp_path, kind="hermes", prompt="go"), which=everywhere)
    in_flight(late)
    assert len(r.herd.writes("agent.prompt")) == 1


def test_a_second_workspace_create_mid_create_is_refused(tmp_path: Path) -> None:
    r = rig(tmp_path)
    late = during(
        r,
        "workspace.create",
        lambda: create_workspace(r.writer.client, "b", memory=r.writer.memory),
    )
    assert isinstance(create_workspace(r.writer.client, "a", memory=r.writer.memory), WorkspaceDone)
    in_flight(late)
    assert len(r.herd.writes("workspace.create")) == 1


def test_a_second_rename_mid_rename_is_refused(tmp_path: Path) -> None:
    r = rig(tmp_path)
    r.herd.add_workspace("w2")
    late = during(
        r, SNAPSHOT, lambda: rename_workspace(r.writer.client, "w2", "b", memory=r.writer.memory)
    )
    assert isinstance(
        rename_workspace(r.writer.client, "w2", "a", memory=r.writer.memory), WorkspaceDone
    )
    in_flight(late)
    assert len(r.herd.writes("workspace.rename")) == 1


def test_a_second_workspace_close_mid_close_is_refused(tmp_path: Path) -> None:
    r = rig(tmp_path)
    r.herd.add_workspace("w2")
    close = WorkspaceCloseAsk("w2", confirmed=True)
    late = during(r, SNAPSHOT, lambda: close_workspace(r.writer, close))
    renamed = during(
        r,
        "workspace.close",
        lambda: rename_workspace(r.writer.client, "w2", "b", memory=r.writer.memory),
    )
    assert isinstance(close_workspace(r.writer, close), WorkspaceDone)
    in_flight(late)
    in_flight(renamed)
    assert len(r.herd.writes("workspace.close")) == 1


# A harness that declares a launch prompt flag takes its first prompt on its command line.


def test_only_harnesses_measured_to_take_a_launch_prompt_declare_it() -> None:
    declared = [
        kind for kind in harness_kinds() if (launch := launch_of(kind)) and launch.takes_prompt
    ]
    assert declared == ["opencode"]


def argv_rig(tmp_path: Path) -> Rig:
    r = listening_rig(tmp_path, session="")

    def opened_on_its_prompt(herd: FakeHerd, params: dict[str, JsonValue]) -> None:
        herd.agents[NEW_PANE].update(interactive_ready=True, launch_pending=False)
        args = params.get("args")
        if isinstance(args, list) and args:
            herd.screens[NEW_PANE] = [echoed(f"> {str(args[-1]).partition('=')[2]}")]

    r.herd.after["agent.start"] = opened_on_its_prompt
    return r


def test_opencode_takes_its_first_prompt_on_the_command_line(tmp_path: Path) -> None:
    r = argv_rig(tmp_path)
    attempts_at_spawn: list[list[str]] = []
    started_hook = r.herd.after["agent.start"]

    def spawned(herd: FakeHerd, params: dict[str, JsonValue]) -> None:
        attempts_at_spawn.append([str(rec.get("endpoint")) for rec in r.audit_records])
        started_hook(herd, params)

    r.herd.after["agent.start"] = spawned
    result = start_agent(r.writer, ask(tmp_path, "opencode", prompt="-v fix it"), which=everywhere)
    assert isinstance(result, AgentStarted)
    [start] = r.herd.writes("agent.start")
    # One argv element, so a prompt that starts with a dash is never read as an option.
    assert start["args"] == ["--prompt=-v fix it"]
    assert r.herd.writes("agent.prompt") == []
    assert isinstance(result.first, PromptSent)
    assert result.first.verified
    # The prompt was on disk before the process that carries it was spawned.
    assert attempts_at_spawn == [["start", "prompt"]]
    prompt_attempt = r.audit_records[1]
    assert (prompt_attempt["phase"], prompt_attempt.get("target")) == ("attempt", NEW_PANE)
    assert prompt_attempt.get("payload") is None
    outcomes = [rec for rec in r.audit_records if rec["phase"] == "outcome"]
    assert [rec.get("result") for rec in outcomes] == ["confirmed:echo", f"started:{NEW_PANE}"]
    # Echoed, but under eight characters once spaces go, so only its length is kept.
    assert outcomes[0].get("payload") is None


def test_a_launch_prompt_the_pane_never_shows_is_sent_unverified(tmp_path: Path) -> None:
    r = argv_rig(tmp_path)
    r.herd.after["agent.start"] = lambda herd, _params: herd.agents[NEW_PANE].update(
        interactive_ready=True, launch_pending=False
    )
    r.herd.screens[NEW_PANE] = [SETTLED]
    result = start_agent(r.writer, ask(tmp_path, "opencode", prompt="fix it"), which=everywhere)
    assert isinstance(result, AgentStarted)
    assert isinstance(result.first, PromptSent)
    assert not result.first.verified
    assert "accepted:argv" in [rec.get("result") for rec in r.audit_records]


class _PromptsUnrecordable:
    def __init__(self, inner: AuditTrail) -> None:
        self.inner = inner

    def record(self, write: Write, /) -> Entry:
        if write.endpoint == PROMPT:
            raise AuditUnavailableError("disk full")
        return self.inner.record(write)


def test_a_launch_prompt_that_cannot_be_recorded_spawns_nothing(tmp_path: Path) -> None:
    r = argv_rig(tmp_path)
    writer = Writer(
        r.writer.client,
        _PromptsUnrecordable(r.writer.audit),
        r.writer.pacing,
        memory=r.writer.memory,
    )
    result = refused(
        start_agent(writer, ask(tmp_path, "opencode", prompt="fix it"), which=everywhere)
    )
    assert (result.refusal, result.status) == (Refusal.UNRECORDABLE, 503)
    assert r.herd.writes("agent.start") == []
    assert r.herd.writes("pane.close") == [{"pane_id": NEW_PANE}]
    assert r.audit_records[-1].get("result") == "refused:unrecordable"


def test_a_launch_prompt_holds_the_new_pane(tmp_path: Path) -> None:
    r = argv_rig(tmp_path)
    started_hook = r.herd.after["agent.start"]
    late: list[object] = []

    def other_tab(herd: FakeHerd, params: dict[str, JsonValue]) -> None:
        started_hook(herd, params)
        late.append(send_first_prompt(r.writer, FirstPromptAsk(as_pane_id(NEW_PANE), "x")))

    r.herd.after["agent.start"] = other_tab
    _ = start_agent(r.writer, ask(tmp_path, "opencode", prompt="go"), which=everywhere)
    in_flight(late)
    assert r.herd.writes("agent.prompt") == []


def test_a_launch_prompt_fills_in_after_a_model() -> None:
    launch = launch_of("opencode")
    assert launch is not None
    assert launch.args("", "") == ()
    assert launch.args("m", "hi") == ("--model", "m", "--prompt=hi")


def test_a_launch_prompt_is_read_back_for_as_long_as_the_tui_takes_to_draw(tmp_path: Path) -> None:
    r = argv_rig(tmp_path)
    r.herd.after["agent.start"] = lambda herd, _params: herd.agents[NEW_PANE].update(
        interactive_ready=True, launch_pending=False
    )
    # Measured: opencode draws a launch prompt seconds after agent.start returns, not within one.
    r.herd.screens[NEW_PANE] = [""] * 8 + [echoed("> fix it")]
    result = start_agent(r.writer, ask(tmp_path, "opencode", prompt="fix it"), which=everywhere)
    assert isinstance(result, AgentStarted)
    assert isinstance(result.first, PromptSent)
    assert result.first.verified


def test_only_a_tab_launched_with_its_prompt_keeps_no_shell_history(tmp_path: Path) -> None:
    r = argv_rig(tmp_path)
    _ = start_agent(r.writer, ask(tmp_path, "opencode", prompt="fix it"), which=everywhere)
    [tab] = r.herd.writes("tab.create")
    # bash saves no history when HISTFILE is null, so the command line herdr typed is not kept.
    assert tab["env"] == {"OPENCODE_CONFIG_CONTENT": OPENCODE_ASK, "HISTFILE": ""}
    for kind, prompt in (("opencode", ""), ("claude", "fix it")):
        r = listening_rig(tmp_path)
        _ = start_agent(r.writer, ask(tmp_path, kind, prompt=prompt), which=everywhere)
        [tab] = r.herd.writes("tab.create")
        env = tab.get("env") or {}
        assert isinstance(env, dict)
        assert "HISTFILE" not in env


PICKLE = "opencode/big-pickle"


def declaring(r: Rig, *models: str) -> Writer:
    """The rig's writer, with the operator having declared *models* for opencode."""
    declared = declared_models(Settings(opencode_models=models))
    return dataclasses.replace(r.writer, declared=declared)


def test_opencode_starts_on_a_model_the_operator_declared(tmp_path: Path) -> None:
    r = start_rig(tmp_path)
    writer = declaring(r, "anthropic/claude-sonnet-4-5", PICKLE)
    result = start_agent(writer, ask(tmp_path, "opencode", model=PICKLE), which=everywhere)
    assert isinstance(result, AgentStarted)
    [start] = r.herd.writes("agent.start")
    assert (start["kind"], start["args"]) == ("opencode", ["--model", PICKLE])


@pytest.mark.parametrize(
    "model", ["opencode/BIG-PICKLE", "opencode/big", "big-pickle", f"{PICKLE}-2", f" {PICKLE}x"]
)
def test_a_model_is_offered_only_when_it_is_a_declared_id_exactly(
    tmp_path: Path, model: str
) -> None:
    r = start_rig(tmp_path)
    writer = declaring(r, PICKLE)
    result = refused(start_agent(writer, ask(tmp_path, "opencode", model=model), which=everywhere))
    assert result.refusal is Refusal.BAD_MODEL
    assert r.herd.writes("tab.create") == []


def test_with_no_models_declared_a_posted_opencode_model_is_refused_not_dropped(
    tmp_path: Path,
) -> None:
    r = start_rig(tmp_path)
    writer = declaring(r)
    result = refused(start_agent(writer, ask(tmp_path, "opencode", model=PICKLE), which=everywhere))
    assert result.refusal is Refusal.BAD_MODEL
    assert "not one offered for this kind" in result.says
    assert r.herd.writes("tab.create") == []


def test_a_declared_list_is_opencode_s_alone(tmp_path: Path) -> None:
    r = start_rig(tmp_path)
    writer = declaring(r, PICKLE)
    result = refused(start_agent(writer, ask(tmp_path, model=PICKLE), which=everywhere))
    assert result.refusal is Refusal.BAD_MODEL
    claude = launch_of("claude", writer.declared)
    assert claude is not None
    assert claude.models == ("fable", "opus", "sonnet", "haiku")


def test_a_declared_model_and_a_launch_prompt_are_each_their_own_argv_element(
    tmp_path: Path,
) -> None:
    r = argv_rig(tmp_path)
    writer = declaring(r, PICKLE)
    prompt = "-v fix it --model other"
    result = start_agent(
        writer, ask(tmp_path, "opencode", model=PICKLE, prompt=prompt), which=everywhere
    )
    assert isinstance(result, AgentStarted)
    [start] = r.herd.writes("agent.start")
    assert start["args"] == ["--model", PICKLE, f"--prompt={prompt}"]
    assert isinstance(result.first, PromptSent)


def test_every_declared_model_list_names_a_setting_that_exists() -> None:
    lists = Settings().model_lists()
    named = [
        launch.models_setting
        for kind in harness_kinds()
        if (launch := launch_of(kind)) and launch.models_setting
    ]
    assert named
    assert all(name in lists for name in named), named
    assert declared_models(Settings()) == {"opencode": ()}
