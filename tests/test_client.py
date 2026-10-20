"""Client tests: the vocabulary, the shapes, and the two guards."""

import ast
import inspect
import json
import socket
import subprocess
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from pydantic import BaseModel, JsonValue, TypeAdapter

from herdr_pwa import protocol
from herdr_pwa.engine import (
    INTERRUPT_KEY,
    NOT_READY_CODE,
    PANE_BUSY_CODE,
    SESSION_MOVED_CODE,
    AgentStatus,
    BlockedPrompt,
    EngineError,
    EngineSettings,
    EngineTimeout,
    HerdrClient,
    InvalidRequest,
    InvalidResponse,
    Launch,
    NewTab,
    PaneId,
    ProtocolMismatch,
    ReadOptions,
    Seat,
    SessionId,
    TargetNotFound,
    as_pane_id,
    as_session_id,
    engine_settings,
)
from herdr_pwa.settings import Settings
from tests._jsonpath import at, obj
from tests._wire_support import FakeTransport, Reply, TickingClock, no_sleep
from tests.engine_seam import (
    SocketTransport,
    client_module,
    discovery_subscriptions,
    method_of,
    payload_field,
    raise_for_error_frame,
    wire_request,
)
from tests.engine_seam import wire_response as wire

FIXTURE = Path(__file__).parent / "fixtures" / "snapshot.example.json"
SNAPSHOT_JSON = FIXTURE.read_text()

# Converted once and deliberately: a bare string or a session id at a verb is a type error.
P3 = as_pane_id("w1:p3")
P5 = as_pane_id("w1:p5")
SESS = as_session_id("5b609dba-d756-493a-a3fc-b4e09bf0a183")

# Read off the generated request types, never spelled, so an upstream rename moves both.
SNAPSHOT = method_of(wire_request.SessionSnapshotRequest)
GET = method_of(wire_request.AgentGetRequest)
READ = method_of(wire_request.AgentReadRequest)
PROMPT = method_of(wire_request.AgentPromptRequest)
SEND_KEYS = method_of(wire_request.AgentSendKeysRequest)
FOCUS = method_of(wire_request.AgentFocusRequest)
START = method_of(wire_request.AgentStartRequest)
TAB_CREATE = method_of(wire_request.TabCreateRequest)
PANE_CLOSE = method_of(wire_request.PaneCloseRequest)
WS_CREATE = method_of(wire_request.WorkspaceCreateRequest)
WS_RENAME = method_of(wire_request.WorkspaceRenameRequest)
WS_CLOSE = method_of(wire_request.WorkspaceCloseRequest)


# A socket nobody listens on, so a slip is a connection error rather than traffic.
SETTINGS = EngineSettings(socket_path=Path("/nonexistent/herdr-pwa-test.sock"))


# --------------------------------------------------------------------------
# Canned frames
# --------------------------------------------------------------------------


# Derived, so the next protocol bump cannot quietly turn the drift tests into their opposite.
DRIFTED = protocol.EXPECTED_PROTOCOL + 1


def snapshot_frame(protocol_number: int | None = None) -> dict[str, JsonValue]:
    payload = obj(TypeAdapter(JsonValue).validate_json(SNAPSHOT_JSON))
    if protocol_number is not None:
        obj(payload, "result", "snapshot")["protocol"] = protocol_number
    return payload


def raw_agent(**overrides: JsonValue) -> dict[str, JsonValue]:
    raw = dict(
        obj(TypeAdapter(JsonValue).validate_json(SNAPSHOT_JSON), "result", "snapshot", "agents", 0)
    )
    raw.update(overrides)
    return raw


def agent_frame(**overrides: JsonValue) -> dict[str, JsonValue]:
    return {
        "id": "x",
        "result": {"type": "agent_info", "agent": raw_agent(**overrides)},
    }


def read_frame(text: str) -> dict[str, JsonValue]:
    return {
        "id": "x",
        "result": {
            "type": "pane_read",
            "read": {
                "format": "text",
                "pane_id": "w1:p3",
                "revision": 7,
                "source": "visible",
                "tab_id": "w1:t1",
                "text": text,
                "truncated": False,
                "workspace_id": "w1",
            },
        },
    }


def build(
    replies: dict[str, Reply] | None = None,
    *,
    settings: EngineSettings | None = None,
    protocol_number: int | None = None,
    sleep: Callable[[float], None] = no_sleep,
) -> tuple[HerdrClient, FakeTransport]:
    """A client wired to a fake wire that answers a healthy snapshot by default."""
    frames: dict[str, Reply] = {SNAPSHOT: snapshot_frame(protocol_number)}
    frames.update(replies or {})
    wire = FakeTransport(replies=frames)
    engine = HerdrClient(settings or SETTINGS, transport=wire, clock=TickingClock(), sleep=sleep)
    return engine, wire


# --------------------------------------------------------------------------
# The request layer
# --------------------------------------------------------------------------


def test_a_read_is_issued_as_its_dotted_method_with_named_params() -> None:
    """The socket vocabulary, not the CLI's. ``api snapshot`` is gone."""
    eng, wire = build()
    eng.snapshot()
    assert wire.last.method == SNAPSHOT == "session.snapshot"
    assert wire.last.params == {}
    assert wire.last.timeout == eng.settings.command_timeout


def test_an_explicit_timeout_reaches_the_wire() -> None:
    eng, wire = build()
    eng.snapshot(timeout=0.25)
    assert wire.last.timeout == 0.25


def test_a_write_is_marked_as_never_safe_to_resend() -> None:
    """The retry rule and the write guard are driven off the same set."""
    eng, wire = build({GET: agent_frame()})
    eng.prompt(Seat(P3), "hi")
    issued = {c.method: c.idempotent for c in wire.calls}
    assert issued[PROMPT] is False
    assert issued[SNAPSHOT] is True
    assert issued[GET] is True


def test_an_error_from_the_wire_is_not_swallowed() -> None:
    eng, _ = build({GET: TargetNotFound("gone", code="agent_not_found")})
    with pytest.raises(TargetNotFound):
        eng.agent(as_pane_id("nosuch"))


def test_closing_the_client_releases_the_connection() -> None:
    eng, wire = build()
    eng.close()
    assert wire.closed == 1


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------


def test_snapshot_unwraps_result_snapshot() -> None:
    eng, _ = build()
    snap = eng.snapshot()
    assert snap.protocol == protocol.EXPECTED_PROTOCOL
    assert len(snap.agents) == 6


def test_snapshot_without_the_inner_object_is_invalid() -> None:
    eng, _ = build({SNAPSHOT: {"id": "x", "result": {"type": "session_snapshot"}}})
    with pytest.raises(InvalidResponse):
        eng.snapshot()


def test_a_reply_from_the_wrong_arm_of_the_union_is_refused() -> None:
    """The failure the generated models exist to catch."""
    eng, _ = build({GET: {"id": "x", "result": {"type": "ok"}}})
    with pytest.raises(InvalidResponse) as exc:
        eng.agent(P3)
    assert "AgentInfoResponseResult" in exc.value.message


def test_a_read_of_a_frame_the_schema_rejects_warns_and_still_serves(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Same doctrine as the protocol guard, one level down."""
    frame = snapshot_frame()
    obj(frame, "result", "snapshot", "agents", 0)["agent_status"] = "vibing"
    eng, _ = build({SNAPSHOT: frame})
    with caplog.at_level("WARNING"):
        snap = eng.snapshot()
    assert "schema" in caplog.text
    assert AgentStatus.UNKNOWN in {a.status for a in snap.agents}


def test_agents_returns_ranked_agents() -> None:
    eng, _ = build()
    found = eng.agents()
    assert [a.rank for a in found] == sorted(a.rank for a in found)
    # The fixture has no blocked or done agent, so working leads and idle trails.
    assert found[0].status is AgentStatus.WORKING
    assert found[-1].status is AgentStatus.IDLE


def test_agent_get_unwraps_result_agent() -> None:
    eng, wire = build({GET: agent_frame()})
    a = eng.agent(as_pane_id("w652ae557d5b9c1:p2"))
    assert wire.last.method == GET
    assert wire.last.params == {"target": "w652ae557d5b9c1:p2"}
    assert a.session == "5b609dba-d756-493a-a3fc-b4e09bf0a183"
    assert a.workspace == ""  # a single agent carries no workspace list to join


def test_read_builds_the_documented_params() -> None:
    eng, wire = build({READ: read_frame("viewport text")})
    assert eng.read(P3, ReadOptions(lines=40, source="detection")) == "viewport text"
    assert wire.last.method == READ
    assert wire.last.params == {
        "target": "w1:p3",
        "lines": 40,
        "source": "detection",
        "format": "text",
        # The model's own default, dumped with the rest: the mirror renders text
        # nodes, so escape sequences are stripped at the source rather than
        # sanitized here.
        "strip_ansi": True,
    }


def test_read_defaults_to_the_visible_source() -> None:
    """Pinned because a recent read past the viewport scrolls the agent's own pane, not only for cost."""
    eng, wire = build({READ: read_frame("viewport text")})
    eng.read(P3)
    assert wire.last.params["source"] == "visible"


def test_an_unnamed_source_comes_from_settings_rather_than_a_literal() -> None:
    """The read_source field must be read: a call naming no source takes it."""
    eng, wire = build(
        {READ: read_frame("viewport text")},
        settings=replace(SETTINGS, read_source="detection"),
    )
    eng.read(P3)
    assert wire.last.params["source"] == "detection"


def test_a_named_source_still_beats_the_configured_default() -> None:
    """Wiring the default may not quietly override a caller who was explicit."""
    eng, wire = build(
        {READ: read_frame("viewport text")},
        settings=replace(SETTINGS, read_source="detection"),
    )
    eng.read(P3, ReadOptions(source="visible"))
    assert wire.last.params["source"] == "visible"
    eng.blocked_prompt(P3)
    assert wire.last.params["source"] == "detection"


def test_a_configured_recent_default_is_capped_like_an_explicit_one() -> None:
    """The cap is on the resolved source, not on the caller's argument."""
    eng, wire = build(
        {READ: read_frame("viewport text")},
        settings=replace(SETTINGS, read_source="recent", read_lines=200),
    )
    eng.read(P3)
    assert wire.last.params["source"] == "recent"
    assert wire.last.params["lines"] == eng.settings.recent_max_lines


def test_a_recent_read_is_capped_so_it_cannot_outrun_the_timeout() -> None:
    eng, wire = build({READ: read_frame("viewport text")})
    eng.read(P3, ReadOptions(lines=200, source="recent"))
    lines = wire.last.params["lines"]
    assert isinstance(lines, int)
    assert lines == eng.settings.recent_max_lines
    assert lines * 0.035 < eng.settings.command_timeout


def test_a_visible_read_takes_its_line_count_as_given() -> None:
    eng, wire = build({READ: read_frame("viewport text")})
    eng.read(P3, ReadOptions(lines=200))
    assert wire.last.params["lines"] == 200


def test_a_read_answering_no_text_is_invalid() -> None:
    eng, _ = build({READ: {"id": "x", "result": {"type": "pane_read", "read": {}}}})
    with pytest.raises(InvalidResponse):
        eng.read(P3)


def decoded_frame(text: bytes) -> dict[str, JsonValue]:
    """A reply frame carrying ``text``, round-tripped through the real wire."""
    line = json.dumps(read_frame("\x00PLACEHOLDER\x00")).encode()
    framed = line.replace(b"\\u0000PLACEHOLDER\\u0000", text)
    reply = SocketTransport._decode(framed)  # pyright: ignore[reportPrivateUsage]
    raise_for_error_frame(reply)  # pane content is data, never an envelope
    return reply.payload


def test_pane_text_that_merely_looks_like_json_still_comes_back() -> None:
    """Pane content is data, never an envelope. A pane showing a JSON error document must not be mistaken for the runner reporting one."""
    looks_like = '{"error": {"code": "nope", "message": "this is pane content"}}'
    eng, _ = build({READ: decoded_frame(json.dumps(looks_like)[1:-1].encode())})
    assert eng.read(P3) == looks_like


def test_undecodable_pane_content_is_returned_not_raised() -> None:
    """The replacement glyph a mid-multibyte viewport cut produces."""
    eng, _ = build({READ: decoded_frame(b"caf\xc3 binary bytes")})
    text = eng.read(P3)
    assert "\ufffd" in text
    assert text.endswith("binary bytes")


def test_blocked_prompt_reads_the_detection_source() -> None:
    # The footer and the cursor are input on purpose: without both the run is not believed a menu.
    menu = "Run the tests?\n\u276f 1. Yes\n  2. No\n\nEnter to select · Esc to cancel\n"
    eng, wire = build({READ: read_frame(menu)})
    p = eng.blocked_prompt(P5, session=SESS)
    assert wire.last.params["source"] == "detection"
    assert p.target == "w1:p5"
    assert p.session == SESS
    assert [o.label for o in p.options] == ["Yes", "No"]


def test_a_colour_read_is_parsed_whole_never_beside_an_older_text_read() -> None:
    """Text and colour come from ONE read: the colour read is stripped for the grammar."""
    capture = Path(__file__).parent / "fixtures" / "capture-blocked-opencode-question"
    ansi = (capture / "visible.ansi").read_text(encoding="utf-8")
    # The text read is a moment older: the transcript above the dialog has since moved.
    older = (capture / "detection.txt").read_text(encoding="utf-8").replace("Done.", "Working.")

    def answer(params: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return read_frame(ansi if params["format"] == "ansi" else older)

    eng, wire = build({READ: answer})
    p = eng.blocked_prompt(P5, session=SESS)
    assert [c.params["format"] for c in wire.calls if c.method == READ] == ["text", "ansi"]
    assert "Working." not in p.raw
    assert p.focus == 0
    assert [o.number for o in p.options] == [1, 2, 3, 4]


# --------------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------------


def test_prompt_sends_the_text_as_a_named_parameter() -> None:
    """The CLI's positional-argv trap is gone with the CLI."""
    eng, wire = build({GET: agent_frame()})
    eng.prompt(Seat(P3), "--not-a-flag")
    assert wire.last.method == PROMPT
    assert wire.last.params == {"target": "w1:p3", "text": "--not-a-flag"}


def test_send_keys_forwards_every_key_as_a_list() -> None:
    eng, wire = build({GET: agent_frame()})
    eng.send_keys(Seat(P3), "1", "enter")
    assert wire.last.method == SEND_KEYS
    assert wire.last.params == {"target": "w1:p3", "keys": ["1", "enter"]}


def test_send_keys_requires_a_key() -> None:
    """And says so inside the taxonomy: a bare ``ValueError`` out of a public verb is a 500 with a traceback the moment a route forwards user input."""
    eng, wire = build()
    with pytest.raises(InvalidRequest) as caught:
        eng.send_keys(Seat(P3))
    assert isinstance(caught.value, EngineError | ValueError)  # pyright: ignore[reportUnnecessaryIsInstance]
    assert caught.value.code == "invalid_request"
    assert wire.calls == []


def test_an_unknown_read_source_is_an_engine_error_not_a_bare_valueerror() -> None:
    """``?source=`` off a phone is a typo, not a crash. It never leaves the process either way -- but it has to arrive as something a route can map."""
    eng, wire = build()
    with pytest.raises(InvalidRequest) as caught:
        eng.read(P3, ReadOptions(source="visable"))
    assert isinstance(caught.value, EngineError)  # pyright: ignore[reportUnnecessaryIsInstance]
    assert caught.value.code == "invalid_request"
    assert wire.calls == []


def test_focus_forwards_the_target() -> None:
    eng, wire = build()
    eng.focus(P3)
    assert wire.last.method == FOCUS
    assert wire.last.params == {"target": "w1:p3"}


# --------------------------------------------------------------------------
# Writes require an agent that is actually listening
# --------------------------------------------------------------------------


def test_keys_are_refused_to_an_agent_that_is_still_launching() -> None:
    """The safety the socket unlocks and the CLI could not."""
    eng, wire = build({GET: agent_frame(launch_pending=True)})
    with pytest.raises(EngineError) as exc:
        eng.send_keys(Seat(P3), "1")
    assert exc.value.code == NOT_READY_CODE
    assert SEND_KEYS not in wire.methods  # nothing was typed


def test_a_prompt_is_refused_when_the_agent_is_not_interactive() -> None:
    eng, wire = build({GET: agent_frame(interactive_ready=False)})
    with pytest.raises(EngineError) as exc:
        eng.prompt(Seat(P3), "hi")
    assert exc.value.code == NOT_READY_CODE
    assert PROMPT not in wire.methods


def test_a_runner_that_does_not_report_readiness_is_not_blocked() -> None:
    """Absent is not false, and this matters today: herdr 0.8.0 omits both fields entirely, so refusing on unknown would refuse every write."""
    raw = raw_agent()
    assert "launch_pending" not in raw
    assert "interactive_ready" not in raw
    eng, wire = build({GET: {"id": "x", "result": {"type": "agent_info", "agent": raw}}})
    eng.prompt(Seat(P3), "hi")
    assert PROMPT in wire.methods


def test_a_ready_agent_is_written_to() -> None:
    eng, wire = build({GET: agent_frame(interactive_ready=True, launch_pending=False)})
    eng.send_keys(Seat(P3), "enter")
    assert wire.methods == [SNAPSHOT, GET, SEND_KEYS]


def test_the_readiness_check_is_fail_closed_on_a_frame_it_cannot_read() -> None:
    """A write may not proceed on a frame this build cannot validate."""
    eng, _ = build({GET: agent_frame(agent_status="vibing")})
    with pytest.raises(InvalidResponse):
        eng.prompt(Seat(P3), "hi")


# --------------------------------------------------------------------------
# The interrupt is the exception, because it is the way OUT of busy
# --------------------------------------------------------------------------


def test_the_interrupt_is_sent_to_an_agent_that_is_not_accepting_input() -> None:
    """Ctrl-c is the way out of busy, so a busy agent cannot refuse it."""
    eng, wire = build({GET: agent_frame(interactive_ready=False)})
    eng.send_keys(Seat(P3), INTERRUPT_KEY)
    assert SEND_KEYS in wire.methods


def test_every_other_key_is_still_refused_to_an_agent_that_is_not_accepting() -> None:
    """The exemption is one key wide. An answer to a prompt is still swallowed."""
    eng, wire = build({GET: agent_frame(interactive_ready=False)})
    with pytest.raises(EngineError) as exc:
        eng.send_keys(Seat(P3), "enter")
    assert exc.value.code == NOT_READY_CODE
    assert SEND_KEYS not in wire.methods


def test_the_interrupt_bundled_with_another_key_is_not_exempt() -> None:
    """``ctrl-c`` then ``enter`` is not an interrupt; the second key is input."""
    eng, wire = build({GET: agent_frame(interactive_ready=False)})
    with pytest.raises(EngineError) as exc:
        eng.send_keys(Seat(P3), INTERRUPT_KEY, "enter")
    assert exc.value.code == NOT_READY_CODE
    assert SEND_KEYS not in wire.methods


def test_the_interrupt_is_still_refused_while_the_agent_is_launching() -> None:
    """The other half of the guard stands: launching is not busy."""
    eng, wire = build({GET: agent_frame(launch_pending=True, interactive_ready=False)})
    with pytest.raises(EngineError) as exc:
        eng.send_keys(Seat(P3), INTERRUPT_KEY)
    assert exc.value.code == NOT_READY_CODE
    assert SEND_KEYS not in wire.methods


def test_a_key_reaches_an_agent_that_is_launching_and_blocked() -> None:
    """`blocked` beside `launch_pending` is the state a key is FOR."""
    eng, wire = build({GET: agent_frame(launch_pending=True, agent_status="blocked")})
    eng.send_keys(Seat(P3), "enter")
    assert SEND_KEYS in wire.methods


def test_a_launching_agent_that_is_not_blocked_still_refuses_the_key() -> None:
    """The exception is `blocked`, not `launch_pending`. Nothing else moved."""
    for status in ("idle", "working", "unknown"):
        eng, wire = build({GET: agent_frame(launch_pending=True, agent_status=status)})
        with pytest.raises(EngineError) as exc:
            eng.send_keys(Seat(P3), "enter")
        assert exc.value.code == NOT_READY_CODE
        assert SEND_KEYS not in wire.methods


def test_a_launching_blocked_agent_at_a_recycled_pane_is_still_refused() -> None:
    """The session-holder check is untouched: a key must not reach the wrong agent."""
    eng, wire = build({GET: agent_frame(launch_pending=True, agent_status="blocked")})
    with pytest.raises(TargetNotFound):
        eng.send_keys(Seat(P3, as_session_id("not-the-holder")), "enter")
    assert SEND_KEYS not in wire.methods


def test_the_interrupt_is_still_refused_at_a_pane_somebody_else_holds() -> None:
    """Pane ids are recycled: an exempt key must not go to the wrong agent."""
    eng, wire = build({GET: agent_frame(interactive_ready=False)})
    with pytest.raises(TargetNotFound):
        eng.send_keys(Seat(P3, as_session_id("not-the-holder")), INTERRUPT_KEY)
    assert SEND_KEYS not in wire.methods


def test_focus_does_not_require_readiness() -> None:
    """Focusing a pane types nothing, and watching an agent start is legitimate."""
    eng, wire = build()
    eng.focus(P3)
    assert GET not in wire.methods


# --------------------------------------------------------------------------
# The protocol guard: warn on reads, fail closed on writes
# --------------------------------------------------------------------------


def test_protocol_drift_still_serves_reads(caplog: pytest.LogCaptureFixture) -> None:
    eng, _ = build(protocol_number=DRIFTED)
    with caplog.at_level("WARNING"):
        snap = eng.snapshot()
    assert snap.protocol == DRIFTED
    assert "protocol" in caplog.text.lower()


def test_the_protocol_warning_is_not_repeated_every_poll(
    caplog: pytest.LogCaptureFixture,
) -> None:
    eng, _ = build(protocol_number=DRIFTED)
    with caplog.at_level("WARNING"):
        for _ in range(5):
            eng.snapshot()
    assert caplog.text.lower().count(f"protocol {DRIFTED}") == 1


@pytest.mark.parametrize("verb", ["prompt", "send_keys", "focus"])
def test_protocol_drift_fails_every_write_closed(verb: str) -> None:
    eng, wire = build({GET: agent_frame()}, protocol_number=DRIFTED)
    drive: dict[str, Callable[[], object]] = {
        "prompt": lambda: eng.prompt(Seat(P3), "x"),
        "send_keys": lambda: eng.send_keys(Seat(P3), "x"),
        "focus": lambda: eng.focus(P3),
    }
    with pytest.raises(ProtocolMismatch):
        drive[verb]()
    assert wire.methods == [SNAPSHOT]  # nothing was driven


def test_a_write_checks_the_protocol_before_driving() -> None:
    eng, wire = build({GET: agent_frame()})
    eng.focus(P3)
    assert wire.methods == [SNAPSHOT, FOCUS]


def test_a_write_verb_runs_the_protocol_guard_once_not_twice() -> None:
    """``prompt`` guarded in the verb *and* again inside ``_call``."""
    eng, wire = build(
        {GET: agent_frame()},
        settings=replace(SETTINGS, protocol_max_age=1e-6),
    )
    eng.prompt(Seat(P3), "hi")
    assert wire.methods == [SNAPSHOT, GET, PROMPT]


def test_the_write_guard_cannot_recurse_through_its_own_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``WRITE_METHODS`` is three hand-written strings, and sets grow."""
    monkeypatch.setattr(client_module, "WRITE_METHODS", frozenset({SNAPSHOT, PROMPT}))
    eng, _ = build()
    with pytest.raises(EngineError) as caught:
        eng.snapshot()
    assert not isinstance(caught.value, RecursionError)
    assert "re-entered" in str(caught.value)


def test_the_protocol_guard_does_not_re_snapshot_once_known() -> None:
    eng, wire = build({GET: agent_frame()})
    eng.snapshot()
    eng.focus(P3)
    eng.focus(P3)
    assert wire.methods == [SNAPSHOT, FOCUS, FOCUS]


def test_a_stale_protocol_ok_no_longer_authorises_a_write() -> None:
    """The cached protocol OK must expire, or a runner restart onto a new protocol keeps being written to."""
    eng, wire = build({GET: agent_frame()})
    eng.snapshot()
    eng.last_protocol_at -= eng.settings.protocol_max_age + 1  # the OK went stale
    wire.replies[SNAPSHOT] = snapshot_frame(DRIFTED)  # runner restarted on a new one
    with pytest.raises(ProtocolMismatch):
        eng.prompt(Seat(P3), "hi")
    assert wire.methods == [SNAPSHOT, SNAPSHOT]  # re-checked, not driven


def test_a_stale_but_still_correct_protocol_is_simply_refreshed() -> None:
    eng, wire = build({GET: agent_frame()})
    eng.snapshot()
    eng.last_protocol_at -= eng.settings.protocol_max_age + 1
    eng.prompt(Seat(P3), "hi")
    assert wire.methods == [SNAPSHOT, SNAPSHOT, GET, PROMPT]


def test_drift_can_be_configured_not_to_fail_writes() -> None:
    """Off by default and stays off by default; this proves the switch is wired rather than decorative -- and that opting out skips the extra snapshot."""
    eng, wire = build(
        settings=replace(SETTINGS, protocol_drift_fails_writes=False, expected_protocol=44),
        protocol_number=DRIFTED,
    )
    eng.focus(P3)
    assert wire.methods == [FOCUS]


# --------------------------------------------------------------------------
# The guard is not bypassable
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("request_type", "params"),
    [
        (
            wire_request.AgentPromptRequest,
            wire_request.AgentPromptParams(target="w1:p3", text="hi"),
        ),
        (
            wire_request.AgentSendKeysRequest,
            wire_request.AgentSendKeysParams(target="w1:p3", keys=["1"]),
        ),
        (wire_request.AgentFocusRequest, wire_request.AgentTarget(target="w1:p3")),
    ],
)
def test_a_write_through_the_private_caller_still_fails_closed(
    request_type: type[BaseModel], params: BaseModel
) -> None:
    """The guard is a property of issuing a request, not of remembering to ask."""
    eng, wire = build(protocol_number=DRIFTED)
    with pytest.raises(ProtocolMismatch):
        _ = eng._call(request_type, params)  # pyright: ignore[reportPrivateUsage]
    assert wire.methods == [SNAPSHOT]  # nothing was driven


def test_a_read_through_the_private_caller_is_never_blocked() -> None:
    """Reads degrade, only writes fail closed."""
    eng, _ = build(protocol_number=DRIFTED)
    assert eng.snapshot().protocol == DRIFTED


# Unlisted means write, so a forgotten declaration fails in the safe direction.
READ_VERBS = frozenset(
    {
        "snapshot",
        "agents",
        "agent",
        "read",
        "blocked_prompt",
        "shell_pane",
        "started_agent",
        "explain",
        "agent_frame",
        "pane_pids",
    }
)


def _requests_issued_by(verb: ast.FunctionDef) -> set[str]:
    """The dotted method names a verb's own body constructs a request type for."""
    types = {
        node.attr
        for node in ast.walk(verb)
        if isinstance(node, ast.Attribute) and node.attr.endswith("Request")
    }
    # The AST names the attribute; the generated module is what it names.
    return {method_of(cast(type[BaseModel], getattr(wire_request, name))) for name in types}


def test_every_write_verb_is_registered_as_a_write() -> None:
    """Derived, not enumerated -- because the enumeration could not fail."""
    body = ast.parse(inspect.getsource(client_module))
    cls = next(
        node
        for node in ast.walk(body)
        if isinstance(node, ast.ClassDef) and node.name == "HerdrClient"
    )
    verbs = [
        node
        for node in cls.body
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")
    ]
    assert {v.name for v in verbs} >= READ_VERBS, "a declared read verb is gone"
    derived: set[str] = set()
    for verb in verbs:
        if verb.name not in READ_VERBS:
            derived |= _requests_issued_by(verb)
    assert derived == protocol.WRITE_METHODS, (
        "a verb that drives an agent is not registered as a write (or a new read "
        f"verb needs declaring in READ_VERBS): derived {sorted(derived)}"
    )

    # ...and each registered string is still the one the schema pins.
    assert {
        PROMPT,
        SEND_KEYS,
        FOCUS,
        START,
        TAB_CREATE,
        PANE_CLOSE,
        WS_CREATE,
        WS_RENAME,
        WS_CLOSE,
    } == protocol.WRITE_METHODS
    for read_method in (SNAPSHOT, GET, READ):
        assert read_method not in protocol.WRITE_METHODS


def test_the_socket_vocabulary_is_not_the_cli_vocabulary() -> None:
    """The two spellings that would silently 'work' if assumed derivable."""
    assert SEND_KEYS == "agent.send_keys"
    assert SNAPSHOT == "session.snapshot"
    assert protocol.CMD_SNAPSHOT == ("api", "snapshot")
    assert protocol.CMD_AGENT_SEND_KEYS == ("agent", "send-keys")


def test_a_method_name_is_read_off_the_generated_literal() -> None:
    """The mechanism the whole no-second-copy claim rests on."""
    assert method_of(wire_request.AgentPromptRequest) == "agent.prompt"
    assert payload_field(wire.SessionSnapshotResponseResult) == "snapshot"
    assert payload_field(wire.AgentInfoResponseResult) == "agent"


def test_a_request_type_that_pins_no_single_method_is_refused() -> None:
    """The derivation fails loudly if codegen ever stops pinning one literal."""

    class Bogus(BaseModel):
        id: str
        method: str
        params: dict[str, JsonValue]

    with pytest.raises(InvalidResponse):
        method_of(Bogus)


def test_the_request_that_goes_on_the_wire_is_what_the_schema_dumps() -> None:
    """Params are serialized by the generated model, not assembled by hand."""
    eng, wire_ = build({READ: read_frame("x")})
    eng.read(P3, ReadOptions(lines=5, source="detection"))
    assert wire_.last.params == {
        "target": "w1:p3",
        "lines": 5,
        "source": "detection",
        "format": "text",
        "strip_ansi": True,
    }


# --------------------------------------------------------------------------
# The state is per instance, not per process
# --------------------------------------------------------------------------


def test_two_clients_do_not_share_a_protocol_cache() -> None:
    """Protocol memory is per instance: one client's good protocol never licenses another."""
    good, good_wire = build()
    drifted, drifted_wire = build(protocol_number=DRIFTED)

    good.snapshot()
    drifted.snapshot()
    assert (good.last_protocol, drifted.last_protocol) == (
        protocol.EXPECTED_PROTOCOL,
        DRIFTED,
    )

    good.focus(P3)
    with pytest.raises(ProtocolMismatch):
        drifted.focus(P3)

    assert good_wire.methods == [SNAPSHOT, FOCUS]
    assert drifted_wire.methods == [SNAPSHOT]  # nothing was driven
    assert good.transport is not drifted.transport


def test_two_clients_can_be_configured_differently() -> None:
    """One runner, two expectations: config is per instance too, so a second view built for a DRIFTED protocol is writable while the default is not."""
    old, _ = build(
        settings=replace(SETTINGS, expected_protocol=protocol.EXPECTED_PROTOCOL),
        protocol_number=DRIFTED,
    )
    new, _ = build(settings=replace(SETTINGS, expected_protocol=DRIFTED), protocol_number=DRIFTED)
    with pytest.raises(ProtocolMismatch):
        old.focus(P3)
    new.focus(P3)
    assert old.settings.command_timeout == new.settings.command_timeout


def test_the_warn_once_flag_resets_with_a_fresh_instance(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Warn-once must mean once *per client*, not once per process -- otherwise the drift warning is emitted a single time for the life of the interpreter and no later view ever says it."""
    first, _ = build(protocol_number=DRIFTED)
    with caplog.at_level("WARNING"):
        first.snapshot()
        first.snapshot()
    assert caplog.text.lower().count(f"protocol {DRIFTED}") == 1
    assert first.warned_protocol == DRIFTED

    caplog.clear()
    second, _ = build(protocol_number=DRIFTED)
    with caplog.at_level("WARNING"):
        second.snapshot()
    assert caplog.text.lower().count(f"protocol {DRIFTED}") == 1  # said again, by a new view

    caplog.clear()
    first.forget_protocol()
    assert (first.last_protocol, first.warned_protocol) == (None, None)
    with caplog.at_level("WARNING"):
        first.snapshot()
    assert caplog.text.lower().count(f"protocol {DRIFTED}") == 1


def test_a_client_built_with_nothing_opens_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Construction must not dial: a service that starts before the runner must not die at import."""

    def no_socket(*_args: object) -> socket.socket:
        raise AssertionError("building a client opened a socket")

    monkeypatch.setattr(socket, "socket", no_socket)
    fresh = HerdrClient(replace(SETTINGS, socket_path=Path("/nonexistent/x.sock")))
    assert isinstance(fresh.transport, SocketTransport)


def test_there_is_no_module_level_protocol_state_left() -> None:
    """The literal defect: three globals mutated through a ``global`` statement."""
    tree = ast.parse(Path(client_module.__file__).read_text(encoding="utf-8"))
    declared = [
        name for node in ast.walk(tree) if isinstance(node, ast.Global) for name in node.names
    ]
    assert not declared, f"module-level state is back: {declared}"
    assigned_via_globals = [
        ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "globals"
    ]
    assert not assigned_via_globals, (
        f"globals() reaches module state without the keyword: {assigned_via_globals}"
    )
    for name in ("_last_protocol", "_last_protocol_at", "_warned_protocol"):
        assert not hasattr(client_module, name)


# --------------------------------------------------------------------------
# The type discipline reaches the keystroke, not just the lookup
# --------------------------------------------------------------------------

# Every line of this type-checked clean while the write verbs took a plain str.
PROBE = """
from herdr_pwa.engine import HerdrClient, Seat
from herdr_pwa.engine import Snapshot

def probe(c: HerdrClient) -> None:
    s = Snapshot()
    a = s.agents[0]
    c.send_keys(Seat(a.session), "1")
    c.prompt(Seat(a.session), "rm -rf /")
    s.by_session(a.target)
"""

# One reportArgumentType per keystroke line, and none on a line the probe did not write.
PROBE_ERROR_LINES = {8, 9, 10}


def _pyright(path: Path) -> JsonValue:
    """Run the repo's own pyright over one file and return its JSON report."""
    done = subprocess.run(  # noqa: S603 - our own interpreter's checker on a file we wrote
        [sys.executable, "-m", "basedpyright", "--outputjson", str(path)],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parent.parent,
        check=False,
    )
    if not done.stdout.strip():
        pytest.skip(f"pyright did not run: {done.stderr[:400]}")
    return TypeAdapter(JsonValue).validate_json(done.stdout)


def test_a_session_id_cannot_reach_a_write_verb(tmp_path: Path) -> None:
    """The probe, type-checked for real rather than reasoned about."""
    probe = tmp_path / "probe.py"
    probe.write_text(PROBE)
    report = _pyright(probe)
    diagnostics = [obj(d) for d in cast(list[JsonValue], at(report, "generalDiagnostics"))]
    errors = [d for d in diagnostics if d["severity"] == "error"]
    assert {cast(int, at(d, "range", "start", "line")) + 1 for d in errors} == PROBE_ERROR_LINES, (
        f"expected an error on each probe line, got {errors}"
    )
    assert all(d["rule"] == "reportArgumentType" for d in errors)


def test_a_session_id_handed_to_a_write_verb_raises_at_runtime() -> None:
    """The static error is the fence; this is the runtime shape guard behind it."""
    eng, wire = build({GET: agent_frame()})
    # PaneId() checks nothing: it is how an untyped value reaches a verb unvalidated.
    uuid = PaneId("5b609dba-d756-493a-a3fc-b4e09bf0a183")
    calls: tuple[Callable[[], object], ...] = (
        lambda: eng.send_keys(Seat(uuid), "1"),
        lambda: eng.prompt(Seat(uuid), "hi"),
        lambda: eng.focus(uuid),
        lambda: eng.agent(uuid),
        lambda: eng.read(uuid),
        lambda: eng.blocked_prompt(uuid),
    )
    for call in calls:
        with pytest.raises(ValueError, match="session id"):
            call()
    assert wire.calls == []  # nothing reached the runner


def test_a_pane_id_cannot_be_smuggled_into_a_blocked_prompt() -> None:
    """``BlockedPrompt`` is what a tap-to-answer UI builds its keystroke from."""
    with pytest.raises(ValueError, match="session id"):
        BlockedPrompt.parse("q", target=PaneId("5b609dba-d756-493a-a3fc-b4e09bf0a183"))
    with pytest.raises(ValueError, match="pane id"):
        BlockedPrompt.parse("q", session=SessionId("w1:p3"))
    p = BlockedPrompt.parse("q", target=as_pane_id("  w1:p3 "), session=SESS)
    assert (p.target, p.session) == ("w1:p3", SESS)


def test_the_address_is_normalised_before_it_goes_on_the_wire() -> None:
    """A URL segment arrives with whitespace more often than anyone expects."""
    eng, wire = build({GET: agent_frame()})
    eng.focus(PaneId(" w1:p3\n"))
    assert wire.last.params == {"target": "w1:p3"}


def test_the_rendered_protocol_check_and_the_write_guard_agree() -> None:
    """One runner, one number. They used to be two."""
    eng, _ = build(settings=replace(SETTINGS, expected_protocol=DRIFTED), protocol_number=DRIFTED)
    snap = eng.snapshot()
    assert snap.protocol_ok is True  # what the header renders...
    eng.focus(P3)  # ...and what the guard allows


def test_an_unconfigured_snapshot_still_judges_itself_by_the_build() -> None:
    """Nobody who never configured anything sees a behaviour change."""
    eng, _ = build(protocol_number=DRIFTED)
    assert eng.snapshot().protocol_ok is False


# --------------------------------------------------------------------------
# The write path addresses by identity, not only by recycled pane id
# --------------------------------------------------------------------------


def test_a_recycled_pane_id_refuses_the_keystroke() -> None:
    """The stale-target gap, closed on the round trip that was already happening."""
    eng, wire = build({GET: agent_frame()})
    someone_else = as_session_id("11111111-1111-4111-8111-111111111111")
    with pytest.raises(TargetNotFound) as exc:
        eng.send_keys(Seat(P3, someone_else), "1")
    assert exc.value.code == SESSION_MOVED_CODE
    assert SEND_KEYS not in wire.methods  # nothing was typed
    with pytest.raises(TargetNotFound):
        eng.prompt(Seat(P3, someone_else), "hi")
    assert PROMPT not in wire.methods


def test_the_identity_re_check_passes_when_the_pane_still_holds_that_agent() -> None:
    eng, wire = build({GET: agent_frame()})
    eng.send_keys(Seat(P3, SESS), "1")
    assert wire.methods == [SNAPSHOT, GET, SEND_KEYS]


def test_the_identity_re_check_is_opt_in() -> None:
    """A caller that never learned the session is exactly as it was: address only."""
    eng, wire = build({GET: agent_frame()})
    eng.send_keys(Seat(P3), "1")
    assert SEND_KEYS in wire.methods


# --------------------------------------------------------------------------
# The lifecycle verbs
# --------------------------------------------------------------------------

PROCESS_INFO = method_of(wire_request.PaneProcessInfoRequest)


def process_info_frame(*, settled: bool = True) -> dict[str, JsonValue]:
    """A pane whose only foreground process is its own shell -- or one still running something, which is exactly what makes ``agent.start`` fail with ``agent_pane_busy`` on 2 of 5 unguarded attempts."""
    running: list[JsonValue] = [{"pid": 4242, "name": "bash", "argv": ["/bin/bash"]}]
    if not settled:
        running.append({"pid": 4243, "name": "direnv", "argv": ["direnv", "export"]})
    return {
        "id": "x",
        "result": {
            "type": "pane_process_info",
            "process_info": {
                "pane_id": "w1:p9",
                "shell_pid": 4242,
                "foreground_process_group_id": 4242,
                "foreground_processes": running,
            },
        },
    }


def test_a_process_info_reply_the_schema_rejects_still_names_its_pids() -> None:
    """The pids are salvaged from a frame whose envelope drifted, if that part still reads."""
    frame = process_info_frame(settled=False)
    del obj(frame, "result")["type"]
    eng, _ = build({PROCESS_INFO: frame})
    assert eng.pane_pids("w1:p9") == (4242, 4243)


def test_a_process_info_the_schema_rejects_names_no_pids() -> None:
    frame = process_info_frame()
    obj(frame, "result", "process_info")["shell_pid"] = "not a pid"
    eng, _ = build({PROCESS_INFO: frame})
    assert eng.pane_pids("w1:p9") == ()


def test_a_process_info_reply_with_no_process_info_is_invalid() -> None:
    eng, _ = build({PROCESS_INFO: {"id": "x", "result": {"type": "pane_process_info"}}})
    with pytest.raises(InvalidResponse, match="no 'process_info' object"):
        eng.pane_pids("w1:p9")


def started_frame(session: str | None = None) -> dict[str, JsonValue]:
    agent = raw_agent(pane_id="w1:p9")
    if session is None:
        agent.pop("agent_session", None)
    else:
        agent["agent_session"] = {
            "kind": "id",
            "agent": "claude",
            "source": "herdr:claude",
            "value": session,
        }
    return {
        "id": "x",
        "result": {"type": "agent_started", "agent": agent, "argv": ["claude"]},
    }


def tab_created_frame() -> dict[str, JsonValue]:
    return {
        "id": "x",
        "result": {
            "type": "tab_created",
            "tab": {
                "tab_id": "w1:t9",
                "workspace_id": "w1",
                "number": 9,
                "label": "x",
                "focused": False,
                "pane_count": 1,
                "agent_status": "unknown",
            },
            "root_pane": {
                "pane_id": "w1:p9",
                "terminal_id": "term_9",
                "workspace_id": "w1",
                "tab_id": "w1:t9",
                "focused": False,
                "agent_status": "unknown",
                "revision": 0,
            },
        },
    }


def test_a_created_tab_answers_with_the_pane_to_start_in() -> None:
    """``tab.create`` carries two payload objects, which is why the lifecycle verbs cannot go through ``_result``: there is no single field to unwrap."""
    eng, wire = build({TAB_CREATE: tab_created_frame()})
    assert eng.create_tab(NewTab("w1", "/somewhere", "x")) == "w1:p9"
    assert wire.last.params["focus"] is False, (
        "creating an agent from a phone must not move the desktop's cursor"
    )


def test_a_create_is_never_re_sent_by_the_transport() -> None:
    """A re-sent create leaks a tab with a live shell in it that nothing tidies."""
    eng, wire = build({TAB_CREATE: tab_created_frame()})
    eng.create_tab(NewTab("w1", "/somewhere", "x"))
    assert wire.last.idempotent is False


def test_a_start_waits_for_the_shell_before_it_asks() -> None:
    """The readiness gate: ``agent.start`` does not wait for a settled shell, so this does."""
    busy = iter([process_info_frame(settled=False), process_info_frame()])
    eng, wire = build({PROCESS_INFO: lambda _: next(busy), START: started_frame("s-1")})
    eng.start_agent(Launch("n", "claude", "w1:p9"))
    assert wire.methods.count(PROCESS_INFO) == 2
    assert wire.methods[-1] == START, "the start went out after the pane settled"


def test_a_pane_that_never_settles_refuses_the_start_rather_than_hanging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(client_module, "_SHELL_POLLS", 3)
    eng, wire = build({PROCESS_INFO: process_info_frame(settled=False)})
    with pytest.raises(EngineError) as caught:
        eng.start_agent(Launch("n", "claude", "w1:p9"))
    assert caught.value.code == PANE_BUSY_CODE
    assert START not in wire.methods


def test_a_busy_pane_is_the_one_write_this_client_retries() -> None:
    """And it is not a violation of the never-retry rule."""
    attempts: list[dict[str, JsonValue] | Exception] = [
        EngineError("busy", code=PANE_BUSY_CODE),
        started_frame("s-1"),
    ]

    def answer(_: dict[str, JsonValue]) -> dict[str, JsonValue]:
        reply = attempts.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    eng, wire = build({PROCESS_INFO: process_info_frame(), START: answer})
    assert eng.start_agent(Launch("n", "claude", "w1:p9")).session == "s-1"
    assert wire.methods.count(START) == 2


def test_every_other_start_failure_is_raised_on_the_first_attempt() -> None:
    eng, wire = build({PROCESS_INFO: process_info_frame(), START: EngineTimeout("too slow")})
    with pytest.raises(EngineTimeout):
        eng.start_agent(Launch("n", "claude", "w1:p9"))
    assert wire.methods.count(START) == 1


def test_a_started_agent_with_no_session_says_so_as_the_empty_string() -> None:
    """A start with no session yet says so as the empty string, never ``None``."""
    eng, _ = build({PROCESS_INFO: process_info_frame(), START: started_frame(None)})
    assert eng.start_agent(Launch("n", "claude", "w1:p9")).session == ""


def test_the_model_argument_reaches_the_runner_after_a_bare_dash_dash() -> None:
    eng, wire = build({PROCESS_INFO: process_info_frame(), START: started_frame("s")})
    eng.start_agent(Launch("n", "claude", "w1:p9", ("--model", "haiku")))
    start = next(c for c in wire.calls if c.method == START)
    assert start.params["args"] == ["--model", "haiku"]
    timeout_ms = start.params["timeout_ms"]
    assert isinstance(timeout_ms, int)
    assert timeout_ms > 3000, "the runner rejects a start timeout at or below 3000ms"


def test_a_close_re_checks_the_pane_is_still_held_by_that_session() -> None:
    """The recycled-pane-id bug at its worst: this is the one write that destroys."""
    eng, wire = build({GET: agent_frame()})
    someone_else = as_session_id("11111111-1111-4111-8111-111111111111")
    with pytest.raises(TargetNotFound) as caught:
        eng.close_pane(Seat(P3, someone_else))
    assert caught.value.code == SESSION_MOVED_CODE
    assert PANE_CLOSE not in wire.methods, "somebody else's pane was closed"

    eng.close_pane(Seat(P3, SESS))
    assert wire.methods[-1] == PANE_CLOSE


def test_a_close_is_never_re_sent_by_the_transport() -> None:
    """The guard that matters most here: a close retried after an ambiguous failure lands on whatever now holds the recycled id."""
    eng, wire = build({GET: agent_frame()})
    eng.close_pane(Seat(P3, SESS))
    assert wire.last.idempotent is False


def test_a_close_fails_closed_on_an_unexpected_protocol() -> None:
    eng, wire = build({GET: agent_frame()}, protocol_number=DRIFTED)
    with pytest.raises(ProtocolMismatch):
        eng.close_pane(Seat(P3, SESS))
    assert PANE_CLOSE not in wire.methods


# --------------------------------------------------------------------------
# The configuration boundary
# --------------------------------------------------------------------------


def test_an_injected_settings_object_is_never_second_guessed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The client dials the socket it was handed, whatever the environment names."""
    # Real settings names: the app's Settings would read both, and refuse the port outright.
    monkeypatch.setenv("HP_SOCKET_PATH", str(tmp_path / "from-the-environment.sock"))
    monkeypatch.setenv("HP_PORT", "not a port")
    handed = EngineSettings(socket_path=tmp_path / "handed.sock", command_timeout=1.5)
    eng = HerdrClient(handed)
    assert eng.settings is handed
    assert isinstance(eng.transport, SocketTransport)
    assert eng.transport.path == handed.socket_path
    assert eng.transport.timeout == handed.command_timeout


def test_the_engine_reads_the_apps_settings_into_its_own(tmp_path: Path) -> None:
    """The engine takes the socket and timeout from Settings, defaulting to herdr's own socket."""
    named = engine_settings(Settings(socket_path=tmp_path / "named.sock", command_timeout=1.5))
    assert named == EngineSettings(socket_path=tmp_path / "named.sock", command_timeout=1.5)
    unnamed = engine_settings(Settings(socket_path=None))
    assert unnamed.socket_path == Path(protocol.DEFAULT_SOCKET_PATH).expanduser()
    assert unnamed.command_timeout == Settings().command_timeout


def test_the_engine_declares_the_eight_fields_it_reads_and_no_more() -> None:
    """``EngineSettings`` is the engine's whole configuration seam, and the honest dependency."""
    engine_reads = {name for name in dir(EngineSettings) if not name.startswith("_")}
    assert engine_reads == {
        "socket_path",
        "command_timeout",
        "expected_protocol",
        "protocol_max_age",
        "protocol_drift_fails_writes",
        "read_source",
        "read_lines",
        "recent_max_lines",
    }
    assert "bind_host" not in engine_reads


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


def test_the_discovery_subscriptions_carry_no_pane_id() -> None:
    """That is the whole point of them: a subscription no roster can stale."""
    subs = discovery_subscriptions()
    assert [one["type"] for one in subs] == [
        "pane.created",
        "pane.agent_detected",
        "pane.closed",
    ]
    assert not any("pane_id" in one for one in subs)


def test_one_connection_carries_both_the_roster_and_the_discovery_events() -> None:
    """No third connection: `subscriptions` is a list and both kinds ride it."""
    eng, _ = build()
    stream = eng.watch_statuses(["w1:p1", "w9:pNEW"])
    assert stream.subscriptions == [
        {"type": "pane.agent_status_changed", "pane_id": "w1:p1"},
        {"type": "pane.agent_status_changed", "pane_id": "w9:pNEW"},
        {"type": "pane.created"},
        {"type": "pane.agent_detected"},
        {"type": "pane.closed"},
    ]


def test_an_empty_roster_still_leaves_a_stream_worth_opening() -> None:
    """A cold herd has no panes to follow and is exactly when discovery matters."""
    eng, _ = build()
    assert eng.watch_statuses([]).subscriptions == discovery_subscriptions()
