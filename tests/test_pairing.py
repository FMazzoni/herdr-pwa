import inspect
import json
from pathlib import Path

import pytest

from herdr_pwa import pairing, tailscale
from herdr_pwa.pairing import (
    TEARDOWN_COMMAND,
    PairingError,
    pair,
    pair_url,
    publish,
    qr_text,
    serve_state,
)
from herdr_pwa.tailscale import SERVE_CONFIG_PATH, LocalApi, LocalApiError
from tests.fakes import NODE, Canned, FakeTailscaled, status_route, tailscaled

PORT = 8772
OURS = f"http://127.0.0.1:{PORT}"
LIVE = {
    "TCP": {"443": {"HTTPS": True}},
    "Web": {f"{NODE}:443": {"Handlers": {"/": {"Proxy": OURS}}}},
}


def state_of(fake: FakeTailscaled) -> pairing.ServeState:
    return serve_state(fake.api, port=PORT, host=NODE)


def posts(fake: FakeTailscaled) -> list[str]:
    return [r.body for r in fake.requests if r.method == "POST"]


def test_the_url_is_always_https_and_safe_to_screenshot() -> None:
    assert pair_url(f"{NODE}.") == f"https://{NODE}/"
    assert pair_url(NODE, "agents") == f"https://{NODE}/agents"
    assert "?" not in pair_url(NODE)
    with pytest.raises(PairingError):
        _ = pair_url("  ")


def test_a_qr_is_rendered_only_for_an_https_url() -> None:
    assert qr_text(f"https://{NODE}/").strip()
    for bad in ("", "https://", f"http://{NODE}/", "javascript:alert(1)"):
        with pytest.raises(PairingError):
            _ = qr_text(bad)


def test_an_existing_matching_config_is_found_and_not_rewritten(tmp_path: Path) -> None:
    with tailscaled(tmp_path, serve_config=LIVE) as fake:
        state = state_of(fake)
        assert (state.published, state.occupied_by, state.funnel) == (True, "", False)
        assert publish(fake.api, state) is state
        assert posts(fake) == []


@pytest.mark.parametrize("config", [{}, None])
def test_nothing_served_is_not_an_error(tmp_path: Path, config: object) -> None:
    with tailscaled(tmp_path, serve_config=config) as fake:
        state = state_of(fake)
        assert (state.published, state.configured, state.occupied_by) == (False, False, "")


def test_publish_sends_the_etag_and_a_second_run_writes_nothing(tmp_path: Path) -> None:
    with tailscaled(tmp_path, serve_config={}) as fake:
        etag = fake.etag()
        done = publish(fake.api, state_of(fake))
        assert done.changed
        assert done.published
        post = next(r for r in fake.requests if r.method == "POST")
        assert post.headers["if-match"] == etag
        assert fake.serve_config == LIVE
        again = state_of(fake)
        assert again.published
        assert not publish(fake.api, again).changed
        assert len(posts(fake)) == 1


def test_publish_keeps_what_somebody_else_serves_and_does_not_mutate_its_input(
    tmp_path: Path,
) -> None:
    other = {"Web": {"other.ts.net:443": {"Handlers": {"/x": {"Proxy": "http://127.0.0.1:9000"}}}}}
    with tailscaled(tmp_path, serve_config=other) as fake:
        state = state_of(fake)
        before = json.dumps(state.config, sort_keys=True)
        _ = publish(fake.api, state)
        assert json.dumps(state.config, sort_keys=True) == before
        sites = tailscale.serve_config(posts(fake)[0])["Web"]
        assert isinstance(sites, dict)
        assert "other.ts.net:443" in sites
        assert f"{NODE}:443" in sites


@pytest.mark.parametrize(
    "spelling", ["http://localhost:8772", "127.0.0.1:8772", "http://127.0.0.1:8772/"]
)
def test_the_same_place_spelled_differently_is_still_ours(tmp_path: Path, spelling: str) -> None:
    config = {"Web": {f"{NODE}:443": {"Handlers": {"/": {"Proxy": spelling}}}}}
    with tailscaled(tmp_path, serve_config=config) as fake:
        assert state_of(fake).published


@pytest.mark.parametrize(
    ("handler", "occupant"),
    [({"Proxy": "http://127.0.0.1:8770"}, "8770"), ({"Path": "/srv"}, "non-proxy")],
)
def test_another_handler_at_the_root_is_never_overwritten(
    tmp_path: Path, handler: dict[str, str], occupant: str
) -> None:
    config = {"Web": {f"{NODE}:443": {"Handlers": {"/": handler}}}}
    with tailscaled(tmp_path, serve_config=config) as fake:
        state = state_of(fake)
        assert occupant in state.occupied_by
        with pytest.raises(PairingError, match="Refusing to take it over") as caught:
            _ = publish(fake.api, state)
        assert TEARDOWN_COMMAND in str(caught.value)
        assert posts(fake) == []


def test_funnel_switched_on_stops_the_write(tmp_path: Path) -> None:
    with tailscaled(tmp_path, serve_config={"AllowFunnel": {f"{NODE}:443": True}}) as fake:
        state = state_of(fake)
        assert state.funnel
        with pytest.raises(PairingError, match="Funnel"):
            _ = publish(fake.api, state)
        assert posts(fake) == []


def test_funnel_turned_off_again_is_not_funnel_and_never_reaches_the_write(tmp_path: Path) -> None:
    with tailscaled(tmp_path, serve_config={"AllowFunnel": {f"{NODE}:443": False}}) as fake:
        state = state_of(fake)
        assert not state.funnel
        _ = publish(fake.api, state)
        assert "funnel" not in posts(fake)[0].casefold()


def test_no_command_this_module_prints_enables_funnel_or_resets_serve() -> None:
    source = inspect.getsource(pairing)
    assert "tailscale funnel" not in source
    assert "serve reset'" not in TEARDOWN_COMMAND
    assert TEARDOWN_COMMAND == "tailscale serve --https=443 off"


def test_a_concurrent_change_is_refused_rather_than_clobbered(tmp_path: Path) -> None:
    with tailscaled(tmp_path, serve_config={}) as fake:
        state = state_of(fake)
        fake.serve_config = {"Web": {"x:443": {}}}
        with pytest.raises(PairingError, match="nothing was written"):
            _ = publish(fake.api, state)
        assert fake.serve_config == {"Web": {"x:443": {}}}


@pytest.mark.parametrize(("status", "message"), [(403, "operator"), (500, "nothing published")])
def test_a_refused_write_says_what_to_do(tmp_path: Path, status: int, message: str) -> None:
    with (
        tailscaled(tmp_path, serve_config={}, post_status=status) as fake,
        pytest.raises(PairingError, match=message),
    ):
        _ = publish(fake.api, state_of(fake))


@pytest.mark.parametrize("body", ["", "not json", "[1]"])
def test_an_unreadable_serve_config_is_an_error(tmp_path: Path, body: str) -> None:
    canned = {("GET", SERVE_CONFIG_PATH): Canned(body=body)}
    with tailscaled(tmp_path, canned=canned) as fake, pytest.raises(PairingError):
        _ = state_of(fake)


def test_pair_publishes_once_and_then_finds_itself(tmp_path: Path) -> None:
    with tailscaled(tmp_path, canned=status_route(), serve_config={}) as fake:
        first = pair(fake.api, port=PORT)
        second = pair(fake.api, port=PORT)
    assert first.url == second.url == f"https://{NODE}/"
    assert (first.served, first.state.changed, second.state.changed) == (True, True, False)
    assert TEARDOWN_COMMAND in first.note


def test_pair_without_write_changes_nothing(tmp_path: Path) -> None:
    with tailscaled(tmp_path, canned=status_route(), serve_config={}) as fake:
        result = pair(fake.api, port=PORT, write=False)
        assert not result.served
        assert posts(fake) == []


def test_no_url_means_no_qr(tmp_path: Path) -> None:
    with pytest.raises(LocalApiError, match="Install Tailscale"):
        _ = pair(LocalApi(tmp_path / "missing.sock"), port=PORT)
