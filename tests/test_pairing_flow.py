"""The pairing flow driven end to end through the real app by a scripted phone.

The human step, scanning the code and allowing notifications, is the only part left out.
"""

import time
from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import JsonValue, TypeAdapter

from herdr_pwa.engine import EngineSettings, HerdrClient
from herdr_pwa.pairing import pair, qr_text
from herdr_pwa.push import Subscription, SubscriptionStore
from herdr_pwa.settings import Settings
from herdr_pwa.web.app import create_app
from tests.fakes import LOGIN, NODE, status_route, tailscaled
from tests.test_push import raw_subscription
from tests.webfakes import SNAPSHOT, WORKING_PANE, FakeWire, agents_of, snapshot_frame

# Bounds a wait on the background loop; the loop itself runs every few milliseconds.
DEADLINE = 10.0


def blocked_frame() -> dict[str, JsonValue]:
    frame = snapshot_frame()
    for agent in agents_of(frame):
        if agent.get("pane_id") == WORKING_PANE:
            agent["agent_status"] = "blocked"
    return frame


def settings_for(tmp_path: Path) -> Settings:
    return Settings(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "no-runner.sock",
        tailnet_login=LOGIN,
        push_subject="mailto:operator@example.net",
        push_interval=0.02,
        push_debounce=0.0,
    )


def test_a_scripted_phone_pairs_installs_subscribes_and_is_pushed_to(tmp_path: Path) -> None:
    with tailscaled(tmp_path, canned=status_route(), serve_config={}) as fake:
        paired = pair(fake.api, port=8772)
        assert fake.serve_config == {
            "TCP": {"443": {"HTTPS": True}},
            "Web": {f"{NODE}:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8772"}}}},
        }
    assert paired.url == f"https://{NODE}/"
    assert qr_text(paired.url)

    wire = FakeWire({SNAPSHOT: snapshot_frame()})

    def factory(engine: EngineSettings) -> HerdrClient:
        return HerdrClient(engine, transport=wire)

    sent: list[dict[str, JsonValue]] = []
    payload: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])

    def deliver(subscription: Subscription, data: str) -> None:
        del subscription
        sent.append(payload.validate_json(data))

    app = create_app(settings_for(tmp_path), factory, deliver=deliver)
    # The phone, as tailscale serve presents it: same Host, identity header injected.
    ingress = {"Tailscale-User-Login": LOGIN}
    with TestClient(app, base_url=paired.url.rstrip("/"), headers=ingress) as phone:
        assert phone.get("/").status_code == 200
        for shell in ("/sw.js", "/manifest.webmanifest", "/offline", "/apple-touch-icon.png"):
            assert phone.get(shell).status_code == 200, shell
        assert phone.get("/push/key").json()["enabled"] is True
        device = raw_subscription()
        origin = {"Origin": paired.url.rstrip("/")}
        answer = phone.post("/push/subscribe", headers=origin, json=device)
        assert answer.json() == {"endpoint": device["endpoint"], "devices": 1, "armed": True}

        # Give the loop its priming tick on a calm herd, then block an agent.
        deadline = time.monotonic() + DEADLINE
        while not (tmp_path / "state" / "notify.json").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        wire.replies[SNAPSHOT] = blocked_frame()
        while not sent and time.monotonic() < deadline:
            time.sleep(0.01)
    assert sent, "the loop never pushed the blocked agent"
    assert sent[0]["event"] == "blocked"
    assert str(sent[0]["title"]).startswith("needs you")


def test_the_paired_url_refuses_anyone_the_ingress_did_not_vouch_for(tmp_path: Path) -> None:
    settings = settings_for(tmp_path).model_copy(update={"push_notifier": False})
    client = TestClient(create_app(settings), base_url=f"https://{NODE}")
    assert client.get("/push/key").status_code == 401
    assert client.get("/sw.js", headers={"Tailscale-User-Login": "other@x.org"}).status_code == 403
    cross = client.post(
        "/push/subscribe",
        headers={"Tailscale-User-Login": LOGIN, "Origin": "https://evil.example"},
        json=raw_subscription(),
    )
    assert cross.status_code == 403
    assert not SubscriptionStore(tmp_path / "state").list_all()
