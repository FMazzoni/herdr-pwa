"""Every state file in the shape the previous build wrote still loads, and is written back the same.

The documents below copy the key sets and value types of a live state directory (taken read-only,
2026-10-15); the values themselves are made up, and the VAPID pair is derived from a fixed scalar.
"""

import base64
import json
import stat
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from pydantic import JsonValue, TypeAdapter

from herdr_pwa import audit, devreport, push
from herdr_pwa.clock import WallTime
from herdr_pwa.ids import SessionId
from herdr_pwa.notify_state import Interrupts, InterruptStore, SeenLedger, StatusLedger
from tests.fakes import Clock

NOW = 1_792_000_000.0
SESSION = "5e550001-0000-0000-0000-000000000000"
OBJECT: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])


def b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def written(path: Path, document: object) -> Path:
    """The file as the previous build wrote it: indented JSON, a newline, 0600 in a 0700 dir."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _ = path.write_text(json.dumps(document, indent=2) + "\n")
    path.chmod(0o600)
    return path


def keys_of(path: Path) -> dict[str, str]:
    """Each top-level key with its JSON type, so a rewrite can be compared with what was there."""
    return {
        key: type(value).__name__ for key, value in OBJECT.validate_json(path.read_text()).items()
    }


def vapid_document() -> dict[str, object]:
    private = ec.derive_private_key(0x2B7E151628AED2A6ABF7158809CF4F3C, ec.SECP256R1())
    public = private.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    return {
        "version": 1,
        "created_at": 1760058000,
        "curve": "P-256",
        "public_key": b64u(public),
        "private_key": b64u(private.private_numbers().private_value.to_bytes(32, "big")),
    }


def subscription_entry(endpoint: str = "https://web.push.apple.com/QAbc-123") -> dict[str, object]:
    point = bytes([0x04]) + bytes(range(1, 65))
    return {
        "endpoint": endpoint,
        "keys": {"p256dh": b64u(point), "auth": b64u(bytes(range(16)))},
        "expirationTime": None,
        "added_at": 1792009470,
    }


def test_the_vapid_file_loads_and_is_never_rewritten(tmp_path: Path) -> None:
    document = vapid_document()
    path = written(tmp_path / push.VAPID_FILENAME, document)
    before = path.read_text()
    keys = push.load_or_create_vapid_keys(tmp_path)
    assert (keys.public_key, keys.private_key) == (document["public_key"], document["private_key"])
    assert path.read_text() == before


def test_a_minted_vapid_file_has_the_keys_the_previous_build_wrote(tmp_path: Path) -> None:
    keys = push.load_or_create_vapid_keys(tmp_path / "state", clock=lambda: WallTime(NOW))
    assert keys_of(keys.path) == keys_of(written(tmp_path / "old.json", vapid_document()))
    assert stat.S_IMODE(keys.path.stat().st_mode) == 0o600


def test_the_subscription_store_loads_and_round_trips_its_shape(tmp_path: Path) -> None:
    entry = subscription_entry()
    path = written(tmp_path / push.SUBSCRIPTIONS_FILENAME, {"version": 1, "subscriptions": [entry]})
    store = push.SubscriptionStore(tmp_path, clock=Clock(NOW))
    [device] = store.list_all()
    assert device.push_info() == {"endpoint": entry["endpoint"], "keys": entry["keys"]}
    assert (device.added_at, device.expiration_time) == (1792009470, None)
    _ = store.add(
        push.Subscription.model_validate(
            subscription_entry("https://fcm.googleapis.com/fcm/send/x")
        )
    )
    stored = OBJECT.validate_json(path.read_text())
    assert stored["version"] == 1
    assert stored["subscriptions"] == [
        entry,
        {**subscription_entry("https://fcm.googleapis.com/fcm/send/x"), "added_at": int(NOW)},
    ]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_the_delivery_health_loads_and_round_trips_its_shape(tmp_path: Path) -> None:
    document = {
        "version": 1,
        "sent": 41,
        "failed": 2,
        "pruned": 1,
        "quieted": 7,
        "last_send_at": 1792008000.125,
        "last_error": "boom",
        "last_error_at": 1792007000.5,
        "last_quiet_at": 1792006000.25,
        "updated_at": 1792008000.125,
    }
    path = written(tmp_path / push.HEALTH_FILENAME, document)
    log = push.DeliveryLog(tmp_path, clock=Clock(NOW + 0.00049))
    record = log.load()
    assert record is not None
    assert (record.sent, record.failed, record.last_error, record.never_delivered) == (
        41,
        2,
        "boom",
        False,
    )
    log.sent()
    assert OBJECT.validate_json(path.read_text()) == {
        **document,
        "sent": 42,
        "last_send_at": NOW,
        "updated_at": NOW,
    }
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    ("content", "sent"),
    [
        ("{torn", None),
        ("[]", None),
        (json.dumps({"version": 2, "sent": 1}), None),
        (json.dumps({"sent": 1}), None),
        (json.dumps({"version": 1, "sent": "many", "failed": 3}), 0),
        (json.dumps({"version": 1, "sent": True, "failed": 3}), 0),
    ],
)
def test_damaged_health_reads_as_none_or_costs_only_the_field(
    tmp_path: Path, content: str, sent: int | None
) -> None:
    _ = (tmp_path / push.HEALTH_FILENAME).write_text(content)
    record = push.DeliveryLog(tmp_path).load()
    assert (None if record is None else record.sent) == sent
    if record is not None:
        assert record.failed == 3


def test_the_status_ledger_loads_and_round_trips_its_shape(tmp_path: Path) -> None:
    document = {"version": 1, "updated_at": NOW - 60.5, "statuses": {SESSION: "working"}}
    path = written(tmp_path / "notify.json", document)
    ledger = StatusLedger(tmp_path, clock=Clock(NOW))
    assert ledger.load() == {SESSION: "working"}
    ledger.save({SESSION: "blocked"})
    assert OBJECT.validate_json(path.read_text()) == {
        "version": 1,
        "updated_at": NOW,
        "statuses": {SESSION: "blocked"},
    }


def test_a_status_that_is_not_text_is_dropped_and_the_rest_kept(tmp_path: Path) -> None:
    statuses = {"a": "idle", "b": 3, "c": None}
    _ = written(tmp_path / "notify.json", {"version": 1, "updated_at": NOW, "statuses": statuses})
    assert StatusLedger(tmp_path, clock=Clock(NOW)).load() == {"a": "idle"}


def test_the_seen_ledger_loads_and_round_trips_its_shape(tmp_path: Path) -> None:
    record = {"seq": 14, "active_at": NOW - 30.25, "seen_at": NOW - 90.5}
    path = written(tmp_path / "seen.json", {"version": 1, "seen": {SESSION: record}})
    seen = SeenLedger(tmp_path, clock=Clock(NOW))
    assert seen.unread_sessions() == {SESSION}
    seen.mark_seen(SessionId(SESSION))
    seen.flush()
    assert OBJECT.validate_json(path.read_text()) == {
        "version": 1,
        "seen": {SESSION: {**record, "seen_at": NOW}},
    }


def test_a_seen_record_missing_a_number_is_dropped_and_the_rest_kept(tmp_path: Path) -> None:
    good = {"seq": 1, "active_at": NOW, "seen_at": NOW - 1}
    bad = {"seq": 1.5, "active_at": NOW, "seen_at": NOW}
    _ = written(tmp_path / "seen.json", {"version": 1, "seen": {"good": good, "bad": bad}})
    assert SeenLedger(tmp_path, clock=Clock(NOW)).unread_sessions() == {"good"}


def test_the_preferences_load_and_round_trip_their_shape(tmp_path: Path) -> None:
    body = {"blocked": True, "done": True, "dnd_until": 0.0}
    path = written(tmp_path / "interrupts.json", {"version": 1, "interrupts": body})
    store = InterruptStore(tmp_path, clock=Clock(NOW))
    assert store.load() == Interrupts(blocked=True, done=True)
    _ = store.snooze(60)
    assert OBJECT.validate_json(path.read_text()) == {
        "version": 1,
        "interrupts": {**body, "dnd_until": NOW + 60},
    }


@pytest.mark.parametrize(
    ("interrupts", "expected"),
    [
        ({"blocked": "no", "done": 1, "dnd_until": -5}, Interrupts()),
        ({"blocked": False, "dnd_until": "soon"}, Interrupts(blocked=False)),
        ("nonsense", Interrupts()),
    ],
)
def test_a_malformed_preference_takes_its_default_and_keeps_the_rest(
    tmp_path: Path, interrupts: object, expected: Interrupts
) -> None:
    _ = written(tmp_path / "interrupts.json", {"version": 1, "interrupts": interrupts})
    assert InterruptStore(tmp_path).load() == expected


def test_the_device_report_reads_lines_the_previous_build_wrote(tmp_path: Path) -> None:
    lines = [
        {
            "received": "2026-10-10T09:59:00.000+00:00",
            "session": "k3x-9",
            "page": 1,
            "seq": 1,
            "events": [
                {"t": 0, "page": 1, "kind": "load", "url": "/", "historyLength": 1},
                {"t": 12, "page": 1, "kind": "resize", "source": "vv", "viewport": {"w": 390}},
            ],
        },
        "{torn",
        {
            "received": "2026-10-10T10:00:00.000+00:00",
            "session": "k3x-9",
            "page": 2,
            "seq": 2,
            "events": [{"t": 3.5, "page": 2, "kind": "visibility", "state": "hidden"}],
        },
    ]
    text = "".join((line if isinstance(line, str) else json.dumps(line)) + "\n" for line in lines)
    _ = (tmp_path / devreport.REPORT_FILENAME).write_text(text)
    assert devreport.render(tmp_path).splitlines() == [
        "session k3x-9: 3 event(s)",
        '        0ms  p1  load         {"url": "/", "historyLength": 1}',
        '       12ms  p1  resize       {"source": "vv", "viewport": {"w": 390}}',
        '        4ms  p2  visibility   {"state": "hidden"}',
    ]


def test_a_report_with_malformed_events_still_reads_every_line(tmp_path: Path) -> None:
    """A non-object event is shown as it was, a wrong-shaped field reads as ``?``."""
    lines: list[JsonValue] = [
        {
            "session": "k3x-9",
            "events": [
                7,
                "x",
                [1],
                None,
                {"t": "soon", "page": 1.5, "kind": 3, "extra": True},
                {"t": 2, "kind": "load", "seen": {"a": 1}},
            ],
        },
        {"session": "k3x-9", "events": "not a list"},
        {"session": "k3x-9", "events": [{"kind": "after"}]},
    ]
    text = "".join(json.dumps(line) + "\n" for line in lines)
    _ = (tmp_path / devreport.REPORT_FILENAME).write_text(text)
    assert devreport.render(tmp_path).splitlines() == [
        "session k3x-9: 7 event(s)",
        "? 7",
        "? 'x'",
        "? [1]",
        "? None",
        '?  p?  ?            {"extra": true}',
        '        2ms  p?  load         {"seen": {"a": 1}}',
        "?  p?  after        {}",
    ]


def test_an_appended_report_line_keeps_everything_the_page_sent(tmp_path: Path) -> None:
    store = devreport.ReportStore(tmp_path, clock=lambda: "2026-10-10T10:00:00.000+00:00")
    posted = {"session": "k3x-9", "page": 1, "seq": 1, "events": [{"t": 0, "kind": "load"}]}
    store.append(devreport.Batch.model_validate(posted))
    line = OBJECT.validate_json(store.path.read_text())
    assert line == {**posted, "received": "2026-10-10T10:00:00.000+00:00"}
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600


def test_the_audit_log_reads_lines_the_previous_build_wrote(tmp_path: Path) -> None:
    attempt = {
        "v": 1,
        "phase": "attempt",
        "id": "9f2c4e1ab0d37c55",
        "ts": "2026-10-15T21:51:02.118000+00:00",
        "endpoint": "prompt",
        "target": "w1:p3",
        "session": SESSION,
        "actor": "you@example.com",
        "actor_via": "tailscale",
        "client_ip": "127.0.0.1",
        "forwarded_for": "198.51.100.7",
        "payload": "go on",
        "payload_chars": 5,
        "payload_truncated": False,
        "payload_omitted": "",
    }
    outcome = {
        "v": 1,
        "phase": "outcome",
        "id": "9f2c4e1ab0d37c55",
        "ts": "2026-10-15T21:51:02.402000+00:00",
        "endpoint": "prompt",
        "target": "w1:p3",
        "result": "sent",
    }
    text = "".join(
        json.dumps(one, ensure_ascii=False, separators=(",", ":")) + "\n"
        for one in (attempt, outcome)
    )
    _ = (tmp_path / audit.AUDIT_FILENAME).write_text(text + "{torn\n")
    found = audit.records(tmp_path)
    assert found == [attempt, outcome, {"phase": "unreadable", "ts": "", "at": "audit.log:3"}]
    assert [audit.describe(one) for one in found] == [
        (
            "2026-10-15T21:51:02  prompt  w1:p3        you@example.com via tailscale from "
            '198.51.100.7  "go on"'
        ),
        "2026-10-15T21:51:02  prompt  w1:p3        -> sent",
        "  UNREADABLE RECORD at audit.log:3",
    ]


def test_a_written_audit_line_is_byte_for_byte_the_previous_format(tmp_path: Path) -> None:
    log = audit.AuditLog(tmp_path, max_bytes=1 << 20, payload_chars=512)
    origin = audit.Origin("you@example.com", "tailscale", "127.0.0.1", "198.51.100.7")
    with log.record(origin, audit.Write(audit.PROMPT, target="w1:p3", payload="ça va")) as entry:
        entry.outcome("sent")
    for line, record in zip(
        (tmp_path / audit.AUDIT_FILENAME).read_text(encoding="utf-8").splitlines(),
        audit.records(tmp_path),
        strict=True,
    ):
        assert line == json.dumps(record, ensure_ascii=False, separators=(",", ":"))


def test_the_audit_log_reads_old_records_and_new_ones_side_by_side(tmp_path: Path) -> None:
    # Older builds kept a prompt's text on its attempt; newer ones keep it on an echoed outcome.
    old = audit.AuditLog(tmp_path, max_bytes=1 << 20, payload_chars=512)
    origin = audit.Origin("you@example.com", "tailscale", "127.0.0.1", "198.51.100.7")
    old_attempt = {
        "v": 1,
        "phase": "attempt",
        "id": "0123456789abcdef",
        "ts": "2026-10-15T21:51:02.118000+00:00",
        "endpoint": "prompt",
        "target": "w1:p3",
        "session": SESSION,
        "actor": "you@example.com",
        "actor_via": "tailscale",
        "client_ip": "127.0.0.1",
        "forwarded_for": "",
        "payload": "go on",
        "payload_chars": 5,
        "payload_truncated": False,
        "payload_omitted": "",
    }
    old_outcome = {
        "v": 1,
        "phase": "outcome",
        "id": "0123456789abcdef",
        "ts": "2026-10-15T21:51:02.402000+00:00",
        "endpoint": "prompt",
        "target": "w1:p3",
        "result": "accepted:noecho",
    }
    _ = (tmp_path / audit.AUDIT_FILENAME).write_text(
        "".join(json.dumps(one) + "\n" for one in (old_attempt, old_outcome))
    )
    with old.record(origin, audit.Write(audit.PROMPT, "w1:p3", payload="carry on")) as entry:
        entry.shown()
        entry.outcome("confirmed:echo")
    with old.record(origin, audit.Write(audit.PROMPT, "w1:p3", payload="hunter2")) as entry:
        entry.outcome("accepted:noecho")
    found = audit.records(tmp_path)
    assert [one["phase"] for one in found] == ["attempt", "outcome"] * 3
    shown = [audit.describe(one) for one in found]
    assert shown[0].endswith('"go on"')
    assert shown[1].endswith("-> accepted:noecho")
    assert shown[2].endswith("<8 chars, text kept only once echoed>")
    assert shown[3].endswith('-> confirmed:echo  "carry on"')
    assert shown[5].endswith("-> accepted:noecho")
    assert "hunter2" not in "\n".join(shown)
    assert "hunter2" not in (tmp_path / audit.AUDIT_FILENAME).read_text()
