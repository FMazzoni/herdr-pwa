"""Every push route over the app: what it answers, what it stores, and what it refuses."""

import json
from pathlib import Path

import pytest

from herdr_pwa.notify_state import InterruptStore, SeenLedger
from herdr_pwa.push import SubscriptionStore, load_or_create_vapid_keys
from tests.test_push import ENDPOINT, raw_subscription
from tests.webfakes import IDENTITY, LOGIN, build_rig

SAME_ORIGIN = {**IDENTITY, "Origin": "http://testserver"}
WRITES = (
    ("/push/subscribe", raw_subscription()),
    ("/push/unsubscribe", {"endpoint": ENDPOINT}),
    ("/push/prefs", {"done": True}),
    ("/push/snooze", {"seconds": 60}),
)
READS = ("/push", "/push/key", "/push/status", "/push/prefs")


def test_the_key_route_hands_out_the_boxes_public_key(tmp_path: Path) -> None:
    rig = build_rig(tmp_path)
    answer = rig.http.get("/push/key")
    assert answer.status_code == 200
    expected = load_or_create_vapid_keys(rig.settings.state_dir).public_key
    assert answer.json() == {"enabled": True, "publicKey": expected}
    assert answer.headers["cache-control"] == "no-store"


def test_subscribe_stores_the_device_and_echoes_it_as_confirmation(tmp_path: Path) -> None:
    rig = build_rig(tmp_path)
    answer = rig.http.post("/push/subscribe", headers=SAME_ORIGIN, json=raw_subscription())
    assert answer.status_code == 200
    assert answer.json() == {"endpoint": ENDPOINT, "devices": 1, "armed": False}
    stored = SubscriptionStore(rig.settings.state_dir).list_all()
    assert [one.endpoint for one in stored] == [ENDPOINT]
    again = rig.http.post("/push/subscribe", headers=SAME_ORIGIN, json=raw_subscription())
    assert again.json()["devices"] == 1


def test_unsubscribe_forgets_only_that_device(tmp_path: Path) -> None:
    rig = build_rig(tmp_path)
    other = "https://push.example.net/other"
    for body in (raw_subscription(), raw_subscription(other)):
        _ = rig.http.post("/push/subscribe", headers=SAME_ORIGIN, json=body)
    answer = rig.http.post("/push/unsubscribe", headers=SAME_ORIGIN, json={"endpoint": ENDPOINT})
    assert answer.json() == {"removed": True, "devices": 1}
    gone = rig.http.post("/push/unsubscribe", headers=SAME_ORIGIN, json={"endpoint": ENDPOINT})
    assert gone.json() == {"removed": False, "devices": 1}


@pytest.mark.parametrize(
    ("route", "body"),
    [
        ("/push/subscribe", b"not json"),
        ("/push/subscribe", b"[1, 2]"),
        ("/push/subscribe", json.dumps({"endpoint": "http://plain.example/x"}).encode()),
        ("/push/subscribe", b"{" + b" " * 9000 + b"}"),
        ("/push/unsubscribe", b"{}"),
        ("/push/prefs", b"null"),
        ("/push/snooze", json.dumps({"seconds": "soon"}).encode()),
    ],
)
def test_a_malformed_body_is_a_400_with_a_reason(tmp_path: Path, route: str, body: bytes) -> None:
    rig = build_rig(tmp_path)
    answer = rig.http.post(route, headers=SAME_ORIGIN, content=body)
    assert answer.status_code == 400
    assert answer.json()["error"]
    assert not SubscriptionStore(rig.settings.state_dir).list_all()


def test_preferences_default_to_blocked_on_and_done_off(tmp_path: Path) -> None:
    rig = build_rig(tmp_path)
    policy = rig.http.get("/push/prefs")
    assert policy.json()["blocked"] is True
    assert policy.json()["done"] is False
    assert policy.json()["snoozed"] is False


def test_a_preference_change_is_kept_on_the_box_and_echoed(tmp_path: Path) -> None:
    rig = build_rig(tmp_path)
    answer = rig.http.post("/push/prefs", headers=SAME_ORIGIN, json={"done": True})
    assert (answer.json()["blocked"], answer.json()["done"]) == (True, True)
    stored = InterruptStore(rig.settings.state_dir).load()
    assert (stored.blocked, stored.done) == (True, True)
    # A key that was not sent is left alone; a non-bool is not a change.
    left = rig.http.post("/push/prefs", headers=SAME_ORIGIN, json={"blocked": "no"})
    assert left.json()["blocked"] is True


def test_snooze_starts_and_ends_a_quiet_window(tmp_path: Path) -> None:
    rig = build_rig(tmp_path)
    on = rig.http.post("/push/snooze", headers=SAME_ORIGIN, json={"seconds": 3600})
    assert on.json()["snoozed"] is True
    off = rig.http.post("/push/snooze", headers=SAME_ORIGIN, json={"seconds": 0})
    assert off.json()["snoozed"] is False


def test_seen_marks_the_session_read_for_the_notifier(tmp_path: Path) -> None:
    rig = build_rig(tmp_path)
    answer = rig.http.post("/push/seen/abc", headers=SAME_ORIGIN)
    assert answer.status_code == 204
    ledger = SeenLedger(rig.settings.state_dir)
    ledger.flush()
    assert "abc" not in ledger.unread_sessions()


def test_status_says_whether_anything_is_being_sent_and_why_not(tmp_path: Path) -> None:
    rig = build_rig(tmp_path)
    _ = rig.http.post("/push/subscribe", headers=SAME_ORIGIN, json=raw_subscription())
    status = rig.http.get("/push/status")
    assert status.json()["devices"] == 1
    assert status.json()["armed"] is False
    assert "HP_PUSH_NOTIFIER" in status.json()["reason"]


def test_an_unarmed_box_says_so_on_the_alerts_page(tmp_path: Path) -> None:
    rig = build_rig(tmp_path, push_notifier=True)
    page = rig.http.get("/push")
    assert page.status_code == 200
    assert "Alerts are not being sent" in page.text
    assert "HP_PUSH_SUBJECT" in page.text
    assert 'id="pref-blocked" checked' in page.text
    assert 'id="pref-done">' in page.text


def test_with_push_off_every_route_says_so_and_no_key_is_minted(tmp_path: Path) -> None:
    rig = build_rig(tmp_path, push_enabled=False)
    for route in READS[1:]:
        answer = rig.http.get(route)
        assert answer.status_code == 503, route
        assert answer.json()["enabled"] is False
    for route, body in WRITES:
        assert rig.http.post(route, headers=SAME_ORIGIN, json=body).status_code == 503
    assert "switched off" in rig.http.get("/push").text
    assert not (rig.settings.state_dir / "vapid.json").exists()


@pytest.mark.parametrize("route", READS)
def test_every_read_refuses_a_request_without_identity(tmp_path: Path, route: str) -> None:
    rig = build_rig(tmp_path, headers={})
    assert rig.http.get(route).status_code == 401


@pytest.mark.parametrize(("route", "body"), WRITES)
def test_every_write_refuses_a_request_without_identity(
    tmp_path: Path, route: str, body: object
) -> None:
    rig = build_rig(tmp_path, headers={})
    answer = rig.http.post(route, headers={"Origin": "http://testserver"}, json=body)
    assert answer.status_code == 401
    assert not SubscriptionStore(rig.settings.state_dir).list_all()


@pytest.mark.parametrize("route", READS)
def test_a_login_other_than_the_pinned_one_is_refused(tmp_path: Path, route: str) -> None:
    rig = build_rig(tmp_path, tailnet_login="someone-else@example.com")
    assert rig.http.get(route).status_code == 403
    pinned = build_rig(tmp_path / "pinned", tailnet_login=LOGIN)
    assert pinned.http.get(route).status_code == 200


@pytest.mark.parametrize(("route", "body"), WRITES)
@pytest.mark.parametrize("origin", ["https://evil.example", "null", None])
def test_every_write_refuses_another_or_no_origin(
    tmp_path: Path, route: str, body: object, origin: str | None
) -> None:
    rig = build_rig(tmp_path)
    headers = dict(IDENTITY) if origin is None else {**IDENTITY, "Origin": origin}
    answer = rig.http.post(route, headers=headers, json=body)
    assert answer.status_code == 403
    assert not SubscriptionStore(rig.settings.state_dir).list_all()
    assert InterruptStore(rig.settings.state_dir).load().done is False


def test_seen_is_a_write_and_is_origin_checked(tmp_path: Path) -> None:
    rig = build_rig(tmp_path)
    answer = rig.http.post("/push/seen/abc", headers={**IDENTITY, "Origin": "https://evil.example"})
    assert answer.status_code == 403
