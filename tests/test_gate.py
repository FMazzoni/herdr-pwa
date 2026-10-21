"""The identity gate covers every route, refuses before reading a body, and caps the body."""

import re
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send

from herdr_pwa import auth, cli
from herdr_pwa.pairing import RawForwardError
from herdr_pwa.settings import Settings
from herdr_pwa.web.gate import MAX_BODY_BYTES
from herdr_pwa.web.seams import Seams
from herdr_pwa.web.security import CSP
from tests.fakes import NODE, status_route, tailscaled
from tests.webfakes import IDENTITY, LOGIN, WORKING_SESSION, Rig, build_rig

# Every route a caller with no identity may reach. A new entry needs a reason here.
OPEN_ROUTES = frozenset(
    {
        # A liveness probe carries no identity and the answer says nothing about the herd.
        ("GET", "/healthz"),
    }
)
FORM = {"Content-Type": "application/x-www-form-urlencoded"}
SAME_ORIGIN = {"Origin": "http://testserver"}
PROMPT = f"/agent/{WORKING_SESSION}/prompt"


def _routes(app: FastAPI) -> Iterator[tuple[str, str]]:
    # Flattened through every included router, so a prefixed route is walked at its full path.
    for route in iter_route_contexts(app.routes):
        assert route.path is not None, f"a route with no path: {route}"
        assert route.methods, f"a route with no methods: {route.path}"
        for method in sorted(route.methods):
            yield method, route.path


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "x", path)


def test_every_route_but_the_open_ones_refuses_a_caller_with_no_identity(
    tmp_path: Path,
) -> None:
    # The debug sink is on so its route is walked too.
    rig = build_rig(tmp_path, headers={}, device_report=True)
    app = rig.http.app
    assert isinstance(app, FastAPI)
    walked = set(_routes(app))
    assert walked >= OPEN_ROUTES, "an open route no longer exists; drop it from the list"
    assert len(walked) > 20
    leaks: list[str] = []
    for method, path in sorted(walked - OPEN_ROUTES):
        response = rig.http.request(method, _concrete(path), headers=SAME_ORIGIN)
        if response.status_code != 401:
            leaks.append(f"{method} {path} -> {response.status_code}")
    assert leaks == []
    for method, path in OPEN_ROUTES:
        assert rig.http.request(method, path).status_code == 200
    assert rig.wire.calls == []


REBINDING = {"Host": "evil.example:8772", "Origin": "http://evil.example:8772"}


def _refusals(
    rig: Rig, headers: dict[str, str], status: int, *, writes_only: bool = False
) -> list[str]:
    app = rig.http.app
    assert isinstance(app, FastAPI)
    walked = sorted(set(_routes(app)) - OPEN_ROUTES)
    assert len(walked) > 20
    leaks: list[str] = []
    for method, path in walked:
        if writes_only and method in {"GET", "HEAD", "OPTIONS"}:
            continue
        response = rig.http.request(method, _concrete(path), headers=headers)
        if response.status_code != status:
            leaks.append(f"{method} {path} -> {response.status_code}")
    return leaks


def test_a_forged_header_on_a_rebinding_host_is_refused_on_every_route(tmp_path: Path) -> None:
    # A DNS-rebinding page sends its own name as Host and Origin, and may set the header.
    rig = build_rig(tmp_path, device_report=True)
    assert _refusals(rig, {**IDENTITY, **REBINDING}, 403) == []
    assert rig.wire.calls == []
    assert rig.http.get("/").status_code == 200


def test_another_tailnet_login_is_refused_on_every_route(tmp_path: Path) -> None:
    rig = build_rig(tmp_path, device_report=True, tailnet_login="owner@example.com")
    assert _refusals(rig, {**IDENTITY, **SAME_ORIGIN}, 403) == []
    assert rig.wire.calls == []


def test_a_cross_origin_write_is_refused_on_every_write_route(tmp_path: Path) -> None:
    rig = build_rig(tmp_path, device_report=True)
    cross = {"Origin": "https://evil.example"}
    assert _refusals(rig, cross, 403, writes_only=True) == []
    assert rig.wire.calls == []


def test_the_served_magicdns_name_is_accepted_in_any_spelling(tmp_path: Path) -> None:
    rig = build_rig(tmp_path, public_host=NODE)
    for host in (NODE, NODE.upper(), f"{NODE}.", f"{NODE}:443"):
        assert rig.http.get("/", headers={"Host": host}).status_code == 200, host
    assert rig.http.get("/", headers={"Host": f"{NODE}:8443"}).status_code == 403
    assert rig.http.get("/").status_code == 403


def _served_app(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, socket: Path, *, login: str = LOGIN
) -> TestClient:
    monkeypatch.setenv("HP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HP_SOCKET_PATH", str(tmp_path / "no-runner.sock"))
    monkeypatch.setenv("HP_TAILSCALED_SOCKET", str(socket))
    monkeypatch.setenv("HP_PUSH_NOTIFIER", "0")
    monkeypatch.delenv("HP_PUBLIC_HOST", raising=False)
    monkeypatch.delenv("HP_ASSUME_LOGIN", raising=False)
    monkeypatch.setenv("HP_TAILNET_LOGIN", login)
    served: list[object] = []

    def run(app: object, **_: object) -> None:
        served.append(app)

    monkeypatch.setattr(cli.uvicorn, "run", run)
    cli.serve(Settings(), 0, Seams())
    (app,) = served
    assert isinstance(app, FastAPI)
    return TestClient(app, headers=IDENTITY)


def test_serve_learns_the_served_name_from_tailscaled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with tailscaled(tmp_path, canned=status_route()) as fake:
        client = _served_app(monkeypatch, tmp_path, fake.socket_path)
    assert client.get("/", headers={"Host": NODE}).status_code == 200
    assert client.get("/", headers=REBINDING).status_code == 403


def test_serve_with_no_learnable_name_refuses_every_header_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    client = _served_app(monkeypatch, tmp_path, tmp_path / "no-tailscaled.sock")
    assert "Refusing every Tailscale identity" in caplog.text
    for host in (NODE, "testserver", "evil.example:8772"):
        assert client.get("/", headers={"Host": host}).status_code == 403, host


def _never_served(*_args: object, **_kwargs: object) -> None:
    pytest.fail("uvicorn was started")


def test_serve_refuses_to_start_with_no_login_chosen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HP_TAILNET_LOGIN", raising=False)
    monkeypatch.delenv("HP_ALLOW_ANY_TAILNET_USER", raising=False)
    monkeypatch.setattr(cli.uvicorn, "run", _never_served)
    settings = Settings(state_dir=tmp_path, tailscaled_socket=tmp_path / "none.sock")
    with pytest.raises(auth.NoLoginPinnedError, match="HP_TAILNET_LOGIN is not set"):
        cli.serve(settings, 0, Seams())


def test_serve_lets_any_tailnet_user_in_only_when_told_to(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("HP_ALLOW_ANY_TAILNET_USER", "1")
    with tailscaled(tmp_path, canned=status_route()) as fake:
        client = _served_app(monkeypatch, tmp_path, fake.socket_path, login="")
    assert "any user your tailnet lets reach this node" in caplog.text
    other = {"Tailscale-User-Login": "anyone@example.org", "Host": NODE}
    assert client.get("/", headers=other).status_code == 200


@pytest.mark.parametrize("target", ["127.0.0.1:8772", "tcp://localhost:8772", "[::1]:8772"])
def test_serve_refuses_a_raw_tcp_forward_to_its_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    # Raw TCP passes the client's own identity header through, so any tailnet device is the owner.
    forward = {"TCP": {"443": {"TCPForward": target, "TerminateTLS": NODE}}}
    monkeypatch.setattr(cli.uvicorn, "run", _never_served)
    with tailscaled(tmp_path, canned=status_route(), serve_config=forward) as fake:
        settings = Settings(
            state_dir=tmp_path, tailnet_login=LOGIN, port=8772, tailscaled_socket=fake.socket_path
        )
        with pytest.raises(RawForwardError, match="forwards raw TCP from port 443"):
            cli.serve(settings, 0, Seams())


def test_serve_starts_when_the_serve_config_cannot_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _ = _served_app(monkeypatch, tmp_path, tmp_path / "no-tailscaled.sock")
    assert "Could not check 'tailscale serve' for a raw TCP forward" in caplog.text


def test_two_identity_headers_are_refused_at_the_gate(tmp_path: Path) -> None:
    rig = build_rig(tmp_path, headers={})
    raw = [("Tailscale-User-Login", LOGIN), ("Tailscale-User-Login", LOGIN)]
    assert rig.http.get("/", headers=raw).status_code == 400
    assert rig.wire.calls == []


def test_an_unrouted_path_is_refused_too(tmp_path: Path) -> None:
    rig = build_rig(tmp_path, headers={})
    response = rig.http.get("/no/such/route")
    assert response.status_code == 401
    assert response.headers["content-security-policy"] == CSP


class _Counting:
    """Counts the body bytes the app pulled through ``receive``."""

    def __init__(self, app: FastAPI) -> None:
        self.app = app
        self.read = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def counted() -> Message:
            message = await receive()
            # An ASGI http.request message carries its body as bytes.
            self.read += len(cast("bytes", message.get("body", b"")))
            return message

        await self.app(scope, counted, send)


def _counting(tmp_path: Path) -> _Counting:
    app = build_rig(tmp_path, headers={}).http.app
    assert isinstance(app, FastAPI)
    return _Counting(app)


@pytest.mark.parametrize("path", [PROMPT, "/workspaces/new", "/push/subscribe"])
def test_a_caller_with_no_identity_is_refused_before_its_body_is_read(
    tmp_path: Path, path: str
) -> None:
    counting = _counting(tmp_path)
    body = b"a" * (8 * 1024 * 1024)
    response = TestClient(counting).post(path, content=body, headers={**FORM, **SAME_ORIGIN})
    assert response.status_code == 401
    assert counting.read == 0


def test_a_declared_oversize_body_is_refused_unread(tmp_path: Path) -> None:
    counting = _counting(tmp_path)
    body = b"a" * (MAX_BODY_BYTES + 1)
    headers = {**IDENTITY, **FORM, **SAME_ORIGIN}
    response = TestClient(counting).post(PROMPT, content=body, headers=headers)
    assert response.status_code == 413
    assert response.headers["content-security-policy"] == CSP
    assert counting.read == 0


def test_a_streamed_body_is_cut_off_at_the_cap(tmp_path: Path) -> None:
    rig = build_rig(tmp_path)
    chunk = b"a" * 4096

    def chunks() -> Iterator[bytes]:
        for _ in range(MAX_BODY_BYTES // len(chunk) + 4):
            yield chunk

    # No Content-Length: the size is only known as the bytes arrive.
    response = rig.http.post(PROMPT, content=chunks(), headers={**FORM, **SAME_ORIGIN})
    assert response.status_code == 413
    assert rig.wire.calls == []


def test_the_longest_prompt_the_form_allows_fits_under_the_cap(tmp_path: Path) -> None:
    rig = build_rig(tmp_path)
    # The textarea's maxlength in astral characters, each twelve bytes once urlencoded.
    prompt = "\U0001f600" * 4000
    response = rig.http.post(PROMPT, data={"text": prompt}, headers=SAME_ORIGIN)
    assert response.status_code != 413


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "::1", "[::1]", "localhost"])
def test_a_loopback_bind_is_allowed(host: str) -> None:
    cli.assert_loopback_bind(host)


@pytest.mark.parametrize(
    "host",
    [
        "0.0.0.0",  # noqa: S104 - the bind under test
        "::",
        "100.64.0.7",
        "10.0.0.5",
        "box.tailnet.ts.net",
        "",
    ],
)
def test_serve_refuses_a_bind_other_peers_can_reach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], host: str
) -> None:
    monkeypatch.setenv("HP_HOST", host)
    monkeypatch.setenv("HP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HP_TAILSCALED_SOCKET", str(tmp_path / "none.sock"))

    def never(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("uvicorn was started on a reachable bind")

    monkeypatch.setattr(cli.uvicorn, "run", never)
    assert Settings().host == host
    assert cli.main(["serve", "--port", "0"]) == 1
    assert "not a loopback address" in capsys.readouterr().err
