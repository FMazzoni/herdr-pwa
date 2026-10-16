"""Every write, driven end to end against the throwaway hp-test session; opt in with HP_LIVE=1."""

import os
from pathlib import Path

import pytest

from herdr_pwa.audit import AuditLog, Origin, Recorder
from herdr_pwa.engine import (
    VISIBLE_READ,
    Agent,
    AgentStatus,
    EngineSettings,
    HerdrClient,
    PaneId,
    SessionId,
)
from herdr_pwa.writes.answer import KeyAsk, KeySent, send_key, send_starting_key
from herdr_pwa.writes.keys import control_token
from herdr_pwa.writes.lifecycle import (
    AgentEnded,
    AgentStarted,
    EndAsk,
    StartRequest,
    end_agent,
    start_agent,
)
from herdr_pwa.writes.outcome import Outcome
from herdr_pwa.writes.prompt import PromptAsk, PromptSent, send_first_prompt, send_prompt
from herdr_pwa.writes.refusal import Refusal, Refused
from herdr_pwa.writes.resolve import Writer
from herdr_pwa.writes.workspaces import (
    WorkspaceCloseAsk,
    WorkspaceDone,
    close_workspace,
    create_workspace,
    rename_workspace,
)
from tests.engine_seam import OPENCODE_ASK
from tests.waiting import until

# Read at import, before the autouse fixture points HOME at a temp dir.
SOCKET = Path.home() / ".config/herdr/sessions/hp-test/herdr.sock"
LIVE = os.environ.get("HP_LIVE") == "1"

pytestmark = pytest.mark.skipif(not LIVE, reason="live herdr test; set HP_LIVE=1 to run it")


def say(label: str, text: str = "") -> None:
    print(f"\n=== {label}")  # noqa: T201 - the evidence this test exists to print
    if text:
        print(text)  # noqa: T201 - the evidence this test exists to print


def show(client: HerdrClient, pane: PaneId, label: str, rows: int = 14) -> str:
    screen = client.read(pane, VISIBLE_READ)
    tail = [line.rstrip() for line in screen.splitlines() if line.strip()][-rows:]
    say(f"pane {pane} after {label}", "\n".join(tail))
    return screen


def agent_at(client: HerdrClient, pane: PaneId) -> Agent | None:
    return client.snapshot().by_target(pane)


@pytest.fixture
def writer(tmp_path: Path) -> Writer:
    # Never the default session the phone is watching.
    assert "/sessions/hp-test/" in str(SOCKET), SOCKET
    assert SOCKET.exists(), "run `just live-session` first"
    client = HerdrClient(EngineSettings(socket_path=SOCKET))
    log = AuditLog(tmp_path / "state", max_bytes=1 << 20, payload_chars=512)
    return Writer(client, Recorder(log, Origin(actor="live-test", via="config")))


def _start(writer: Writer, workspace: str, kind: str, cwd: Path, model: str = "") -> AgentStarted:
    started = start_agent(
        writer, StartRequest(f"hpw-{kind}", kind, workspace, str(cwd), model=model)
    )
    say(f"start_agent {kind}", repr(started))
    assert isinstance(started, AgentStarted), started
    return started


def _claude_session(writer: Writer, pane: PaneId) -> SessionId:
    client = writer.client

    def settled() -> SessionId | None:
        agent = agent_at(client, pane)
        if agent is None:
            return None
        if agent.session:
            return agent.session
        if agent.status is AgentStatus.BLOCKED:
            show(client, pane, "claude stopped at launch")
            tapped = send_starting_key(writer, pane, "enter")
            say("send_starting_key enter", repr(tapped))
        return None

    return until(settled, seconds=90, step=2.0)


def _wait_status(client: HerdrClient, pane: PaneId, *wanted: AgentStatus) -> Agent:
    return until(
        lambda: a if (a := agent_at(client, pane)) and a.status in wanted else None,
        seconds=120,
        step=1.0,
    )


def _replied(client: HerdrClient, pane: PaneId, word: str) -> str:
    # The prompt itself shows the word once; the reply is the second copy.
    return until(
        lambda: s if (s := client.read(pane, VISIBLE_READ)).count(word) >= 2 else None,
        seconds=120,
        step=1.0,
    )


def _answer_permission(writer: Writer, session: SessionId, pane: PaneId, probe: Path) -> None:
    client = writer.client
    ask = PromptAsk(session, f"Use the Bash tool to run exactly: touch {probe}")
    say("send_prompt (bash request)", repr(send_prompt(writer, ask)))
    _ = _wait_status(client, pane, AgentStatus.BLOCKED)
    question = client.blocked_prompt(pane, session=session)
    show(client, pane, "the permission dialog came up")
    stale = send_key(writer, KeyAsk(session, "1", drawn_from="not-this-screen"))
    say("send_key with a stale fingerprint", repr(stale))
    assert isinstance(stale, Refused)
    assert stale.refusal is Refusal.SCREEN_CHANGED
    tapped = send_key(writer, KeyAsk(session, "1", drawn_from=control_token(question)))
    say("send_key 1 (Yes)", repr(tapped))
    assert isinstance(tapped, KeySent)
    assert tapped.outcome is Outcome.CONFIRMED
    _ = _wait_status(client, pane, AgentStatus.IDLE, AgentStatus.DONE)
    show(client, pane, "the digit answered the dialog")
    assert probe.exists(), "the Yes the digit chose ran the command"


def _claude_round(writer: Writer, workspace: str, cwd: Path) -> None:
    client = writer.client
    # Project settings outrank the user's, so this agent asks before Bash whatever mode is global.
    (cwd / ".claude").mkdir()
    _ = (cwd / ".claude/settings.json").write_text('{"permissions": {"defaultMode": "default"}}')
    started = _start(writer, workspace, "claude", cwd, model="haiku")
    session = _claude_session(writer, started.pane)
    _ = _wait_status(client, started.pane, AgentStatus.IDLE, AgentStatus.DONE)
    prompted = send_prompt(writer, PromptAsk(session, "Reply with exactly the word PONG."))
    say("send_prompt PONG", repr(prompted))
    assert isinstance(prompted, PromptSent)
    _ = _replied(client, started.pane, "PONG")
    _ = _wait_status(client, started.pane, AgentStatus.IDLE, AgentStatus.DONE)
    show(client, started.pane, "send_prompt PONG")
    _answer_permission(writer, session, started.pane, cwd / "hp-writes-probe")
    unconfirmed = end_agent(writer, EndAsk(session))
    say("end_agent without confirm", repr(unconfirmed))
    assert isinstance(unconfirmed, Refused)
    ended = end_agent(writer, EndAsk(session, confirmed=True, acknowledged=True))
    say("end_agent confirmed", repr(ended))
    assert isinstance(ended, AgentEnded)
    assert ended.verified


def _environ_has(client: HerdrClient, pane: PaneId, needle: str) -> bool:
    for pid in client.pane_pids(pane):
        try:
            if needle.encode() in Path(f"/proc/{pid}/environ").read_bytes():
                return True
        except OSError:
            continue
    return False


def _late_identity_round(writer: Writer, workspace: str, cwd: Path) -> None:
    client = writer.client
    opencode = _start(writer, workspace, "opencode", cwd)
    assert _environ_has(client, opencode.pane, OPENCODE_ASK)
    say("opencode environment", f"OPENCODE_CONFIG_CONTENT={OPENCODE_ASK} found in /proc")
    show(client, opencode.pane, "start_agent opencode")
    hermes = _start(writer, workspace, "hermes", cwd)
    first = send_first_prompt(writer, hermes.pane, "Reply with exactly the word PONG.")
    say("send_first_prompt hermes", repr(first))
    assert isinstance(first, PromptSent)
    _ = until(
        lambda: a.session or None if (a := agent_at(client, hermes.pane)) else None,
        seconds=60,
        step=1.0,
    )
    show(client, hermes.pane, "send_first_prompt hermes")


def test_every_write_end_to_end(writer: Writer, tmp_path: Path) -> None:
    client = writer.client
    made = create_workspace(client, "hpw-live")
    say("create_workspace", repr(made))
    assert isinstance(made, WorkspaceDone)
    assert made.verified
    workspace = made.workspace_id
    try:
        renamed = rename_workspace(client, workspace, "hpw-live-renamed")
        say("rename_workspace", repr(renamed))
        assert isinstance(renamed, WorkspaceDone)
        assert renamed.verified
        _claude_round(writer, workspace, tmp_path)
        _late_identity_round(writer, workspace, tmp_path)
    finally:
        cost = [a.target for a in client.snapshot().agents if a.workspace_id == workspace]
        say("closing the throwaway workspace, holding", repr(cost))
        armed = close_workspace(writer, WorkspaceCloseAsk(workspace, confirmed=True))
        saw = armed.screen if isinstance(armed, Refused) else ""
        closed = close_workspace(
            writer, WorkspaceCloseAsk(workspace, confirmed=True, armed=True, saw=saw)
        )
        say("close_workspace", repr(closed))
    assert isinstance(closed, WorkspaceDone)
    assert closed.verified
