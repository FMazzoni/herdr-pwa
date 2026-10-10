"""The generated wire models, checked against herdr's own recorded output."""

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

from herdr_pwa.engine import generated, model
from herdr_pwa.engine.generated import request
from herdr_pwa.engine.generated import success_response as wire
from herdr_pwa.engine.generated.event import EventKind
from herdr_pwa.jsontypes import JsonObject, JsonValue, as_object, loads

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
SCHEMA_PATH = FIXTURES / "herdr-api-schema.json"
GENERATOR = ROOT / "scripts" / "generate_models.py"


def _obj(value: object) -> JsonObject:
    found = as_object(value)
    assert found is not None
    return found


def _capture(name: str) -> JsonValue:
    return loads((FIXTURES / name).read_text(encoding="utf-8"))


SCHEMA_DOC = _obj(loads(SCHEMA_PATH.read_text(encoding="utf-8")))


# The vendored contract itself


def test_vendored_schema_is_the_protocol_this_build_expects() -> None:
    """The drift signal."""
    assert SCHEMA_DOC["protocol"] == model.EXPECTED_PROTOCOL == 22
    assert SCHEMA_DOC["schema_version"] == 1


def test_generated_package_records_the_schema_it_came_from() -> None:
    assert SCHEMA_DOC["protocol"] == generated.SCHEMA_PROTOCOL
    assert SCHEMA_DOC["schema_version"] == generated.SCHEMA_VERSION


def test_vendored_schema_still_has_the_five_sub_schemas() -> None:
    """Codegen splits on these keys; a rename upstream breaks the generator."""
    assert set(_obj(SCHEMA_DOC["schemas"])) == {
        "request",
        "success_response",
        "event",
        "subscription_event",
        "error_response",
    }


# The important one: do the generated types read herdr's real output?

CAPTURES = {
    "snapshot.example.json": wire.SessionSnapshotResponseResult,
    "agent-list.example.json": wire.AgentListResponseResult,
}


@pytest.mark.parametrize(("name", "variant"), CAPTURES.items())
def test_generated_models_parse_the_real_captures(name: str, variant: type) -> None:
    """Every recorded herdr response validates, and resolves to the right variant."""
    payload = _capture(name)
    response = wire.SuccessResponse.model_validate(payload)
    assert isinstance(response.result.root, variant)


@pytest.mark.parametrize("name", CAPTURES)
def test_captures_round_trip_without_loss(name: str) -> None:
    """Parse -> dump must reproduce the capture exactly."""
    payload = _capture(name)
    response = wire.SuccessResponse.model_validate(payload)
    assert response.model_dump(mode="json", exclude_none=True) == payload


def test_snapshot_capture_agrees_with_the_handwritten_model() -> None:
    """The generated read and the hand-written read see the same herd."""
    payload = _obj(_capture("snapshot.example.json"))
    parsed = wire.SuccessResponse.model_validate(payload).result.root
    assert isinstance(parsed, wire.SessionSnapshotResponseResult)
    hand = model.Snapshot.from_raw(_obj(_obj(payload["result"])["snapshot"]))

    assert hand.protocol == parsed.snapshot.protocol
    assert hand.version == parsed.snapshot.version
    assert len(hand.agents) == len(parsed.snapshot.agents)
    assert {a.target for a in hand.agents} == {a.pane_id for a in parsed.snapshot.agents}
    assert {w.workspace_id for w in hand.workspaces} == {
        w.workspace_id for w in parsed.snapshot.workspaces
    }


def test_generated_agent_status_matches_the_handwritten_enum() -> None:
    assert [s.value for s in wire.AgentStatus] == [s.value for s in model.AgentStatus]


def test_event_kinds_the_push_path_will_subscribe_to_exist() -> None:
    """Notifications are driven off these two; a rename must fail loudly."""
    kinds = {k.value for k in EventKind}
    assert {"pane_agent_status_changed", "pane_output_changed"} <= kinds


# What herdr exposes that we deliberately do not model

# Wire fields Agent.from_raw actually reads.
AGENT_FIELDS_MODELLED = {
    "agent",
    "agent_session",
    "agent_status",
    "cwd",
    "focused",
    "foreground_cwd",
    "pane_id",
    "revision",
    "name",
    "state_change_seq",
    "tab_id",
    "terminal_title",
    "terminal_title_stripped",
    "title",
    "workspace_id",
}

# Fields we knowingly drop; adopting one moves it across the two sets in the same commit.
AGENT_FIELDS_IGNORED = {
    "display_agent",  # pretty provider name
    "interactive_ready",  # the send path reads it off the wire model, not this one
    "launch_pending",  # likewise
    "screen_detection_skipped",
    "state_labels",  # detection-rule labels behind agent_status
    "terminal_id",  # pane_id is the address we use
    "tokens",  # usage counters, capped at 32 keys by the schema
}

SNAPSHOT_FIELDS_MODELLED = {
    "agents",
    "focused_pane_id",
    "focused_tab_id",
    "focused_workspace_id",
    "panes",
    "protocol",
    "version",
    "workspaces",
}

# A phone renders a feed, not a window manager, so the terminal geometry is not modelled.
SNAPSHOT_FIELDS_IGNORED = {"layouts", "tabs"}


def test_every_agent_field_is_either_modelled_or_knowingly_ignored() -> None:
    """Fails when herdr adds, removes or renames a field on ``AgentInfo``."""
    assert set(wire.AgentInfo.model_fields) == (AGENT_FIELDS_MODELLED | AGENT_FIELDS_IGNORED)
    assert not (AGENT_FIELDS_MODELLED & AGENT_FIELDS_IGNORED)


def test_every_snapshot_field_is_either_modelled_or_knowingly_ignored() -> None:
    assert set(wire.SessionSnapshot.model_fields) == (
        SNAPSHOT_FIELDS_MODELLED | SNAPSHOT_FIELDS_IGNORED
    )


def test_the_request_surface_stays_out_of_the_package_facade() -> None:
    """~90 command types must not be one dotted name away from a template."""
    surface = set(dir(generated))
    request_types = {n for n in dir(request) if n.endswith("Request")}
    leaked = surface & request_types
    assert not leaked, f"request types reachable on the facade: {sorted(leaked)}"
    assert not {n for n in surface if n.endswith("Request")}
    assert "Request" not in set(generated.__all__)


# Staleness


def test_generated_files_record_the_vendored_schema_digest() -> None:
    """Cheap staleness signal: no codegen run needed."""
    digest = hashlib.sha256(SCHEMA_PATH.read_bytes()).hexdigest()
    assert digest == generated.SCHEMA_SHA256
    for module in (ROOT / "src" / "herdr_pwa" / "engine" / "generated").glob("*.py"):
        if module.name == "__init__.py":
            continue
        assert digest in module.read_text(encoding="utf-8"), module.name


def test_committed_generated_code_is_not_stale() -> None:
    """Regenerate into a temp dir and diff."""
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(GENERATOR), "--check"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )
    assert result.returncode == 0, (
        "committed models differ from what the vendored schema generates.\n"
        "Run: uv run python scripts/generate_models.py\n\n"
        f"{result.stdout}\n{result.stderr}"
    )
