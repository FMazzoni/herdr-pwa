import base64
import json
import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest
import pywebpush
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from pywebpush import WebPushException

from herdr_pwa import jsonio, push
from herdr_pwa.push import (
    EVENT_BLOCKED,
    EVENT_CLEAR,
    EVENT_DONE,
    LINK_PLACEHOLDER,
    MAX_BODY_CHARS,
    MAX_PAYLOAD_BYTES,
    DeliveryError,
    DeliveryLog,
    Notice,
    PushError,
    SubjectError,
    Subscription,
    SubscriptionError,
    SubscriptionStore,
    VapidKeyError,
    WebPushSender,
    arm_sender,
    build_clear,
    build_payload,
    check_subject,
    encode_payload,
    fan_out,
    load_or_create_vapid_keys,
    sanitise,
    subject_problem,
)
from herdr_pwa.settings import Settings
from tests.fakes import Clock

ENDPOINT = "https://push.example.invalid/one"
SUBJECT = "mailto:someone@herdr.example.org"


def b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def raw_subscription(
    endpoint: str = ENDPOINT, *, auth: bytes = bytes(range(16))
) -> dict[str, object]:
    point = bytes([0x04]) + bytes(range(64))
    return {"endpoint": endpoint, "keys": {"p256dh": b64u(point), "auth": b64u(auth)}}


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_the_key_is_created_once_at_0600_in_a_0700_dir_and_reused(tmp_path: Path) -> None:
    state = tmp_path / "state"
    first = load_or_create_vapid_keys(state)
    text = first.path.read_text()
    assert (mode(state), mode(first.path)) == (0o700, 0o600)
    for _ in range(3):
        assert load_or_create_vapid_keys(state) == first
    assert first.path.read_text() == text


def test_the_public_key_is_an_uncompressed_point_matching_the_private_half(tmp_path: Path) -> None:
    keys = load_or_create_vapid_keys(tmp_path)
    public = base64.urlsafe_b64decode(keys.public_key + "==")
    assert (len(public), public[0]) == (65, 0x04)
    assert len(base64.urlsafe_b64decode(keys.private_key + "=")) == 32
    assert "=" not in keys.public_key


def test_modes_hold_under_a_permissive_umask(tmp_path: Path) -> None:
    old = os.umask(0)
    try:
        keys = load_or_create_vapid_keys(tmp_path / "s")
    finally:
        _ = os.umask(old)
    assert mode(keys.path) == 0o600


def test_a_corrupt_key_file_fails_loudly_and_is_never_replaced(tmp_path: Path) -> None:
    path = tmp_path / push.VAPID_FILENAME
    _ = path.write_text("{not json")
    with pytest.raises(VapidKeyError, match="orphans every subscribed device"):
        _ = load_or_create_vapid_keys(tmp_path)
    assert path.read_text() == "{not json"


def test_mismatched_halves_fail_rather_than_pick_one(tmp_path: Path) -> None:
    keys = load_or_create_vapid_keys(tmp_path / "a")
    other = load_or_create_vapid_keys(tmp_path / "b")
    document = {"public_key": other.public_key, "private_key": keys.private_key}
    _ = (tmp_path / push.VAPID_FILENAME).write_text(json.dumps(document))
    with pytest.raises(VapidKeyError, match="derives"):
        _ = load_or_create_vapid_keys(tmp_path)


def test_a_key_file_missing_a_half_fails(tmp_path: Path) -> None:
    _ = (tmp_path / push.VAPID_FILENAME).write_text(json.dumps({"public_key": "AAAA"}))
    with pytest.raises(VapidKeyError):
        _ = load_or_create_vapid_keys(tmp_path)


def test_repr_never_leaks_the_private_key(tmp_path: Path) -> None:
    keys = load_or_create_vapid_keys(tmp_path)
    assert keys.private_key not in repr(keys)


@pytest.mark.parametrize(
    "subject",
    [
        SUBJECT,
        "mailto:first.last+push@mail.co.uk",
        "https://herdr.example.org/contact",
        f"  {SUBJECT} ",
    ],
)
def test_a_contact_somebody_could_answer_is_accepted(subject: str) -> None:
    assert subject_problem(subject) == ""
    assert check_subject(subject) == subject.strip()


@pytest.mark.parametrize(
    "subject",
    [
        "mailto:push@herdr-pwa.invalid",
        "mailto:push@herdr-pwa.INVALID",
        "mailto:me@somewhere.example",
        "mailto:me@somewhere.test",
        "mailto:me@box.local",
        "mailto:me@localhost",
        "https://herdr-pwa.invalid/contact",
    ],
)
def test_a_reserved_name_is_refused_because_apple_rejects_it(subject: str) -> None:
    assert "reserved" in subject_problem(subject)


@pytest.mark.parametrize(
    "subject", ["", "  ", "push@herdr.example.org", "tel:+1555", "mailto:", "mailto:x"]
)
def test_no_contact_or_no_host_is_refused(subject: str) -> None:
    assert subject_problem(subject)
    with pytest.raises(SubjectError):
        _ = check_subject(subject)


def test_push_will_not_arm_without_a_real_contact(tmp_path: Path) -> None:
    keys = load_or_create_vapid_keys(tmp_path)
    with pytest.raises(SubjectError):
        _ = arm_sender(keys, subject="", ttl=900)
    assert arm_sender(keys, subject=SUBJECT, ttl=900).subject == SUBJECT


def test_settings_have_no_default_contact() -> None:
    assert Settings().push_subject == ""


@pytest.fixture
def store(tmp_path: Path) -> SubscriptionStore:
    return SubscriptionStore(tmp_path / "state", clock=Clock())


def test_the_store_starts_empty_and_round_trips_at_0600(store: SubscriptionStore) -> None:
    assert store.list_all() == []
    added = store.add(raw_subscription())
    assert store.list_all() == [added]
    assert mode(store.path) == 0o600


def test_resubscribing_a_device_updates_its_keys_and_keeps_its_stamp(tmp_path: Path) -> None:
    clock = Clock()
    store = SubscriptionStore(tmp_path, clock=clock)
    first = store.add(raw_subscription())
    clock.advance(60)
    second = store.add(raw_subscription(auth=bytes(16)))
    assert len(store.list_all()) == 1
    assert second.added_at == first.added_at
    assert second.auth != first.auth


def test_remove_forgets_one_device_and_is_idempotent(store: SubscriptionStore) -> None:
    _ = store.add(raw_subscription())
    _ = store.add(raw_subscription("https://push.example.invalid/two"))
    assert store.remove(ENDPOINT)
    assert not store.remove(ENDPOINT)
    assert [one.endpoint for one in store.list_all()] == ["https://push.example.invalid/two"]


@pytest.mark.parametrize(
    "raw",
    [
        None,
        [],
        {"endpoint": "http://push.example.invalid/x", "keys": {}},
        {"endpoint": ENDPOINT},
        {"endpoint": ENDPOINT, "keys": {"p256dh": "AAAA", "auth": "AAAA"}},
        {
            **raw_subscription(),
            "keys": {"p256dh": b64u(bytes([2]) + bytes(64)), "auth": b64u(bytes(16))},
        },
        raw_subscription("https://push.example.invalid/" + "x" * 2100),
        {**raw_subscription(), "expirationTime": "soon"},
    ],
)
def test_a_malformed_subscription_is_refused(store: SubscriptionStore, raw: object) -> None:
    with pytest.raises(SubscriptionError):
        _ = store.add(raw)


def test_one_bad_record_does_not_hide_the_good_ones(store: SubscriptionStore) -> None:
    good = store.add(raw_subscription())
    entries = jsonio.as_list(jsonio.child(jsonio.loads(store.path.read_text()), "subscriptions"))
    assert entries is not None
    entries.insert(0, {"endpoint": "nope"})
    _ = store.path.write_text(json.dumps({"version": 1, "subscriptions": entries}))
    assert store.list_all() == [good]


@pytest.mark.parametrize("content", ["{nope", "[]", '{"subscriptions": 3}'])
def test_a_corrupt_store_is_loud(store: SubscriptionStore, content: str) -> None:
    _ = store.path.write_text(content)
    with pytest.raises(SubscriptionError):
        _ = store.list_all()


def test_a_failed_write_keeps_the_previous_store(store: SubscriptionStore) -> None:
    _ = store.add(raw_subscription())
    before = store.path.read_text()
    # A directory where the temporary file must go makes the write itself fail.
    store.path.with_name(store.path.name + ".tmp").mkdir()
    with pytest.raises(SubscriptionError):
        _ = store.add(raw_subscription("https://push.example.invalid/two"))
    assert store.path.read_text() == before


def test_ansi_osc_controls_bidi_and_zero_width_are_stripped() -> None:
    text = "\x1b]0;title\x07\x1b[31mred\x1b[0m\u202eevil\u200bx\x00y\nz"
    assert sanitise(text, limit=100) == "red evil x y z"


@pytest.mark.parametrize(
    "text",
    [
        "go to https://evil.example/pay now",
        "open http://10.0.0.1:8080/x",
        "see www.evil.example",
        "mail mailto:a@evil.example",
        "visit evil.example.com",
        "fetch evil.invalid/payload.sh",
        "data:text/html,<script>x</script>",
        "javascript:alert(1)",
        "https\x1b[m://evil.example/x",
    ],
)
def test_urls_never_reach_the_lock_screen(text: str) -> None:
    cleaned = sanitise(text, limit=MAX_BODY_CHARS)
    assert LINK_PLACEHOLDER in cleaned
    for marker in ("://", "www.", "mailto:", ".com", ".invalid", "javascript:"):
        assert marker not in cleaned


@pytest.mark.parametrize(
    "text", ["Should I edit main.py?", "pyproject.toml has no key", "Version 1.2.3?"]
)
def test_ordinary_questions_are_not_mangled(text: str) -> None:
    assert sanitise(text, limit=MAX_BODY_CHARS) == text


def test_the_title_and_body_are_capped() -> None:
    payload = build_payload(Notice(event=EVENT_BLOCKED, title="t" * 200, body="b" * 500), now=0)
    assert len(str(payload["title"])) == 60
    assert len(str(payload["body"])) == 180
    assert str(payload["body"]).endswith("…")


def test_a_body_that_was_only_a_link_falls_back() -> None:
    notice = Notice(event=EVENT_DONE, title="x", body="https://evil.example", fallback="herdr")
    assert build_payload(notice, now=0)["body"] == "herdr"


def test_only_blocked_and_done_are_notifications_and_clear_raises_nothing() -> None:
    with pytest.raises(PushError):
        _ = build_payload(Notice(event=EVENT_CLEAR, title="", body=""), now=0)
    clear = build_clear(now=5)
    assert (clear["event"], clear["tag"], "title" in clear) == (EVENT_CLEAR, "herd", False)


def test_the_encoded_payload_is_compact_and_always_fits() -> None:
    notice = Notice(event=EVENT_BLOCKED, title="ü" * 200, body="ü" * 5000, agent="ü" * 200)
    encoded = encode_payload(build_payload(notice, now=0))
    assert ", " not in encoded
    assert len(encoded.encode()) <= MAX_PAYLOAD_BYTES
    with pytest.raises(PushError):
        _ = encode_payload({"x": "y" * 5000})


def test_pywebpush_signs_and_encrypts_with_this_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # curl=True builds the signed, encrypted request without sending; it writes into the cwd.
    monkeypatch.chdir(tmp_path)
    keys = load_or_create_vapid_keys(tmp_path)
    point = (
        ec.generate_private_key(ec.SECP256R1())
        .public_key()
        .public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    )
    device = {"endpoint": ENDPOINT, "keys": {"p256dh": b64u(point), "auth": b64u(bytes(16))}}
    subscription = Subscription.parse(device)
    data = encode_payload(
        build_payload(Notice(event=EVENT_BLOCKED, title="t", body="secret"), now=0)
    )
    # pywebpush leaves its subscription mapping untyped.
    sign = cast("Callable[..., object]", pywebpush.webpush)
    request = sign(
        subscription_info=subscription.push_info(),
        data=data,
        curl=True,
        vapid_private_key=keys.private_key,
        vapid_claims={"sub": SUBJECT},
    )
    assert f"k={keys.public_key}" in str(request)
    assert "secret" not in str(request)


class Devices:
    def __init__(self, failures: dict[str, DeliveryError]) -> None:
        self.failures: dict[str, DeliveryError] = failures
        self.reached: list[str] = []

    def __call__(self, subscription: Subscription, data: str) -> None:
        del data
        self.reached.append(subscription.endpoint)
        failure = self.failures.get(subscription.endpoint)
        if failure is not None:
            raise failure


def test_one_failing_device_does_not_stop_the_others_and_the_gone_are_pruned(
    tmp_path: Path,
) -> None:
    clock = Clock()
    store = SubscriptionStore(tmp_path)
    endpoints = [f"https://push.example.invalid/{n}" for n in ("gone", "broken", "fine", "expired")]
    for endpoint in endpoints:
        _ = store.add(raw_subscription(endpoint))
    devices = Devices(
        {
            endpoints[0]: DeliveryError("gone", status=410),
            endpoints[1]: DeliveryError("boom", status=500),
            endpoints[3]: DeliveryError("nf", status=404),
        }
    )
    health = DeliveryLog(tmp_path, clock=clock)
    outcome = fan_out("{}", store.list_all(), deliver=devices, store=store, health=health)
    assert devices.reached == endpoints
    assert (outcome.sent, outcome.failed, outcome.pruned) == (1, 1, 2)
    assert [one.endpoint for one in store.list_all()] == [endpoints[1], endpoints[2]]
    record = DeliveryLog(tmp_path).load()
    assert record is not None
    assert (record.sent, record.failed, record.pruned, record.last_error) == (1, 1, 2, "boom")
    assert mode(health.path) == 0o600


def test_nothing_ever_delivered_is_the_story_doctor_tells(tmp_path: Path) -> None:
    health = DeliveryLog(tmp_path, clock=Clock())
    assert health.load() is None
    health.failed("403 BadJwtToken")
    record = DeliveryLog(tmp_path).load()
    assert record is not None
    assert record.never_delivered
    health.sent()
    later = DeliveryLog(tmp_path).load()
    assert later is not None
    assert not later.never_delivered


def test_an_unwritable_health_file_never_stops_a_push(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    _ = blocker.write_text("")
    health = DeliveryLog(blocker, clock=Clock())
    health.sent()
    assert health.load() is None


def test_a_sender_failure_carries_the_push_services_status() -> None:
    class Response:
        status_code: int = 410
        text: str = "gone"

    error = WebPushException("x", response=Response())
    wrapped = DeliveryError(str(error), status=error.status_code)
    assert wrapped.gone
    assert WebPushSender.__call__.__doc__
