"""The pairing flow driven end to end by a scripted client; the human phone step is left out.

The app's routes are wired elsewhere, so a minimal stand-in app composes the same library
calls a real route will: authorize every request, then read the key or store the device.
"""

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from herdr_pwa import auth, jsonio
from herdr_pwa.auth import Access, AuthError
from herdr_pwa.pairing import pair, qr_text
from herdr_pwa.push import (
    EVENT_BLOCKED,
    DeliveryLog,
    Notice,
    Subscription,
    SubscriptionStore,
    build_payload,
    encode_payload,
    fan_out,
    load_or_create_vapid_keys,
)
from tests.fakes import LOGIN, NODE, Clock, status_route, tailscaled
from tests.test_push import raw_subscription


def stand_in_app(state_dir: Path, access: Access) -> FastAPI:
    app = FastAPI()
    store = SubscriptionStore(state_dir)

    def gate(request: Request) -> auth.Identity:
        try:
            return auth.authorize(request.headers, access=access, method=request.method)
        except AuthError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc

    @app.get("/")
    def home(request: Request) -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"login": gate(request).login}

    @app.get("/api/push/key")
    def key(request: Request) -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        _ = gate(request)
        return {"key": load_or_create_vapid_keys(state_dir).public_key}

    @app.post("/api/push/subscribe")
    async def subscribe(request: Request) -> dict[str, int]:  # pyright: ignore[reportUnusedFunction]
        _ = gate(request)
        _ = store.add(jsonio.loads((await request.body()).decode()))
        return {"devices": len(store.list_all())}

    return app


def test_a_scripted_phone_pairs_subscribes_and_is_pushed_to(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    with tailscaled(tmp_path, canned=status_route(), serve_config={}) as fake:
        paired = pair(fake.api, port=8772)
        assert paired.served
        assert fake.serve_config == {
            "TCP": {"443": {"HTTPS": True}},
            "Web": {f"{NODE}:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8772"}}}},
        }
    assert paired.url == f"https://{NODE}/"
    assert qr_text(paired.url)

    # The phone, as tailscale serve presents it: same Host, identity header injected.
    phone = TestClient(stand_in_app(state_dir, Access(tailnet_login=LOGIN)), base_url=paired.url)
    ingress = {"Tailscale-User-Login": LOGIN}
    assert phone.get("/", headers=ingress).json() == {"login": LOGIN}
    key = phone.get("/api/push/key", headers=ingress)
    assert key.json() == {"key": load_or_create_vapid_keys(state_dir).public_key}

    device = raw_subscription()
    answer = phone.post(
        "/api/push/subscribe", headers={**ingress, "Origin": paired.url.rstrip("/")}, json=device
    )
    assert answer.json() == {"devices": 1}

    sent: list[tuple[str, str]] = []

    def deliver(subscription: Subscription, data: str) -> None:
        sent.append((subscription.endpoint, data))

    clock = Clock()
    notice = Notice(event=EVENT_BLOCKED, title="needs you: parser", body="Proceed?", agent="s")
    data = encode_payload(build_payload(notice, now=clock()))
    store = SubscriptionStore(state_dir)
    outcome = fan_out(
        data,
        store.list_all(),
        deliver=deliver,
        store=store,
        health=DeliveryLog(state_dir, clock=clock),
    )
    assert outcome.sent == 1
    assert sent == [(device["endpoint"], data)]


def test_the_paired_url_refuses_anyone_the_ingress_did_not_vouch_for(tmp_path: Path) -> None:
    app = stand_in_app(tmp_path, Access(tailnet_login=LOGIN))
    client = TestClient(app, base_url=f"https://{NODE}")
    assert client.get("/").status_code == 401
    assert client.get("/", headers={"Tailscale-User-Login": "other@x.org"}).status_code == 403
    cross = client.post(
        "/api/push/subscribe",
        headers={"Tailscale-User-Login": LOGIN, "Origin": "https://evil.example"},
        json=raw_subscription(),
    )
    assert cross.status_code == 403
    assert not SubscriptionStore(tmp_path).list_all()
