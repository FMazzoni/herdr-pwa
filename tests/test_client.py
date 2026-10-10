"""Client tests: the vocabulary, the shapes, and the two guards.

The wire is faked here, not the socket -- ``tests/test_transport.py`` stands up
a real AF_UNIX server and proves the framing, so these tests can inject a
:class:`FakeTransport` and stay about what this layer is actually responsible
for: which method a verb issues, which params it builds, which arm of herdr's
response union it accepts, and what it refuses to do.

The canned replies below are byte-faithful in shape to what herdr 0.8.0
actually answers on this box (the snapshot is a recorded capture), so a rename
or a shape change upstream breaks these tests rather than production.
"""

import ast
import inspect
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from conftest import Isolated
from herdr_pwa import protocol
from herdr_pwa.engine import (
    AgentStatus,
    BlockedPrompt,
    EngineError,
    EngineMisconfigured,
    EngineTimeout,
    HerdrClient,
    InvalidRequest,
    InvalidResponse,
    ProtocolMismatch,
    SocketTransport,
    TargetNotFound,
    as_pane_id,
    as_session_id,
    client,
)
from herdr_pwa.engine.generated import request as wire_request
from herdr_pwa.engine.generated import success_response as wire
from herdr_pwa.engine.transport import raise_for_error_frame
from herdr_pwa.settings import Settings


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    """No ambient configuration leaks into a test.

    ``tests/test_settings.py`` has had this for a while; this file did not, and
    it builds ``HerdrClient``s from real ``Settings``. This suite also runs
    *inside* a live session, where ``HERDR_SOCKET_PATH`` is genuinely exported,
    and a repo-root ``.env`` -- gitignored, and encouraged -- silently
    reconfigures it. Demonstrated before this landed: one environment variable
    produced two failures in tests that never mention the environment.
    """
    for name in list(os.environ):
        if name.startswith(("HP_", "HERDR_")):
            monkeypatch.delenv(name, raising=False)


FIXTURE = Path(__file__).parent / "fixtures" / "snapshot.example.json"
SNAPSHOT_JSON = FIXTURE.read_text()

#: Addresses, converted once and deliberately -- which is the whole ergonomic
#: claim of the ``PaneId`` typing. A route holding a URL segment does exactly
#: this: one ``as_pane_id`` call, no ``cast``, no ``# type: ignore``. Passing a
#: bare ``"w1:p3"`` to a verb is now a pyright error, and so is passing
#: ``agent.session``.
P3 = as_pane_id("w1:p3")
P5 = as_pane_id("w1:p5")
SESS = as_session_id("5b609dba-d756-493a-a3fc-b4e09bf0a183")

#: Read out of the generated request types, never spelled: these tests assert
#: against the same single source of truth the client builds from, so a rename
#: upstream moves both together instead of leaving a stale expectation passing.
SNAPSHOT = client._method_of(wire_request.SessionSnapshotRequest)
GET = client._method_of(wire_request.AgentGetRequest)
READ = client._method_of(wire_request.AgentReadRequest)
PROMPT = client._method_of(wire_request.AgentPromptRequest)
SEND_KEYS = client._method_of(wire_request.AgentSendKeysRequest)
FOCUS = client._method_of(wire_request.AgentFocusRequest)
# The lifecycle three, read off their own generated Literals for
# the same reason as the rest: a rename upstream must fail here rather than
# quietly disarming the guard that keeps a close from being retried.
START = client._method_of(wire_request.AgentStartRequest)
TAB_CREATE = client._method_of(wire_request.TabCreateRequest)
PANE_CLOSE = client._method_of(wire_request.PaneCloseRequest)
# The workspace three. `workspace.close` is the largest write in the app -- it ends
# every agent in a workspace at once -- so being registered as a write is not a
# formality here: it is what makes the protocol guard and the no-retry rule
# apply to it at all.
WS_CREATE = client._method_of(wire_request.WorkspaceCreateRequest)
WS_RENAME = client._method_of(wire_request.WorkspaceRenameRequest)
WS_CLOSE = client._method_of(wire_request.WorkspaceCloseRequest)


@dataclass
class Call:
    method: str
    params: dict[str, Any]
    timeout: float | None
    idempotent: bool


@dataclass
class FakeTransport:
    """The wire seam, recording every request and answering canned frames.

    Injected per client, which is what keeps two clients independent: a fake
    installed on one is invisible to the other and to the process-wide default.
    """

    replies: dict[str, Any] = field(default_factory=dict)
    calls: list[Call] = field(default_factory=list)
    closed: int = 0

    def call(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float | None = None,
        idempotent: bool = True,
    ) -> dict[str, Any]:
        self.calls.append(Call(method, dict(params), timeout, idempotent))
        reply: Any = self.replies.get(method, {"id": "x", "result": {"type": "ok"}})
        if isinstance(reply, Exception):
            raise reply
        answer = reply(params) if callable(reply) else reply
        assert isinstance(answer, dict)
        return answer

    def close(self) -> None:
        self.closed += 1

    @property
    def methods(self) -> list[str]:
        return [c.method for c in self.calls]

    @property
    def last(self) -> Call:
        return self.calls[-1]


# --------------------------------------------------------------------------
# Canned frames
# --------------------------------------------------------------------------


#: A protocol this build is NOT expecting, derived rather than written down.
#: These tests used the literal 20 as "the drifted one" while the build
#: expected 19. When herdr moved to 20 every drift test silently stopped testing
#: drift -- they were asserting that the CURRENT protocol was refused. Deriving it
#: means the next bump cannot quietly turn these into their own opposite.
DRIFTED = protocol.EXPECTED_PROTOCOL + 1


def snapshot_frame(protocol_number: int | None = None) -> dict[str, Any]:
    payload = json.loads(SNAPSHOT_JSON)
    if protocol_number is not None:
        payload["result"]["snapshot"]["protocol"] = protocol_number
    return payload


def raw_agent(**overrides: Any) -> dict[str, Any]:
    raw = dict(json.loads(SNAPSHOT_JSON)["result"]["snapshot"]["agents"][0])
    raw.update(overrides)
    return raw


def agent_frame(**overrides: Any) -> dict[str, Any]:
    return {
        "id": "x",
        "result": {"type": "agent_info", "agent": raw_agent(**overrides)},
    }


def read_frame(text: str) -> dict[str, Any]:
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
    replies: dict[str, Any] | None = None,
    *,
    settings: Settings | None = None,
    protocol_number: int | None = None,
) -> tuple[HerdrClient, FakeTransport]:
    """A client wired to a fake wire that answers a healthy snapshot by default."""
    frames: dict[str, Any] = {SNAPSHOT: snapshot_frame(protocol_number)}
    frames.update(replies or {})
    wire = FakeTransport(replies=frames)
    return HerdrClient(settings or Isolated(), transport=wire), wire


@pytest.fixture(autouse=True)
def eng():
    """A genuinely fresh default client per test -- no state carried across.

    The state belongs to an instance, so "fresh process" is spelled by dropping
    the cached instance. The module-level verbs delegate to whatever
    ``default_client()`` returns, so patching this object patches what
    ``client.snapshot()`` runs.
    """
    client.default_client.cache_clear()
    instance = client.default_client()
    instance.transport = FakeTransport(replies={SNAPSHOT: snapshot_frame()})
    yield instance
    client.default_client.cache_clear()


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
    """The retry rule and the write guard are driven off the same set.

    A resent ``agent.prompt`` is a second prompt typed at a real agent, so the
    transport is told which calls it may repeat -- and it is told by the layer
    that knows what the method means.
    """
    eng, wire = build({GET: agent_frame()})
    eng.prompt(P3, "hi")
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
    """The failure the generated models exist to catch.

    ``ResponseResult`` is discriminated by ``type``; a reply resolving to an arm
    nobody asked for is somebody else's answer, and rendering it as this
    caller's agent is exactly the hazard. Dict probing would have accepted it.
    """
    eng, _ = build({GET: {"id": "x", "result": {"type": "ok"}}})
    with pytest.raises(InvalidResponse) as exc:
        eng.agent(P3)
    assert "AgentInfoResponseResult" in exc.value.message


def test_a_read_of_a_frame_the_schema_rejects_warns_and_still_serves(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Same doctrine as the protocol guard, one level down.

    An agent status invented upstream must not blank the dashboard: the
    hand-written models coerce it to ``UNKNOWN``, so the generated schema's
    stricter reading is a warning on a read, not a failure.
    """
    frame = snapshot_frame()
    frame["result"]["snapshot"]["agents"][0]["agent_status"] = "vibing"
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
    assert eng.read(P3, lines=40, source="detection") == "viewport text"
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
    """Pinned on purpose, and for a stronger reason than the cost.

    A `recent` text read asking for more lines than the viewport makes herdr
    drive the AGENT'S OWN mouse-scroll interface: it scrolls that pane up and
    restores it, and the person sitting at the terminal watches it jump and
    snap back, once per read. Measured on our herdr 0.8.0, 10-row pane:
    lines=10 -> 0.00s, lines=11 -> 1.09s. So scrollback must be deliberate and
    user-initiated and must never happen on a poll -- and the ~35ms/line that
    made the old lines=200/source=recent default always blow ``command_timeout``
    (6905ms measured) is that same scrolling, seen from this end."""
    eng, wire = build({READ: read_frame("viewport text")})
    eng.read(P3)
    assert wire.last.params["source"] == "visible"


def test_an_unnamed_source_comes_from_settings_rather_than_a_literal() -> None:
    """The field was declared, validated, documented and read by nobody.

    ``lines`` already defaulted from ``settings.read_lines`` one line above, so
    ``read_source`` was the missing half of a pair. Setting it now changes what
    a call that names no source does, which is what its description promised.
    """
    eng, wire = build(
        {READ: read_frame("viewport text")},
        settings=Isolated(read_source="detection"),
    )
    eng.read(P3)
    assert wire.last.params["source"] == "detection"


def test_a_named_source_still_beats_the_configured_default() -> None:
    """Wiring the default may not quietly override a caller who was explicit.

    ``server._pane_now`` names ``visible`` so the before/after comparison in
    ``sendguard.landed`` compares like with like, and ``blocked_prompt`` names
    ``detection`` because that is where the pending question is. Neither may be
    reconfigured out from under them by an ``HP_READ_SOURCE``.
    """
    eng, wire = build(
        {READ: read_frame("viewport text")},
        settings=Isolated(read_source="detection"),
    )
    eng.read(P3, source="visible")
    assert wire.last.params["source"] == "visible"
    eng.blocked_prompt(P3)
    assert wire.last.params["source"] == "detection"


def test_a_configured_recent_default_is_capped_like_an_explicit_one() -> None:
    """The cap is on the resolved source, not on the caller's argument.

    Otherwise wiring the field would have opened a route to the 200-line
    ``recent`` read measured at ~7s of somebody else's scrolling
    pane -- reachable by configuration and by nothing else.
    """
    eng, wire = build(
        {READ: read_frame("viewport text")},
        settings=Isolated(read_source="recent", read_lines=200),
    )
    eng.read(P3)
    assert wire.last.params["source"] == "recent"
    assert wire.last.params["lines"] == eng.settings.recent_max_lines


def test_a_recent_read_is_capped_so_it_cannot_outrun_the_timeout() -> None:
    eng, wire = build({READ: read_frame("viewport text")})
    eng.read(P3, lines=200, source="recent")
    lines = wire.last.params["lines"]
    assert lines == eng.settings.recent_max_lines
    assert lines * 0.035 < eng.settings.command_timeout


def test_the_reason_for_the_visible_default_is_written_where_the_code_is() -> None:
    """The docstring is the fix, so the docstring is what regresses.

    Cost alone is the weaker half and reads as an optimisation somebody may
    later decide to spend. What must survive is that a `recent` read past the
    viewport moves an operator's screen.
    """
    doc = (
        Path(__file__).resolve().parents[1]
        / "docs/modules/engine/client/HerdrClient.read.md"
    ).read_text(encoding="utf-8")
    assert "viewport_rows" in doc, "the exact threshold is not recorded"
    assert "mouse-scroll" in doc, "the mechanism is not named"
    assert "never" in doc and "poll" in doc, "the rule it implies is not stated"


def test_a_visible_read_takes_its_line_count_as_given() -> None:
    eng, wire = build({READ: read_frame("viewport text")})
    eng.read(P3, lines=200)
    assert wire.last.params["lines"] == 200


def test_a_read_answering_no_text_is_invalid() -> None:
    eng, _ = build({READ: {"id": "x", "result": {"type": "pane_read", "read": {}}}})
    with pytest.raises(InvalidResponse):
        eng.read(P3)


def decoded_frame(text: bytes) -> dict[str, Any]:
    """A reply frame carrying ``text``, round-tripped through the real wire.

    These two tests used to hand the fake a dict that already held the string
    they then asserted on -- no frame was ever encoded, no byte ever decoded, and
    they would have passed against a stub. Building the bytes and putting them
    through :meth:`SocketTransport._decode` and
    :func:`raise_for_error_frame` -- the actual decode and the actual
    error-envelope check -- is what makes them mean something at this layer.
    """
    line = json.dumps(read_frame("\x00PLACEHOLDER\x00")).encode()
    payload = SocketTransport._decode(line.replace(b"\\u0000PLACEHOLDER\\u0000", text))
    raise_for_error_frame(payload)  # pane content is data, never an envelope
    return payload


def test_pane_text_that_merely_looks_like_json_still_comes_back() -> None:
    """Pane content is data, never an envelope. A pane showing a JSON error
    document must not be mistaken for the runner reporting one."""
    looks_like = '{"error": {"code": "nope", "message": "this is pane content"}}'
    eng, _ = build({READ: decoded_frame(json.dumps(looks_like)[1:-1].encode())})
    assert eng.read(P3) == looks_like


def test_undecodable_pane_content_is_returned_not_raised() -> None:
    """The replacement glyph a mid-multibyte viewport cut produces.

    Genuinely undecodable bytes: a UTF-8 sequence cut in half by a viewport
    boundary. ``_decode`` replaces them rather than raising a
    ``UnicodeDecodeError``, which is a ``ValueError`` and would sail past every
    ``except EngineError`` in the app.
    """
    eng, _ = build({READ: decoded_frame(b"caf\xc3 binary bytes")})
    text = eng.read(P3)
    assert "\ufffd" in text and text.endswith("binary bytes")


def test_blocked_prompt_reads_the_detection_source() -> None:
    # The footer is part of the input on purpose: now a numbered run
    # with no key-hint footer and no selection marker is NOT believed to be a
    # live menu -- and it needs BOTH -- so a menu without them
    # would parse to zero options and this
    # test would be asserting the parser's conservatism rather than the
    # client's source choice.
    menu = "Run the tests?\n❯ 1. Yes\n  2. No\n\nEnter to select · Esc to cancel\n"
    eng, wire = build({READ: read_frame(menu)})
    p = eng.blocked_prompt(P5, session=SESS)
    assert wire.last.params["source"] == "detection"
    assert p.target == "w1:p5"
    assert p.session == SESS
    assert [o.label for o in p.options] == ["Yes", "No"]


# --------------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------------


def test_prompt_sends_the_text_as_a_named_parameter() -> None:
    """The CLI's positional-argv trap is gone with the CLI.

    ``--`` before the text made herdr reject it, so text starting with a dash
    was a live hazard that had to be remembered. A named parameter cannot be
    read as an option at all.
    """
    eng, wire = build({GET: agent_frame()})
    eng.prompt(P3, "--not-a-flag")
    assert wire.last.method == PROMPT
    assert wire.last.params == {"target": "w1:p3", "text": "--not-a-flag"}


def test_send_keys_forwards_every_key_as_a_list() -> None:
    eng, wire = build({GET: agent_frame()})
    eng.send_keys(P3, "1", "enter")
    assert wire.last.method == SEND_KEYS
    assert wire.last.params == {"target": "w1:p3", "keys": ["1", "enter"]}


def test_send_keys_requires_a_key() -> None:
    """And says so inside the taxonomy: a bare ``ValueError`` out of a public
    verb is a 500 with a traceback the moment a route forwards user input."""
    eng, wire = build()
    with pytest.raises(InvalidRequest) as caught:
        eng.send_keys(P3)
    assert isinstance(caught.value, EngineError | ValueError)
    assert caught.value.code == "invalid_request"
    assert wire.calls == []


def test_an_unknown_read_source_is_an_engine_error_not_a_bare_valueerror() -> None:
    """``?source=`` off a phone is a typo, not a crash. It never leaves the
    process either way -- but it has to arrive as something a route can map."""
    eng, wire = build()
    with pytest.raises(InvalidRequest) as caught:
        eng.read(P3, source="visable")
    assert isinstance(caught.value, EngineError)
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
    """The safety the socket unlocks and the CLI could not.

    ``launch_pending`` means the pane exists but the provider has not come up in
    it. Keys sent now are not queued; they are typed into a terminal that is not
    reading, and the user believes they answered the prompt.
    """
    eng, wire = build({GET: agent_frame(launch_pending=True)})
    with pytest.raises(EngineError) as exc:
        eng.send_keys(P3, "1")
    assert exc.value.code == client.NOT_READY_CODE
    assert SEND_KEYS not in wire.methods  # nothing was typed


def test_a_prompt_is_refused_when_the_agent_is_not_interactive() -> None:
    eng, wire = build({GET: agent_frame(interactive_ready=False)})
    with pytest.raises(EngineError) as exc:
        eng.prompt(P3, "hi")
    assert exc.value.code == client.NOT_READY_CODE
    assert PROMPT not in wire.methods


def test_a_runner_that_does_not_report_readiness_is_not_blocked() -> None:
    """Absent is not false, and this matters today: herdr 0.8.0 omits both
    fields entirely, so refusing on unknown would refuse every write."""
    raw = raw_agent()
    assert "launch_pending" not in raw and "interactive_ready" not in raw
    eng, wire = build(
        {GET: {"id": "x", "result": {"type": "agent_info", "agent": raw}}}
    )
    eng.prompt(P3, "hi")
    assert PROMPT in wire.methods


def test_a_ready_agent_is_written_to() -> None:
    eng, wire = build({GET: agent_frame(interactive_ready=True, launch_pending=False)})
    eng.send_keys(P3, "enter")
    assert wire.methods == [SNAPSHOT, GET, SEND_KEYS]


def test_the_readiness_check_is_fail_closed_on_a_frame_it_cannot_read() -> None:
    """A write may not proceed on a frame this build cannot validate.

    The read path degrades on the same frame (see the read test above); a write
    does not, because "I could not understand the agent's state" is not a
    licence to type at it.
    """
    eng, _ = build({GET: agent_frame(agent_status="vibing")})
    with pytest.raises(InvalidResponse):
        eng.prompt(P3, "hi")


# --------------------------------------------------------------------------
# The interrupt is the exception, because it is the way OUT of busy
# --------------------------------------------------------------------------


def test_the_interrupt_is_sent_to_an_agent_that_is_not_accepting_input() -> None:
    """The bug reported from the phone: ctrl-c refused on a busy agent.

    ``interactive_ready`` false is the state an agent grinding through
    background work reports, and it is precisely the state a user taps ctrl-c
    in. Refusing to interrupt an agent *because* it is busy inverts the purpose
    of the key -- so the readiness guard, which exists to stop a keystroke being
    swallowed by a terminal that is not reading, does not apply to the one
    keystroke whose whole job is to make it read again.
    """
    eng, wire = build({GET: agent_frame(interactive_ready=False)})
    eng.send_keys(P3, client.INTERRUPT_KEY)
    assert SEND_KEYS in wire.methods


def test_every_other_key_is_still_refused_to_an_agent_that_is_not_accepting() -> None:
    """The exemption is one key wide. An answer to a prompt is still swallowed."""
    eng, wire = build({GET: agent_frame(interactive_ready=False)})
    with pytest.raises(EngineError) as exc:
        eng.send_keys(P3, "enter")
    assert exc.value.code == client.NOT_READY_CODE
    assert SEND_KEYS not in wire.methods


def test_the_interrupt_bundled_with_another_key_is_not_exempt() -> None:
    """``ctrl-c`` then ``enter`` is not an interrupt; the second key is input."""
    eng, wire = build({GET: agent_frame(interactive_ready=False)})
    with pytest.raises(EngineError) as exc:
        eng.send_keys(P3, client.INTERRUPT_KEY, "enter")
    assert exc.value.code == client.NOT_READY_CODE
    assert SEND_KEYS not in wire.methods


def test_the_interrupt_is_still_refused_while_the_agent_is_launching() -> None:
    """The other half of the guard stands: launching is not busy.

    ``launch_pending`` means the provider has not come up in the pane yet, so
    there is nothing there to interrupt -- a ctrl-c lands on whatever is still
    starting up. That is a different hazard from "busy", and the interrupt rule does not
    touch it. One rule carves out the one case where something IS there, which is
    when herdr says ``blocked`` beside it; this agent does not.
    """
    eng, wire = build({GET: agent_frame(launch_pending=True, interactive_ready=False)})
    with pytest.raises(EngineError) as exc:
        eng.send_keys(P3, client.INTERRUPT_KEY)
    assert exc.value.code == client.NOT_READY_CODE
    assert SEND_KEYS not in wire.methods


def test_a_key_reaches_an_agent_that_is_launching_and_blocked() -> None:
    """`blocked` beside `launch_pending` is the state a key is FOR.

    The two arrive together whenever a provider stops during startup to ask
    something -- a trust prompt, a permission dialog -- which is the shape an
    agent prompted immediately after creation lands in. Measured live against
    the runner on 2026-08-29: it takes the key and moves the cursor. This guard
    was the only thing refusing, and refusing it left the user's phone looking
    at a question it could not answer.
    """
    eng, wire = build({GET: agent_frame(launch_pending=True, agent_status="blocked")})
    eng.send_keys(P3, "enter")
    assert SEND_KEYS in wire.methods


def test_a_launching_agent_that_is_not_blocked_still_refuses_the_key() -> None:
    """The exception is `blocked`, not `launch_pending`. Nothing else moved."""
    for status in ("idle", "working", "unknown"):
        eng, wire = build({GET: agent_frame(launch_pending=True, agent_status=status)})
        with pytest.raises(EngineError) as exc:
            eng.send_keys(P3, "enter")
        assert exc.value.code == client.NOT_READY_CODE
        assert SEND_KEYS not in wire.methods


def test_a_launching_blocked_agent_at_a_recycled_pane_is_still_refused() -> None:
    """The session-holder check is untouched: a key must not reach the wrong agent."""
    eng, wire = build({GET: agent_frame(launch_pending=True, agent_status="blocked")})
    with pytest.raises(TargetNotFound):
        eng.send_keys(P3, "enter", session=as_session_id("not-the-holder"))
    assert SEND_KEYS not in wire.methods


def test_the_interrupt_is_still_refused_at_a_pane_somebody_else_holds() -> None:
    """Pane ids are recycled: an exempt key must not go to the wrong agent."""
    eng, wire = build({GET: agent_frame(interactive_ready=False)})
    with pytest.raises(TargetNotFound):
        eng.send_keys(P3, client.INTERRUPT_KEY, session=as_session_id("not-the-holder"))
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
    with pytest.raises(ProtocolMismatch):
        getattr(eng, verb)(P3, "x") if verb != "focus" else eng.focus(P3)
    assert wire.methods == [SNAPSHOT]  # nothing was driven


def test_a_write_checks_the_protocol_before_driving() -> None:
    eng, wire = build({GET: agent_frame()})
    eng.focus(P3)
    assert wire.methods == [SNAPSHOT, FOCUS]


def test_a_write_verb_runs_the_protocol_guard_once_not_twice() -> None:
    """``prompt`` guarded in the verb *and* again inside ``_call``.

    Harmless only because the first run refreshed the freshness window; with a
    short ``protocol_max_age`` the window is already stale by the time the
    second run looks, so it is two snapshots per keystroke.
    """
    eng, wire = build(
        {GET: agent_frame()},
        settings=Isolated(protocol_max_age=1e-6),
    )
    eng.prompt(P3, "hi")
    assert wire.methods == [SNAPSHOT, GET, PROMPT]


def test_the_write_guard_cannot_recurse_through_its_own_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``WRITE_METHODS`` is three hand-written strings, and sets grow.

    The guard refreshes by taking a snapshot, which re-enters ``_call``: the day
    the snapshot method joins the write set, that is unbounded recursion. The
    pinning test only checks the three methods that are in the set today, so it
    would not catch the fourth being added. This is the guard against it -- an
    honest ``EngineError``, not a ``RecursionError`` nothing catches.
    """
    monkeypatch.setattr(client, "WRITE_METHODS", frozenset({SNAPSHOT, PROMPT}))
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
    """Snapshot once at startup, poll per agent forever -- the plan. The
    cached OK must expire, or a runner restart onto a new protocol keeps being
    written to."""
    eng, wire = build({GET: agent_frame()})
    eng.snapshot()
    eng.last_protocol_at -= eng.settings.protocol_max_age + 1  # the OK went stale
    wire.replies[SNAPSHOT] = snapshot_frame(DRIFTED)  # runner restarted on a new one
    with pytest.raises(ProtocolMismatch):
        eng.prompt(P3, "hi")
    assert wire.methods == [SNAPSHOT, SNAPSHOT]  # re-checked, not driven


def test_a_stale_but_still_correct_protocol_is_simply_refreshed() -> None:
    eng, wire = build({GET: agent_frame()})
    eng.snapshot()
    eng.last_protocol_at -= eng.settings.protocol_max_age + 1
    eng.prompt(P3, "hi")
    assert wire.methods == [SNAPSHOT, SNAPSHOT, GET, PROMPT]


def test_drift_can_be_configured_not_to_fail_writes() -> None:
    """Off by default and stays off by default; this proves the switch is wired
    rather than decorative -- and that opting out skips the extra snapshot."""
    eng, wire = build(
        settings=Settings(protocol_drift_fails_writes=False, expected_protocol=44),
        protocol_number=DRIFTED,
    )
    eng.focus(P3)
    assert wire.methods == [FOCUS]


# --------------------------------------------------------------------------
# The guard is not bypassable
# --------------------------------------------------------------------------


def test_there_is_no_public_arbitrary_method_entry_point() -> None:
    """An arbitrary-method entry point exported from the package is a supported
    way to drive an agent around the protocol guard, and one that names no
    runner command, so neither one-file-rule test would catch it."""
    import herdr_pwa.engine as engine

    for name in ("run", "run_text", "call", "request"):
        assert name not in engine.__all__
        assert name not in client.__all__
        assert not hasattr(engine, name)
        assert not hasattr(client, name)
    for verb in (
        "agent",
        "agents",
        "blocked_prompt",
        "focus",
        "prompt",
        "read",
        "send_keys",
        "snapshot",
    ):
        assert verb in engine.__all__ and hasattr(engine, verb)


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
    request_type, params
) -> None:
    """The guard is a property of issuing a request, not of remembering to ask.

    This is the check that survived the transport swap: it used to live in
    ``_run``/``_run_text`` and now lives in ``_call``, which every verb goes
    through.
    """
    eng, wire = build(protocol_number=DRIFTED)
    with pytest.raises(ProtocolMismatch):
        eng._call(request_type, params)
    assert wire.methods == [SNAPSHOT]  # nothing was driven


def test_a_read_through_the_private_caller_is_never_blocked() -> None:
    """Reads degrade, only writes fail closed."""
    eng, _ = build(protocol_number=DRIFTED)
    assert eng.snapshot().protocol == DRIFTED


#: The public verbs that only *read*. Everything else a public verb on
#: ``HerdrClient`` issues is presumed to drive an agent, which is what makes the
#: test below derive rather than enumerate: a fourth **write** verb needs no edit
#: here and fails immediately if it was forgotten in ``WRITE_METHODS``, while a
#: fourth **read** verb has to be declared here on purpose. Unlisted means write,
#: so the direction of the mistake is the safe one.
READ_VERBS = frozenset(
    {
        "snapshot",
        "agents",
        "agent",
        "read",
        "blocked_prompt",
        # The lifecycle two: the readiness gate in front of a start, and the re-read
        # that waits for a just-started agent's session UUID. Both type nothing
        # and create nothing, and both are declared here ON PURPOSE rather than
        # left to the default -- the default is "write", which is the safe way
        # round for a mistake and the wrong answer for these.
        "shell_pane",
        "started_agent",
        # Two more, both read-only and both filed verbatim by the capture
        # store: which detection rule decided this state, and the runner's own
        # `agent.get` frame before this package normalizes it.
        "explain",
        "agent_frame",
        # And one more. It asks the same `pane.process_info` `shell_pane` does,
        # for the pids whose environment says whether an orchestrator is already
        # watching this agent. It types nothing and starts nothing.
        "pane_pids",
    }
)


def _requests_issued_by(verb: ast.FunctionDef) -> set[str]:
    """The dotted method names a verb's own body constructs a request type for.

    Reads the ``wire_request.<X>Request`` attributes out of the AST and turns
    each into its method name through the same ``Literal`` the client reads --
    so this derives from the generated schema, never from a copy of it.
    """
    types = {
        node.attr
        for node in ast.walk(verb)
        if isinstance(node, ast.Attribute) and node.attr.endswith("Request")
    }
    return {client._method_of(getattr(wire_request, name)) for name in types}


def test_every_write_verb_is_registered_as_a_write() -> None:
    """Derived, not enumerated -- because the enumeration could not fail.

    The old version pinned today's three strings against their generated
    ``Literal``s, which catches an upstream *rename* but not the thing that
    actually costs: a fourth write verb added to the client and forgotten in
    ``WRITE_METHODS``. That verb would be silently retryable by the transport
    *and* unguarded by the protocol check, with nothing failing.

    So the set is derived from the client itself: every public verb that is not
    declared a read contributes the methods it issues, and the union must be
    exactly ``WRITE_METHODS``. The rename pin is kept underneath, because it is
    still the thing that makes the derivation mean anything.
    """
    body = ast.parse(inspect.getsource(client))
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
    """The two spellings that would silently 'work' if assumed derivable.

    ``send-keys`` becomes ``send_keys``, and ``api snapshot`` becomes
    ``session.snapshot`` -- a different noun entirely. Nothing derives one from
    the other, which is why the client reads the socket names off the generated
    models rather than transforming the CLI's.
    """
    assert SEND_KEYS == "agent.send_keys"
    assert SNAPSHOT == "session.snapshot"
    assert protocol.CMD_SNAPSHOT == ("api", "snapshot")
    assert protocol.CMD_AGENT_SEND_KEYS == ("agent", "send-keys")


def test_a_method_name_is_read_off_the_generated_literal() -> None:
    """The mechanism the whole no-second-copy claim rests on."""
    assert client._method_of(wire_request.AgentPromptRequest) == "agent.prompt"
    assert client._payload_field(wire.SessionSnapshotResponseResult) == "snapshot"
    assert client._payload_field(wire.AgentInfoResponseResult) == "agent"


def test_a_request_type_that_pins_no_single_method_is_refused() -> None:
    """The derivation fails loudly if codegen ever stops pinning one literal."""

    class Bogus(BaseModel):
        id: str
        method: str
        params: dict[str, Any]

    with pytest.raises(InvalidResponse):
        client._method_of(Bogus)


def test_the_request_that_goes_on_the_wire_is_what_the_schema_dumps() -> None:
    """Params are serialized by the generated model, not assembled by hand.

    The enum members come back as their wire strings, and fields the caller did
    not set are omitted rather than sent as nulls -- both properties of the
    generated model, and both asserted here because the transport just forwards
    whatever this layer produces.
    """
    eng, wire_ = build({READ: read_frame("x")})
    eng.read(P3, lines=5, source="detection")
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
    """The defect, stated as a test.

    While the cache was three module globals, a client that had seen a good
    protocol authorised writes issued through *any* other view. Two instances
    must be able to hold different, simultaneous truths -- and now they also
    hold different connections.
    """
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
    assert client.default_client().last_protocol is None


def test_two_clients_can_be_configured_differently() -> None:
    """One runner, two expectations: config is per instance too, so a second
    view built for a DRIFTED protocol is writable while the default is not."""
    old, _ = build(
        settings=Isolated(expected_protocol=protocol.EXPECTED_PROTOCOL),
        protocol_number=DRIFTED,
    )
    new, _ = build(
        settings=Isolated(expected_protocol=DRIFTED), protocol_number=DRIFTED
    )
    with pytest.raises(ProtocolMismatch):
        old.focus(P3)
    new.focus(P3)
    assert old.settings.command_timeout == new.settings.command_timeout


def test_the_warn_once_flag_resets_with_a_fresh_instance(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Warn-once must mean once *per client*, not once per process -- otherwise
    the drift warning is emitted a single time for the life of the interpreter
    and no later view ever says it."""
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
    assert (
        caplog.text.lower().count(f"protocol {DRIFTED}") == 1
    )  # said again, by a new view

    caplog.clear()
    first.forget_protocol()
    assert (first.last_protocol, first.warned_protocol) == (None, None)
    with caplog.at_level("WARNING"):
        first.snapshot()
    assert caplog.text.lower().count(f"protocol {DRIFTED}") == 1


def test_the_module_verbs_delegate_to_the_default_client(eng) -> None:
    """Callers that never heard of per-instance state keep working, and see one instance."""
    assert client.get_client() is eng is client.default_client()
    client.snapshot()
    client.agents()
    client.focus(P3)
    assert eng.last_protocol == protocol.EXPECTED_PROTOCOL
    assert eng.transport.methods == [SNAPSHOT, SNAPSHOT, FOCUS]


def test_a_client_built_with_nothing_opens_nothing() -> None:
    """Construction must not dial: the default client is built on first *use*,
    and a service that starts before the runner does must not die at import."""
    fresh = HerdrClient(Isolated(socket_path=Path("/nonexistent/x.sock")))
    wire_ = fresh.transport
    assert isinstance(wire_, SocketTransport)
    assert wire_._sock is None


def test_there_is_no_module_level_protocol_state_left() -> None:
    """The literal defect: three globals mutated through a ``global`` statement.

    The ``"global " not in source`` half of this was a substring check on the
    module's own text, defeated by every spelling the guardrail module
    next door catalogues -- ``globals()["x"] = 1``, or the word appearing in a
    docstring explaining its absence. It walks the AST now, which sees a
    ``global`` statement however it is written and nothing that merely looks
    like one.

    The ``hasattr`` half was always the real check and is kept: it asserts the
    state is *gone*, not merely un-rebound.
    """
    tree = ast.parse(Path(client.__file__).read_text(encoding="utf-8"))
    declared = [
        name
        for node in ast.walk(tree)
        if isinstance(node, ast.Global)
        for name in node.names
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
        assert not hasattr(client, name)


# --------------------------------------------------------------------------
# The type discipline reaches the keystroke, not just the lookup
# --------------------------------------------------------------------------

#: The finding, verbatim, as a source file for pyright to judge. Every line here
#: type-checked clean while the write verbs took a plain ``str``: a ``NewType`` is
#: assignable to its supertype, so ``SessionId`` sailed into ``target: str``. The
#: discipline caught the harmless lookup on line 3 and missed both keystrokes.
PROBE = """
from herdr_pwa.engine import HerdrClient, Snapshot

c = HerdrClient()
s = Snapshot()
a = s.agents[0]

c.send_keys(a.session, "1")
c.prompt(a.session, "rm -rf /")
s.by_session(a.target)
"""

#: What the probe must now produce: one error per line, all three of them
#: ``reportArgumentType``, and none of them on a line the probe did not write.
PROBE_ERROR_LINES = {8, 9, 10}


def _pyright(path: Path) -> dict[str, Any]:
    """Run the repo's own pyright over one file and return its JSON report."""
    done = subprocess.run(
        [sys.executable, "-m", "pyright", "--outputjson", str(path)],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parent.parent,
        check=False,
    )
    if not done.stdout.strip():
        pytest.skip(f"pyright did not run: {done.stderr[:400]}")
    return json.loads(done.stdout)


def test_a_session_id_cannot_reach_a_write_verb(tmp_path: Path) -> None:
    """The ticket's probe, run for real rather than reasoned about.

    ``send_keys(agent.session, "1")`` is a keystroke aimed by an identity at a
    verb that wants an address. It used to be zero pyright errors, and it was
    fail-closed only because herdr answers ``target_not_found`` -- which
    ``as_pane_id``'s own docstring names as the thing a future runner might stop
    doing. Now all three lines are static errors, and the two that matter are the
    two that used to pass.
    """
    probe = tmp_path / "probe.py"
    probe.write_text(PROBE)
    report = _pyright(probe)
    errors = [d for d in report["generalDiagnostics"] if d["severity"] == "error"]
    assert {d["range"]["start"]["line"] + 1 for d in errors} == PROBE_ERROR_LINES, (
        f"expected an error on each probe line, got {errors}"
    )
    assert all(d["rule"] == "reportArgumentType" for d in errors)


def test_a_session_id_handed_to_a_write_verb_raises_at_runtime() -> None:
    """The static error is the fence; this is the runtime shape guard behind it.

    Untyped values reach these verbs -- a URL segment, a notification payload --
    and for those the type says nothing. A UUID where an address belongs is a
    caller bug and must raise here rather than be handed to the runner, which is
    exactly the guard ``by_session``/``by_target`` have always had.
    """
    eng, wire = build({GET: agent_frame()})
    uuid = "5b609dba-d756-493a-a3fc-b4e09bf0a183"
    for call in (
        lambda: eng.send_keys(uuid, "1"),  # type: ignore
        lambda: eng.prompt(uuid, "hi"),  # type: ignore
        lambda: eng.focus(uuid),  # type: ignore
        lambda: eng.agent(uuid),  # type: ignore
        lambda: eng.read(uuid),  # type: ignore
        lambda: eng.blocked_prompt(uuid),  # type: ignore
    ):
        with pytest.raises(ValueError, match="session id"):
            call()
    assert wire.calls == []  # nothing reached the runner


def test_a_pane_id_cannot_be_smuggled_into_a_blocked_prompt() -> None:
    """``BlockedPrompt`` is what a tap-to-answer UI builds its keystroke from."""
    with pytest.raises(ValueError, match="session id"):
        BlockedPrompt.parse("q", target="5b609dba-d756-493a-a3fc-b4e09bf0a183")  # type: ignore
    with pytest.raises(ValueError, match="pane id"):
        BlockedPrompt.parse("q", session="w1:p3")  # type: ignore
    p = BlockedPrompt.parse("q", target=as_pane_id("  w1:p3 "), session=SESS)
    assert (p.target, p.session) == ("w1:p3", SESS)


def test_the_address_is_normalised_before_it_goes_on_the_wire() -> None:
    """A URL segment arrives with whitespace more often than anyone expects.

    Passed here untyped on purpose: a caller that went through ``as_pane_id``
    was already normalised by it, so what this pins is the verb's *own* guard --
    the one that has to hold for the values the type system never saw.
    """
    eng, wire = build({GET: agent_frame()})
    eng.focus(" w1:p3\n")  # type: ignore
    assert wire.last.params == {"target": "w1:p3"}


def test_the_rendered_protocol_check_and_the_write_guard_agree() -> None:
    """One runner, one number. They used to be two.

    ``Snapshot.protocol_ok`` compared against the module-level
    ``EXPECTED_PROTOCOL`` while ``_require_writable`` compared against
    ``settings.expected_protocol``. A client configured for protocol 20, talking
    to a runner speaking the same, therefore rendered a "protocol mismatch" banner over
    a dashboard whose writes it was perfectly happy to allow -- the setting was
    honoured by the guard and ignored by the view.
    """
    eng, _ = build(
        settings=Isolated(expected_protocol=DRIFTED), protocol_number=DRIFTED
    )
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
    """The stale-target gap, closed on the round trip that was already happening.

    ``prompt``/``send_keys`` do three locked round trips (snapshot, readiness,
    write) and used to address purely by pane id -- the one place model.py's own
    rule ("re-check against the target before it drives anything") was not
    followed. Pane ids are recycled, so a few seconds of staleness is a keystroke
    in a stranger's terminal. The readiness round trip already carries
    ``agent_session``, so comparing it costs nothing on the wire.
    """
    eng, wire = build({GET: agent_frame()})
    someone_else = as_session_id("11111111-1111-4111-8111-111111111111")
    with pytest.raises(TargetNotFound) as exc:
        eng.send_keys(P3, "1", session=someone_else)
    assert exc.value.code == client.SESSION_MOVED_CODE
    assert SEND_KEYS not in wire.methods  # nothing was typed
    with pytest.raises(TargetNotFound):
        eng.prompt(P3, "hi", session=someone_else)
    assert PROMPT not in wire.methods


def test_the_identity_re_check_passes_when_the_pane_still_holds_that_agent() -> None:
    eng, wire = build({GET: agent_frame()})
    eng.send_keys(P3, "1", session=SESS)
    assert wire.methods == [SNAPSHOT, GET, SEND_KEYS]


def test_the_identity_re_check_is_opt_in() -> None:
    """A caller that never learned the session is exactly as it was: address only.

    Refusing every sessionless write would break the module verbs and every
    caller written before this existed, so the check arms only when the caller
    can say who it meant.
    """
    eng, wire = build({GET: agent_frame()})
    eng.send_keys(P3, "1")
    assert SEND_KEYS in wire.methods


# --------------------------------------------------------------------------
# The lifecycle verbs
# --------------------------------------------------------------------------

PROCESS_INFO = client._method_of(wire_request.PaneProcessInfoRequest)


def process_info_frame(*, settled: bool = True) -> dict[str, Any]:
    """A pane whose only foreground process is its own shell -- or one still
    running something, which is exactly what makes ``agent.start`` fail with
    ``agent_pane_busy`` on 2 of 5 unguarded attempts."""
    running = [{"pid": 4242, "name": "bash", "argv": ["/bin/bash"]}]
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


def started_frame(session: str | None = None) -> dict[str, Any]:
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


def tab_created_frame() -> dict[str, Any]:
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
    """``tab.create`` carries two payload objects, which is why the lifecycle
    verbs cannot go through ``_result``: there is no single field to unwrap."""
    eng, wire = build({TAB_CREATE: tab_created_frame()})
    assert eng.create_tab(workspace_id="w1", cwd="/somewhere", label="x") == "w1:p9"
    assert wire.last.params["focus"] is False, (
        "creating an agent from a phone must not move the desktop's cursor"
    )


def test_a_create_is_never_re_sent_by_the_transport() -> None:
    """A re-sent create leaks a tab with a live shell in it that nothing tidies."""
    eng, wire = build({TAB_CREATE: tab_created_frame()})
    eng.create_tab(workspace_id="w1", cwd="/somewhere", label="x")
    assert wire.last.idempotent is False


def test_a_start_waits_for_the_shell_before_it_asks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The readiness gate: ``agent.start`` does not wait, so this does.

    The precondition check is instantaneous, not slow -- it fails in 0s with
    ``agent_pane_busy`` when the pane is not a shell at a prompt, which a pane
    created milliseconds ago frequently is not.
    """
    monkeypatch.setattr(client, "_SHELL_INTERVAL", 0)
    busy = iter([process_info_frame(settled=False), process_info_frame()])
    eng, wire = build({PROCESS_INFO: lambda _: next(busy), START: started_frame("s-1")})
    eng.start_agent(name="n", kind="claude", pane_id="w1:p9")
    assert wire.methods.count(PROCESS_INFO) == 2
    assert wire.methods[-1] == START, "the start went out after the pane settled"


def test_a_pane_that_never_settles_refuses_the_start_rather_than_hanging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(client, "_SHELL_INTERVAL", 0)
    monkeypatch.setattr(client, "_SHELL_POLLS", 3)
    eng, wire = build({PROCESS_INFO: process_info_frame(settled=False)})
    with pytest.raises(EngineError) as caught:
        eng.start_agent(name="n", kind="claude", pane_id="w1:p9")
    assert caught.value.code == client.PANE_BUSY_CODE
    assert START not in wire.methods


def test_a_busy_pane_is_the_one_write_this_client_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """And it is not a violation of the never-retry rule.

    ``agent_pane_busy`` is reported by an instantaneous precondition check
    *before* the runner does anything, so a refused start has provably typed
    nothing. Every other failure -- a timeout above all, which may mean the
    agent came up -- is raised on the first attempt.
    """
    monkeypatch.setattr(client, "_START_BACKOFF", 0)
    attempts: list[Any] = [
        EngineError("busy", code=client.PANE_BUSY_CODE),
        started_frame("s-1"),
    ]

    def answer(_: dict[str, Any]) -> dict[str, Any]:
        reply = attempts.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    eng, wire = build({PROCESS_INFO: process_info_frame(), START: answer})
    assert eng.start_agent(name="n", kind="claude", pane_id="w1:p9").session == "s-1"
    assert wire.methods.count(START) == 2


def test_every_other_start_failure_is_raised_on_the_first_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(client, "_START_BACKOFF", 0)
    eng, wire = build(
        {PROCESS_INFO: process_info_frame(), START: EngineTimeout("too slow")}
    )
    with pytest.raises(EngineTimeout):
        eng.start_agent(name="n", kind="claude", pane_id="w1:p9")
    assert wire.methods.count(START) == 1


def test_a_started_agent_with_no_session_says_so_as_the_empty_string() -> None:
    """The hard fact, at the seam where it is first knowable.

    ``None`` would be a value every caller can forget to check; the empty string
    is what the app's own ``SessionId`` doors already refuse.
    """
    eng, _ = build({PROCESS_INFO: process_info_frame(), START: started_frame(None)})
    assert eng.start_agent(name="n", kind="claude", pane_id="w1:p9").session == ""


def test_the_model_argument_reaches_the_runner_after_a_bare_dash_dash() -> None:
    eng, wire = build({PROCESS_INFO: process_info_frame(), START: started_frame("s")})
    eng.start_agent(name="n", kind="claude", pane_id="w1:p9", args=("--model", "haiku"))
    start = next(c for c in wire.calls if c.method == START)
    assert start.params["args"] == ["--model", "haiku"]
    assert start.params["timeout_ms"] > 3000, (
        "the runner rejects a start timeout at or below 3000ms"
    )


def test_a_close_re_checks_the_pane_is_still_held_by_that_session() -> None:
    """The recycled-pane-id bug at its worst: this is the one write that destroys.

    Same mechanism as the prompt's identity re-check, and the same round trip --
    ``agent.get`` already carries ``agent_session``, so comparing it is free.
    """
    eng, wire = build({GET: agent_frame()})
    someone_else = as_session_id("11111111-1111-4111-8111-111111111111")
    with pytest.raises(TargetNotFound) as caught:
        eng.close_pane(P3, session=someone_else)
    assert caught.value.code == client.SESSION_MOVED_CODE
    assert PANE_CLOSE not in wire.methods, "somebody else's pane was closed"

    eng.close_pane(P3, session=SESS)
    assert wire.methods[-1] == PANE_CLOSE


def test_a_close_is_never_re_sent_by_the_transport() -> None:
    """The guard that matters most here: a close retried after an ambiguous
    failure lands on whatever now holds the recycled id."""
    eng, wire = build({GET: agent_frame()})
    eng.close_pane(P3, session=SESS)
    assert wire.last.idempotent is False


def test_a_close_fails_closed_on_an_unexpected_protocol() -> None:
    eng, wire = build({GET: agent_frame()}, protocol_number=DRIFTED)
    with pytest.raises(ProtocolMismatch):
        eng.close_pane(P3, session=SESS)
    assert PANE_CLOSE not in wire.methods


# --------------------------------------------------------------------------
# The configuration boundary
# --------------------------------------------------------------------------


def test_a_bad_bind_host_no_longer_reaches_the_engine_at_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reported bug, now fixed one layer deeper than the first fix could.

    ``HP_BIND_HOST=0.0.0.0`` first reached ``client.snapshot()`` as a raw
    ``pydantic_core.ValidationError``; the settings layer caught it and re-raised it as an
    ``EngineMisconfigured``, which made it renderable but still let server
    configuration stop an engine verb. Now the un-injected constructor
    builds a ``settings.EngineSettings``, which has no ``bind_host`` to
    validate, so the bad value is not merely handled here -- it is unreachable.
    """
    monkeypatch.setenv("HP_BIND_HOST", "0.0.0.0")
    client.default_client.cache_clear()
    engine = client.HerdrClient()
    assert not hasattr(engine.settings, "bind_host")
    assert engine.settings.command_timeout > 0


def test_a_bad_engine_field_still_fails_typed_rather_than_as_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The error taxonomy is kept, exercised by a field the engine really reads.

    ``command_timeout`` is one of the eight, so a bad one is genuinely the
    engine's problem and must still arrive as an ``EngineMisconfigured`` rather
    than as a ``ValidationError`` from outside the ``EngineError`` taxonomy.
    """
    monkeypatch.setenv("HP_COMMAND_TIMEOUT", "0")
    client.default_client.cache_clear()
    with pytest.raises(EngineMisconfigured) as caught:
        client.snapshot()
    assert caught.value.code == "engine_misconfigured"
    assert "command_timeout" in str(caught.value)
    assert isinstance(caught.value, EngineError)


def test_the_typed_configuration_failure_repeats_rather_than_being_memoized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``@cache`` does not memoize exceptions, so every call re-raises.

    That is the behaviour to keep -- what was wrong was the *type*, not the
    repetition: an operator who fixes the variable and restarts gets a working
    process, and one who does not gets the same typed refusal every time
    rather than one 500 and then a corrupt cache entry.
    """
    monkeypatch.setenv("HP_COMMAND_TIMEOUT", "0")
    client.default_client.cache_clear()
    for _ in range(3):
        with pytest.raises(EngineMisconfigured):
            client.agents()


def test_an_injected_settings_object_is_never_second_guessed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing builds a ``Settings`` when one was handed in -- not even to check.

    So a client the server built, or a test built, keeps working while the
    environment holds a bind host that would refuse to validate. Now
    the un-injected path would survive that host too; this asserts the older,
    narrower promise, which is that the injected object is used verbatim.
    """
    settings = Isolated()
    monkeypatch.setenv("HP_BIND_HOST", "0.0.0.0")
    eng, wire = build(settings=settings)
    assert eng.snapshot().protocol == protocol.EXPECTED_PROTOCOL
    assert wire.last.method == SNAPSHOT


def test_the_engine_declares_the_eight_fields_it_reads_and_no_more() -> None:
    """``EngineSettings`` is the seam the fix landed on, and the honest dependency.

    The protocol says what the engine needs; ``settings.EngineSettings`` is the
    mixin that supplies exactly that and is built alone by the un-injected
    constructor. tests/test_settings.py holds the two together.
    """
    engine_reads = {
        name for name in dir(client.EngineSettings) if not name.startswith("_")
    }
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
    """That is the whole point of them: a subscription no roster can stale.

    Measured on herdr 0.8.0 against panes in w4 and w654c7902e43791 -- neither
    the workspace the subscriber sits in -- so the delivery really is herd-wide
    and not the one-workspace traffic first observed.
    """
    subs = client.discovery_subscriptions()
    assert [one["type"] for one in subs] == [
        "pane.created",
        "pane.agent_detected",
        "pane.closed",
    ]
    assert not any("pane_id" in one for one in subs)


def test_one_connection_carries_both_the_roster_and_the_discovery_events() -> None:
    """No third connection: `subscriptions` is a list and both kinds ride it.

    The runner hangs up on a second request to a subscribed connection (~100ms,
    measured twice), so anything the stream is to hear has to be named in the
    one subscribe frame. Both kinds were seen arriving on a single connection.
    """
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
    assert eng.watch_statuses([]).subscriptions == client.discovery_subscriptions()
