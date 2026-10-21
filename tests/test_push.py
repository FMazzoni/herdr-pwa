import base64
import json
import os
import socket
import stat
import threading
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast

import pytest
import pywebpush
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from pydantic import ValidationError
from pywebpush import WebPushException

from herdr_pwa import push, statedir
from herdr_pwa.push import (
    EVENT_BLOCKED,
    EVENT_CLEAR,
    EVENT_DONE,
    LINK_PLACEHOLDER,
    MAX_BODY_CHARS,
    MAX_PAYLOAD_BYTES,
    PUSH_LIMITS,
    DeliveryError,
    DeliveryLog,
    Notice,
    Outlet,
    PushError,
    PushLimits,
    SubjectError,
    Subscription,
    SubscriptionError,
    SubscriptionKeys,
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

ENDPOINT = "https://fcm.googleapis.com/fcm/send/one"
SUBJECT = "mailto:someone@herdr.example.org"


def b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def raw_subscription(
    endpoint: str = ENDPOINT, *, auth: bytes = bytes(range(16))
) -> dict[str, object]:
    point = bytes([0x04]) + bytes(range(64))
    return {"endpoint": endpoint, "keys": {"p256dh": b64u(point), "auth": b64u(auth)}}


def subscribed(endpoint: str = ENDPOINT, *, auth: bytes = bytes(range(16))) -> Subscription:
    return Subscription.model_validate(raw_subscription(endpoint, auth=auth))


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
        "mailto:first.last+push@mail.example.org",
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
    added = store.add(subscribed())
    assert store.list_all() == [added]
    assert mode(store.path) == 0o600


def test_resubscribing_a_device_updates_its_keys_and_keeps_its_stamp(tmp_path: Path) -> None:
    clock = Clock()
    store = SubscriptionStore(tmp_path, clock=clock)
    first = store.add(subscribed())
    clock.advance(60)
    second = store.add(subscribed(auth=bytes(16)))
    assert len(store.list_all()) == 1
    assert second.added_at == first.added_at
    assert second.keys.auth != first.keys.auth


def test_remove_forgets_one_device_and_is_idempotent(store: SubscriptionStore) -> None:
    _ = store.add(subscribed())
    _ = store.add(subscribed("https://fcm.googleapis.com/fcm/send/two"))
    assert store.remove(ENDPOINT)
    assert not store.remove(ENDPOINT)
    assert [one.endpoint for one in store.list_all()] == ["https://fcm.googleapis.com/fcm/send/two"]


@pytest.mark.parametrize(
    "raw",
    [
        None,
        [],
        {"endpoint": "http://fcm.googleapis.com/fcm/send/x", "keys": {}},
        {"endpoint": ENDPOINT},
        {"endpoint": ENDPOINT, "keys": {"p256dh": "AAAA", "auth": "AAAA"}},
        {
            **raw_subscription(),
            "keys": {"p256dh": b64u(bytes([2]) + bytes(64)), "auth": b64u(bytes(16))},
        },
        raw_subscription("https://fcm.googleapis.com/fcm/send/" + "x" * 2100),
        {**raw_subscription(), "expirationTime": "soon"},
    ],
)
def test_a_malformed_subscription_is_refused(raw: object) -> None:
    with pytest.raises(ValidationError):
        _ = Subscription.model_validate(raw)


def test_one_bad_record_does_not_hide_the_good_ones(store: SubscriptionStore) -> None:
    good = store.add(subscribed())
    entries = [{"endpoint": "nope"}, good.model_dump(mode="json", by_alias=True)]
    _ = store.path.write_text(json.dumps({"version": 1, "subscriptions": entries}))
    assert store.list_all() == [good]


@pytest.mark.parametrize("content", ["{nope", "[]", '{"subscriptions": 3}'])
def test_a_corrupt_store_is_loud(store: SubscriptionStore, content: str) -> None:
    _ = store.path.write_text(content)
    with pytest.raises(SubscriptionError):
        _ = store.list_all()


def test_a_failed_write_keeps_the_previous_store(
    store: SubscriptionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = store.add(subscribed())
    before = store.path.read_text()
    # A full disk surfaces at the flush to it, before the rename could replace the store.
    monkeypatch.setattr(statedir.os, "fsync", _disk_full)
    with pytest.raises(OSError, match="disk full"):
        _ = store.add(subscribed("https://fcm.googleapis.com/fcm/send/two"))
    assert store.path.read_text() == before


def test_a_device_added_while_one_is_pruned_is_kept(
    store: SubscriptionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    gone, kept, added = (f"https://fcm.googleapis.com/fcm/send/{n}" for n in ("a", "b", "c"))
    _ = store.add(subscribed(gone))
    _ = store.add(subscribed(kept))
    write = statedir.write_secret
    pruning, release, wrote = threading.Event(), threading.Event(), threading.Event()

    def held(path: Path, text: str, *, exclusive: bool = False) -> None:
        # The pruner stops inside its write, after reading the store, until the add has had its go.
        if threading.current_thread().name == "pruner":
            pruning.set()
            _ = release.wait(5)
        write(path, text, exclusive=exclusive)
        if threading.current_thread().name == "adder":
            wrote.set()

    monkeypatch.setattr(statedir, "write_secret", held)
    pruner = threading.Thread(target=store.remove, args=(gone,), name="pruner")
    adder = threading.Thread(target=store.add, args=(subscribed(added),), name="adder")
    pruner.start()
    assert pruning.wait(5)
    adder.start()
    _ = wrote.wait(0.5)
    release.set()
    pruner.join(5)
    adder.join(5)
    assert [one.endpoint for one in store.list_all()] == [kept, added]


def _disk_full(_fd: int) -> None:
    raise OSError("disk full")


def test_ansi_osc_controls_bidi_and_zero_width_are_stripped() -> None:
    text = "\x1b]0;title\x07\x1b[31mred\x1b[0m\u202eevil\u200bx\x00y\nz"
    assert sanitise(text, limit=100) == "red evil x y z"


@pytest.mark.parametrize(
    ("text", "cleaned"),
    [
        ("before \x1b]0;evil title, no terminator", "before"),
        ("\x1bPq#0;2;0;0;0sixel\x1b\\after", "after"),
        ("\x1b_APC payload\x1b\\after", "after"),
        ("\x1b(Bafter", "after"),
        ("\x1b7saved\x1b8", "saved"),
    ],
)
def test_escapes_are_read_as_the_screen_reads_them(text: str, cleaned: str) -> None:
    # One grammar for escapes: a payload the pane never shows never reaches the lock screen.
    assert sanitise(text, limit=100) == cleaned


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


@pytest.mark.parametrize(
    ("text", "cleaned"),
    [
        ("(see https://evil.example/pay)", "(see [link]"),
        ("run -evil.example.com now", "run -[link] now"),
        ("a..evil.example.com", "a..[link]"),
        ("x.com.evil.example/pay", "[link]"),
    ],
)
def test_a_link_inside_punctuation_is_still_found(text: str, cleaned: str) -> None:
    assert sanitise(text, limit=MAX_BODY_CHARS) == cleaned


# One unbroken word made the old pattern rescan the rest of it from every letter: 18 s at 20k.
ADVERSARIAL = ("a" * 100_000, "a." * 50_000, "a-" * 50_000, "1a" * 50_000, "a.." * 33_333)


@pytest.mark.parametrize("text", ADVERSARIAL, ids=["word", "dots", "dashes", "digits", "breaks"])
def test_scanning_for_links_is_linear_in_the_text(text: str) -> None:
    # CPU time, so a loaded gate cannot inflate it; the old pattern took minutes, far past 1 s.
    started = time.process_time()
    _ = push._URL_RE.sub(LINK_PLACEHOLDER, text)  # pyright: ignore[reportPrivateUsage]
    assert time.process_time() - started < 1.0


def test_a_long_notice_is_cut_before_it_is_scanned() -> None:
    started = time.process_time()
    cleaned = sanitise("a" * 100_000, limit=MAX_BODY_CHARS)
    assert time.process_time() - started < 1.0
    assert cleaned == "a" * (MAX_BODY_CHARS - 1) + "…"


@pytest.mark.parametrize("padding", range(4075, 4095))
def test_a_link_cut_by_the_scan_cap_is_dropped_and_the_cut_marked(padding: int) -> None:
    cleaned = sanitise("https://x/" + "a" * padding + " evil.ru/pay", limit=MAX_BODY_CHARS)
    assert cleaned == f"{LINK_PLACEHOLDER} …"


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
    subscription = Subscription.model_validate(device)
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


def test_a_subscription_past_its_expiration_is_pruned_without_a_send(tmp_path: Path) -> None:
    clock = Clock()
    store = SubscriptionStore(tmp_path)
    ms = clock() * 1000
    expired = {**raw_subscription(), "expirationTime": ms}
    live = {
        **raw_subscription("https://fcm.googleapis.com/fcm/send/live"),
        "expirationTime": ms + 1,
    }
    for raw in (expired, live, raw_subscription("https://fcm.googleapis.com/fcm/send/forever")):
        _ = store.add(Subscription.model_validate(raw))
    devices = Devices({})
    health = DeliveryLog(tmp_path, clock=clock)
    outcome = fan_out("{}", store.list_all(), Outlet(devices, store, health), now=clock())
    assert devices.reached == [
        "https://fcm.googleapis.com/fcm/send/live",
        "https://fcm.googleapis.com/fcm/send/forever",
    ]
    assert (outcome.sent, outcome.pruned) == (2, 1)
    assert ENDPOINT not in [one.endpoint for one in store.list_all()]
    record = health.load()
    assert record is not None
    assert record.pruned == 1


def test_one_failing_device_does_not_stop_the_others_and_the_gone_are_pruned(
    tmp_path: Path,
) -> None:
    clock = Clock()
    store = SubscriptionStore(tmp_path)
    endpoints = [
        f"https://fcm.googleapis.com/fcm/send/{n}" for n in ("gone", "broken", "fine", "expired")
    ]
    for endpoint in endpoints:
        _ = store.add(subscribed(endpoint))
    devices = Devices(
        {
            endpoints[0]: DeliveryError("gone", status=410),
            endpoints[1]: DeliveryError("boom", status=500),
            endpoints[3]: DeliveryError("nf", status=404),
        }
    )
    health = DeliveryLog(tmp_path, clock=clock)
    outcome = fan_out("{}", store.list_all(), Outlet(devices, store, health), now=clock())
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


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://web.push.apple.com/QAbc-123",
        "https://fcm.googleapis.com/fcm/send/abc:def",
        "https://updates.push.services.mozilla.com/wpush/v2/gAAA",
        "https://wns2-par02p.notify.windows.com/w/?token=AAAA",
        "https://fcm.googleapis.com:443/fcm/send/abc",
    ],
)
def test_the_browsers_push_services_are_accepted(endpoint: str) -> None:
    assert subscribed(endpoint).endpoint == endpoint


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://127.0.0.1/",
        "https://10.0.0.7/push",
        "https://192.168.1.20/push",
        "https://[::1]/push",
        "https://169.254.169.254/latest/meta-data",
        "https://localhost/push",
        "https://printer.local/push",
        "https://metadata.google.internal/push",
        "https://fcm.googleapis.com.attacker.example/push",
        "https://evilfcm.googleapis.com/push",
        "https://fcm.googleapis.com:8443/fcm/send/abc",
    ],
)
def test_an_endpoint_off_the_push_services_is_refused(endpoint: str) -> None:
    with pytest.raises(ValidationError, match="not on a browser push service"):
        _ = subscribed(endpoint)


def test_an_endpoint_ending_in_a_newline_is_refused() -> None:
    # A pattern's ``$`` also matches before a final newline: https://docs.python.org/3/library/re.html
    with pytest.raises(ValidationError, match="must be an https:// URL"):
        _ = subscribed(f"{ENDPOINT}\n")


def test_an_eleventh_device_is_refused_but_a_known_one_still_updates(
    store: SubscriptionStore,
) -> None:
    endpoints = [f"https://fcm.googleapis.com/fcm/send/{n}" for n in range(push.MAX_DEVICES)]
    for endpoint in endpoints:
        _ = store.add(subscribed(endpoint))
    with pytest.raises(SubscriptionError, match="remove one first"):
        _ = store.add(subscribed("https://fcm.googleapis.com/fcm/send/eleventh"))
    _ = store.add(subscribed(endpoints[0], auth=bytes(16)))
    assert [one.endpoint for one in store.list_all()] == [*endpoints[1:], endpoints[0]]


def unchecked(endpoint: str) -> Subscription:
    # A local endpoint the validator refuses, built raw so the sender itself is what is tested.
    point = (
        ec.generate_private_key(ec.SECP256R1())
        .public_key()
        .public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    )
    keys = SubscriptionKeys.model_validate({"p256dh": b64u(point), "auth": b64u(bytes(16))})
    return Subscription.model_construct(endpoint=endpoint, keys=keys)


def a_sender(tmp_path: Path, limits: PushLimits = PUSH_LIMITS) -> WebPushSender:
    return arm_sender(load_or_create_vapid_keys(tmp_path), subject=SUBJECT, ttl=60, limits=limits)


def test_a_push_service_that_never_answers_fails_within_the_timeout(tmp_path: Path) -> None:
    limits = replace(PUSH_LIMITS, send_timeout=0.5)
    with socket.create_server(("127.0.0.1", 0)) as silent:
        port = cast("tuple[str, int]", silent.getsockname())[1]
        started = time.monotonic()
        with pytest.raises(DeliveryError) as refused:
            a_sender(tmp_path, limits)(unchecked(f"https://127.0.0.1:{port}/push"), "{}")
    assert time.monotonic() - started < 5
    assert refused.value.status is None


@contextmanager
def answering(status: int, headers: dict[str, str], body: bytes) -> Generator[list[str]]:
    seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            seen.append(self.path)
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            _ = self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - the base names it
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        seen.append(f"http://127.0.0.1:{server.server_address[1]}")
        yield seen
    finally:
        server.shutdown()
        server.server_close()


def test_a_redirect_is_the_answer_and_is_never_followed(tmp_path: Path) -> None:
    with (
        answering(204, {}, b"") as target,
        answering(307, {"Location": f"{target[0]}/elsewhere"}, b"moved") as service,
    ):
        with pytest.raises(DeliveryError) as refused:
            a_sender(tmp_path)(unchecked(f"{service[0]}/push"), "{}")
        assert service[1:] == ["/push"]
        assert target[1:] == []
    assert refused.value.status == 307


def test_a_refusal_keeps_the_status_and_reason_and_never_the_body(tmp_path: Path) -> None:
    with (
        answering(500, {}, b"internal-detail-from-the-service") as service,
        pytest.raises(DeliveryError) as refused,
    ):
        a_sender(tmp_path)(unchecked(f"{service[0]}/push"), "{}")
    assert str(refused.value) == "push service answered 500 Internal Server Error"
    assert refused.value.status == 500


def test_a_fan_out_past_its_budget_skips_the_rest(tmp_path: Path) -> None:
    clock = Clock()
    elapsed = Clock(0.0)
    store = SubscriptionStore(tmp_path)
    endpoints = [f"https://fcm.googleapis.com/fcm/send/{n}" for n in range(3)]
    reached: list[str] = []

    def slow(subscription: Subscription, data: str) -> None:
        del data
        reached.append(subscription.endpoint)
        # The first send used the whole budget.
        elapsed.advance(PUSH_LIMITS.fan_out_budget + 1)

    health = DeliveryLog(tmp_path, clock=clock)
    devices = [subscribed(endpoint) for endpoint in endpoints]
    outlet = Outlet(slow, store, health, clock=elapsed)
    outcome = fan_out("{}", devices, outlet, now=clock())
    assert reached == endpoints[:1]
    assert (outcome.sent, outcome.failed) == (1, 2)
    record = health.load()
    assert record is not None
    assert "skipped 2 devices" in record.last_error
