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

from herdr_pwa import cli
from herdr_pwa.settings import Settings
from herdr_pwa.web.gate import MAX_BODY_BYTES
from herdr_pwa.web.security import CSP
from tests.webfakes import IDENTITY, WORKING_SESSION, build_rig

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


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "100.64.0.7", "10.0.0.5", "box.ts.net", ""])  # noqa: S104 - the bind under test
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
