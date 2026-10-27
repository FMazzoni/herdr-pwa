"""A session doubted for where it started is trusted only when the pane's own process holds it.

The case these reproduce: a project folder renamed under a running Claude Code agent. herdr
reports the pane in the new folder, the session's log still says it started in the old one,
and the agent itself is the one the pane's keys reach.
"""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import JsonValue

from herdr_pwa.engine import Agent, EngineError, as_pane_id, as_session_id, holds_session
from herdr_pwa.writes.prompt import PromptAsk, PromptSent, send_prompt
from herdr_pwa.writes.refusal import Refusal, Refused
from tests._fake_herd import P3, SETTLED, FakeHerd, Rig, echoed, rig
from tests.engine_seam import ClaudeAdapter

OLD = "/home/user/projects/before"
NEW = "/home/user/projects/after"
REPO = "/home/user/projects/app"
SESSION = "5e550017-0000-0000-0000-000000000000"
OTHER = "00000090-0000-0000-0000-000000000000"
LEADER = 5001
NESTED = 5002
START = 12345
NAMESPACE = "pid:[4026531836]"


def _log(transcripts: Path, session: str, cwd: str) -> None:
    project = transcripts / "-home-user-projects-before"
    project.mkdir(parents=True, exist_ok=True)
    record = {"type": "user", "cwd": cwd, "sessionId": session, "uuid": "u1"}
    (project / f"{session}.jsonl").write_text(json.dumps(record) + "\n")


def _record(session: str, pid: int, **override: JsonValue) -> dict[str, JsonValue]:
    """A synthetic Claude Code process record, shaped as Claude Code writes one."""
    record: dict[str, JsonValue] = {
        "pid": pid,
        "sessionId": session,
        "cwd": OLD,
        "startedAt": 1_700_000_000_000,
        "procStart": str(START),
        "version": "0.0.0",
        "kind": "interactive",
        "entrypoint": "cli",
        "pidDomain": f"linux:00000000000000000000000000000000:{NAMESPACE}",
        "status": "idle",
    }
    record.update(override)
    return record


def _claims(base: Path, pid: int, body: str) -> None:
    sessions = base / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / f"{pid}.json").write_text(body)


def _process(proc: Path, pid: int, *, start: int = START, namespace: str = NAMESPACE) -> None:
    """A /proc entry for *pid*: its stat line, starting at *start*, and its pid namespace."""
    entry = proc / str(pid)
    (entry / "ns").mkdir(parents=True, exist_ok=True)
    fields = ["S", "1", str(pid), *["0"] * 16, str(start), "0"]
    (entry / "stat").write_text(f"{pid} (claude) {' '.join(fields)}\n")
    (entry / "ns" / "pid").symlink_to(namespace)


class Renamed:
    """A folder renamed under a live agent: the log says OLD, herdr says NEW."""

    def __init__(self, tmp_path: Path, *, cwd: str = NEW, session: str = SESSION) -> None:
        self.base = tmp_path / "claude"
        self.proc = tmp_path / "proc"
        _log(self.base / "projects", session, OLD)
        self.herd = FakeHerd()
        self.herd.put(P3, session, agent="claude", cwd=cwd, foreground_cwd=cwd)
        self.herd.screens[P3] = [SETTLED, echoed("\u276f hello")]
        r = rig(tmp_path, self.herd)
        self.rig: Rig = replace(r, writer=replace(r.writer, stores=self.stores, proc=self.proc))

    @property
    def stores(self) -> tuple[str, ...]:
        return (str(self.base / "projects"),)

    def held_by(self, pid: int, record: str) -> None:
        """The pane's terminal is led by *pid*, whose Claude Code record is *record*."""
        self.herd.leaders[P3] = pid
        _claims(self.base, pid, record)
        _process(self.proc, pid)

    def send(self, session: str = SESSION) -> PromptSent | Refused:
        return send_prompt(self.rig.writer, PromptAsk(as_session_id(session), "hello"))


def _refused(got: PromptSent | Refused, herd: FakeHerd) -> None:
    assert isinstance(got, Refused), got
    assert got.refusal is Refusal.DOUBTED_IDENTITY
    assert herd.writes("agent.prompt") == []


def test_a_renamed_folder_whose_pane_process_holds_the_session_is_sent_to(
    tmp_path: Path,
) -> None:
    case = Renamed(tmp_path)
    case.held_by(LEADER, json.dumps(_record(SESSION, LEADER)))
    assert isinstance(case.send(), PromptSent)
    assert len(case.herd.writes("agent.prompt")) == 1


def test_a_sibling_worktrees_session_on_a_pane_whose_process_does_not_hold_it_is_refused(
    tmp_path: Path,
) -> None:
    """Started in one worktree, reported on a pane in its sibling, held by that pane's own."""
    case = Renamed(tmp_path, cwd=f"{REPO}/.worktrees/b")
    _log(case.base / "projects", SESSION, f"{REPO}/.worktrees/a")
    case.held_by(LEADER, json.dumps(_record(OTHER, LEADER)))
    _refused(case.send(), case.herd)


def test_a_nested_agent_never_holds_the_pane(tmp_path: Path) -> None:
    """A child the Bash tool started runs in its own session, so it never leads the terminal."""
    case = Renamed(tmp_path)
    case.held_by(LEADER, json.dumps(_record(OTHER, LEADER)))
    _claims(case.base, NESTED, json.dumps(_record(SESSION, NESTED)))
    _process(case.proc, NESTED)
    _refused(case.send(), case.herd)


@pytest.mark.parametrize(
    "record",
    [
        pytest.param("", id="empty"),
        pytest.param("{not json", id="unreadable"),
        pytest.param(json.dumps({"session": SESSION, "pid": LEADER}), id="another-format"),
        pytest.param(json.dumps(_record(SESSION, LEADER, procStart=str(START + 1))), id="reused"),
        pytest.param(json.dumps(_record(SESSION, LEADER, procStart=START)), id="start-a-number"),
        pytest.param(
            json.dumps(_record(SESSION, LEADER, pidDomain="linux:0:pid:[1]")), id="other-namespace"
        ),
        pytest.param(json.dumps(_record(OTHER, LEADER)), id="another-session"),
    ],
)
def test_any_doubt_about_the_holder_keeps_the_refusal(tmp_path: Path, record: str) -> None:
    case = Renamed(tmp_path)
    case.held_by(LEADER, record)
    _refused(case.send(), case.herd)


def test_no_record_for_the_leader_keeps_the_refusal(tmp_path: Path) -> None:
    case = Renamed(tmp_path)
    case.herd.leaders[P3] = LEADER
    _process(case.proc, LEADER)
    _refused(case.send(), case.herd)


def test_a_leader_gone_from_proc_keeps_the_refusal(tmp_path: Path) -> None:
    case = Renamed(tmp_path)
    case.herd.leaders[P3] = LEADER
    _claims(case.base, LEADER, json.dumps(_record(SESSION, LEADER)))
    _refused(case.send(), case.herd)


def test_a_pane_led_by_its_shell_keeps_the_refusal(tmp_path: Path) -> None:
    """The shell leads once the agent has quit; a record for the shell's pid proves nothing."""
    case = Renamed(tmp_path)
    _refused(case.send(), case.herd)


def test_a_runner_that_will_not_say_keeps_the_refusal(tmp_path: Path) -> None:
    case = Renamed(tmp_path)
    case.held_by(LEADER, json.dumps(_record(SESSION, LEADER)))
    case.herd.fail["pane.process_info"] = EngineError("no process info")
    _refused(case.send(), case.herd)


def test_only_claude_code_records_a_holder(tmp_path: Path) -> None:
    _claims(tmp_path, LEADER, json.dumps(_record(SESSION, LEADER)))
    _process(tmp_path / "proc", LEADER)
    root = tmp_path / "projects"
    for kind, held in (("claude", True), ("opencode", False), ("hermes", False)):
        agent = Agent(session=as_session_id(SESSION), target=as_pane_id(P3), agent=kind)
        assert holds_session(agent, LEADER, root=root, proc=tmp_path / "proc") is held, kind


def test_a_harness_keeping_the_default_never_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check asks the harness's adapter, not its kind's name."""
    _claims(tmp_path, LEADER, json.dumps(_record(SESSION, LEADER)))
    _process(tmp_path / "proc", LEADER)
    monkeypatch.delattr(ClaudeAdapter, "holds")
    agent = Agent(session=as_session_id(SESSION), target=as_pane_id(P3), agent="claude")
    assert not holds_session(agent, LEADER, root=tmp_path / "projects", proc=tmp_path / "proc")
