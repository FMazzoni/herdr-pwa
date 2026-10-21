"""A stateful fake runner for the write tests: panes, screens and the writes that reach them."""

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import JsonValue, TypeAdapter

from herdr_pwa.audit import AuditLog, Origin, Record, Recorder, records
from herdr_pwa.engine import (
    EngineSettings,
    HerdrClient,
    PaneId,
    SessionId,
    WireReply,
    as_pane_id,
    as_session_id,
)
from herdr_pwa.writes.outcome import Pacing
from herdr_pwa.writes.resolve import Writer
from tests._jsonpath import obj
from tests._wire_support import as_reply

FIXTURES = Path(__file__).parent / "fixtures"
SNAPSHOT: dict[str, JsonValue] = obj(
    TypeAdapter(JsonValue).validate_json((FIXTURES / "snapshot.example.json").read_text())
)
_TEMPLATE_AGENT: dict[str, JsonValue] = obj(SNAPSHOT, "result", "snapshot", "agents", 4)
_TEMPLATE_WORKSPACE: dict[str, JsonValue] = obj(SNAPSHOT, "result", "snapshot", "workspaces", 3)

S1 = as_session_id("6ae1e031-ddb4-4684-ab07-68073f7fec47")
S2 = as_session_id("a36eed14-ff20-48e5-9b50-b8b846cbf977")
P3 = as_pane_id("w1:p3")
P5 = as_pane_id("w1:p5")

_SCROLLBACK = "\n\u25cf Done. The file has been created.\n\n\u273b Sautéed for 4s\n"
_RULE = "─" * 30


def _box(draft: str = "") -> str:
    return f"{_RULE}\n\u276f{' ' + draft if draft else ''}\n{_RULE}\n"


SETTLED = _SCROLLBACK + _box()


def echoed(line: str) -> str:
    """The settled screen once ``line`` has been submitted: in the transcript, above the box."""
    return f"{_SCROLLBACK}{line}\n{_box()}"


def drafted(text: str) -> str:
    """The settled screen with ``text`` typed into the input box and never submitted."""
    return _SCROLLBACK + _box(text)


def capture(name: str, source: str = "detection") -> str:
    """One captured screen from the fixtures."""
    return (FIXTURES / name / f"{source}.txt").read_text()


def ok() -> dict[str, JsonValue]:
    return {"id": "x", "result": {"type": "ok"}}


def _target(params: dict[str, JsonValue]) -> str:
    value = params.get("target") or params.get("pane_id") or ""
    return value if isinstance(value, str) else ""


class FakeClock:
    """A clock that only moves when the code under test sleeps."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


type Hook = Callable[["FakeHerd", dict[str, JsonValue]], None]


# Mutable: a fake the test drives by changing what it answers.
@dataclass(slots=True)
class FakeHerd:
    """Answers the socket methods the writes use, from state a test arranges."""

    agents: dict[str, dict[str, JsonValue]] = field(default_factory=dict[str, dict[str, JsonValue]])
    workspaces: dict[str, dict[str, JsonValue]] = field(
        default_factory=dict[str, dict[str, JsonValue]]
    )
    # Successive reads per pane; the last one repeats.
    screens: dict[str, list[str]] = field(default_factory=dict[str, list[str]])
    fail: dict[str, Exception] = field(default_factory=dict[str, Exception])
    # Run after a write is taken, to model what the TUI did with it.
    after: dict[str, Hook] = field(default_factory=dict[str, Hook])
    # agent.get may answer for a different agent than the snapshot: a recycled pane.
    holders: dict[str, dict[str, JsonValue]] = field(
        default_factory=dict[str, dict[str, JsonValue]]
    )
    calls: list[tuple[str, dict[str, JsonValue]]] = field(
        default_factory=list[tuple[str, dict[str, JsonValue]]]
    )
    # The process leading each pane's terminal; a pane not named here leads with its shell.
    leaders: dict[str, int] = field(default_factory=dict[str, int])
    protocol: int = 22
    started_session: str = ""

    def put(
        self,
        pane: str,
        session: str = "",
        *,
        status: str = "idle",
        workspace: str = "w1",
        **extra: JsonValue,
    ) -> None:
        """Place an agent at ``pane``."""
        raw = dict(_TEMPLATE_AGENT)
        raw.update(pane_id=pane, agent_status=status, workspace_id=workspace, **extra)
        if session:
            raw["agent_session"] = {
                "agent": "claude",
                "kind": "id",
                "source": "herdr:claude",
                "value": session,
            }
        else:
            raw.pop("agent_session", None)
        self.agents[pane] = raw
        self.add_workspace(workspace)

    def add_workspace(self, workspace_id: str, label: str = "") -> None:
        """Make sure ``workspace_id`` exists."""
        if workspace_id not in self.workspaces:
            raw = dict(_TEMPLATE_WORKSPACE)
            raw.update(workspace_id=workspace_id, label=label or workspace_id)
            self.workspaces[workspace_id] = raw

    def call(
        self,
        method: str,
        params: dict[str, JsonValue],
        *,
        timeout: float | None = None,
        idempotent: bool = True,
    ) -> WireReply:
        del timeout, idempotent
        self.calls.append((method, dict(params)))
        if method in self.fail:
            raise self.fail[method]
        answer = self._answer(method, params)
        hook = self.after.get(method)
        if hook is not None:
            hook(self, params)
        return as_reply(answer)

    def close(self) -> None:
        pass

    def writes(self, method: str) -> list[dict[str, JsonValue]]:
        """The params of every call to ``method``."""
        return [params for name, params in self.calls if name == method]

    def _answer(self, method: str, params: dict[str, JsonValue]) -> dict[str, JsonValue]:
        handlers: dict[str, Callable[[dict[str, JsonValue]], dict[str, JsonValue]]] = {
            "session.snapshot": self._session_snapshot,
            "agent.get": self._agent_get,
            "agent.read": self._agent_read,
            "pane.close": self._pane_close,
            "pane.process_info": self._pane_process_info,
            "tab.create": self._tab_create,
            "agent.start": self._agent_start,
            "workspace.create": self._workspace_create,
            "workspace.rename": self._workspace_rename,
            "workspace.close": self._workspace_close,
        }
        handler = handlers.get(method)
        return ok() if handler is None else handler(params)

    def _session_snapshot(self, _params: dict[str, JsonValue]) -> dict[str, JsonValue]:
        snap = dict(obj(SNAPSHOT, "result", "snapshot"))
        snap.update(
            protocol=self.protocol,
            agents=list[JsonValue](self.agents.values()),
            workspaces=list[JsonValue](self.workspaces.values()),
        )
        return {"id": "x", "result": {"type": "session_snapshot", "snapshot": snap}}

    def _agent_get(self, params: dict[str, JsonValue]) -> dict[str, JsonValue]:
        pane = _target(params)
        raw = self.holders.get(pane) or self.agents.get(pane)
        if raw is None:
            return {"id": "x", "error": {"code": "agent_not_found", "message": pane}}
        return {"id": "x", "result": {"type": "agent_info", "agent": raw}}

    def _agent_read(self, params: dict[str, JsonValue]) -> dict[str, JsonValue]:
        pane = _target(params)
        queue = self.screens.get(pane) or [""]
        text = queue.pop(0) if len(queue) > 1 else queue[0]
        read: dict[str, JsonValue] = {
            "format": "text",
            "pane_id": pane,
            "revision": 1,
            "source": str(params.get("source") or "visible"),
            "tab_id": "w1:t1",
            "text": text,
            "truncated": False,
            "workspace_id": "w1",
        }
        return {"id": "x", "result": {"type": "pane_read", "read": read}}

    def _pane_close(self, params: dict[str, JsonValue]) -> dict[str, JsonValue]:
        _ = self.agents.pop(_target(params), None)
        return ok()

    def _pane_process_info(self, params: dict[str, JsonValue]) -> dict[str, JsonValue]:
        shell: dict[str, JsonValue] = {"pid": 4242, "name": "bash", "argv": ["/bin/bash"]}
        leader = self.leaders.get(_target(params))
        running: list[JsonValue] = [shell]
        if leader is not None:
            running = [{"pid": leader, "name": "claude", "argv": ["claude"]}]
        info: dict[str, JsonValue] = {
            "pane_id": _target(params),
            "shell_pid": 4242,
            "foreground_process_group_id": 4242 if leader is None else leader,
            "foreground_processes": running,
        }
        return {"id": "x", "result": {"type": "pane_process_info", "process_info": info}}

    def _tab_create(self, params: dict[str, JsonValue]) -> dict[str, JsonValue]:
        workspace = str(params.get("workspace_id") or "w1")
        pane = f"{workspace}:p9"
        root: dict[str, JsonValue] = {
            "pane_id": pane,
            "terminal_id": "term_9",
            "workspace_id": workspace,
            "tab_id": f"{workspace}:t9",
            "focused": False,
            "agent_status": "unknown",
            "revision": 0,
        }
        tab: dict[str, JsonValue] = {
            "tab_id": f"{workspace}:t9",
            "workspace_id": workspace,
            "number": 9,
            "label": str(params.get("label") or ""),
            "focused": False,
            "pane_count": 1,
            "agent_status": "unknown",
        }
        return {"id": "x", "result": {"type": "tab_created", "tab": tab, "root_pane": root}}

    def _agent_start(self, params: dict[str, JsonValue]) -> dict[str, JsonValue]:
        pane = _target(params)
        self.put(pane, self.started_session, status="idle", workspace=pane.split(":")[0])
        raw = self.agents[pane]
        return {"id": "x", "result": {"type": "agent_started", "agent": raw, "argv": ["x"]}}

    def _workspace_create(self, params: dict[str, JsonValue]) -> dict[str, JsonValue]:
        self.add_workspace("w7", str(params.get("label") or ""))
        made = self.workspaces["w7"]
        root = obj(self._tab_create({"workspace_id": "w7"}), "result")
        return {
            "id": "x",
            "result": {
                "type": "workspace_created",
                "workspace": made,
                "tab": root["tab"],
                "root_pane": root["root_pane"],
            },
        }

    def _workspace_rename(self, params: dict[str, JsonValue]) -> dict[str, JsonValue]:
        found = self.workspaces.get(str(params.get("workspace_id")))
        if isinstance(found, dict):
            found["label"] = str(params.get("label") or "")
        return ok()

    def _workspace_close(self, params: dict[str, JsonValue]) -> dict[str, JsonValue]:
        wid = str(params.get("workspace_id"))
        _ = self.workspaces.pop(wid, None)
        for pane in [p for p, a in self.agents.items() if a.get("workspace_id") == wid]:
            del self.agents[pane]
        return ok()


@dataclass(frozen=True, slots=True)
class Rig:
    """A writer wired to a fake herd, a real audit log and a clock that never sleeps."""

    herd: FakeHerd
    writer: Writer
    clock: FakeClock
    state: Path

    @property
    def audit_records(self) -> list[Record]:
        """Every audit record written so far, oldest first."""
        return records(self.state, limit=0)


def rig(tmp_path: Path, herd: FakeHerd | None = None) -> Rig:
    """A fresh rig over ``herd``."""
    herd = herd if herd is not None else FakeHerd()
    clock = FakeClock()
    settings = EngineSettings(socket_path=Path("/nonexistent/herdr-pwa-writes.sock"))
    client = HerdrClient(settings, transport=herd, clock=clock, sleep=clock.sleep)
    log = AuditLog(tmp_path / "state", max_bytes=1 << 20, payload_chars=512)
    recorder = Recorder(log, Origin(actor="tester@example.com", via="config"))
    return Rig(
        herd,
        Writer(client, recorder, Pacing(sleep=clock.sleep, clock=clock)),
        clock,
        tmp_path / "state",
    )


def session(value: str) -> SessionId:
    """A session id typed as one."""
    return as_session_id(value)


def pane(value: str) -> PaneId:
    """A pane id typed as one."""
    return as_pane_id(value)
