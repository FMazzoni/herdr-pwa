"""``herdr-pwa pair`` against a fake tailscaled: what it publishes, prints and refuses."""

from pathlib import Path

import pytest

from herdr_pwa.cli import main, pair
from herdr_pwa.pairing import TEARDOWN_COMMAND
from herdr_pwa.settings import Settings
from herdr_pwa.tailscale import LocalApi
from herdr_pwa.web.seams import Seams
from tests.fakes import NODE, status_route, tailscaled


def run(settings: Settings, api: LocalApi, *, publish: bool = True) -> tuple[int, str]:
    out: list[str] = []
    code = pair(settings, Seams(write=out.append, local_api=lambda _path: api), publish=publish)
    return code, "".join(out)


def test_pair_publishes_once_and_prints_a_qr_of_the_https_url(tmp_path: Path) -> None:
    settings = Settings(state_dir=tmp_path / "state", tailnet_login="operator@example.com")
    with tailscaled(tmp_path, canned=status_route(), serve_config={}) as fake:
        code, text = run(settings, fake.api)
        posts = [seen for seen in fake.requests if seen.method == "POST"]
        again, second = run(settings, fake.api)
        assert [seen for seen in fake.requests if seen.method == "POST"] == posts
    assert (code, again) == (0, 0)
    assert len(posts) == 1
    assert f"https://{NODE}/" in text
    assert "█" in text or "▀" in text or "▄" in text
    assert f"Take it down with: {TEARDOWN_COMMAND}" in text
    assert "already served" in second
    assert "warning" not in text


def test_the_teardown_is_the_narrow_spelling_the_cli_itself_prints() -> None:
    # tailscale's own hint is "tailscale serve --https=443 off"; reset would drop every handler.
    assert TEARDOWN_COMMAND == "tailscale serve --https=443 off"
    assert "reset" not in TEARDOWN_COMMAND


def test_pair_will_not_take_over_another_handler_and_prints_no_qr(tmp_path: Path) -> None:
    taken = {
        "TCP": {"443": {"HTTPS": True}},
        "Web": {f"{NODE}:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8770"}}}},
    }
    with tailscaled(tmp_path, canned=status_route(), serve_config=taken) as fake:
        code, text = run(Settings(state_dir=tmp_path / "state"), fake.api)
        assert fake.serve_config == taken
        assert not [seen for seen in fake.requests if seen.method == "POST"]
    assert code == 1
    assert "8770" in text
    assert "https://" + NODE + "/\n" not in text


def test_check_reports_and_writes_nothing(tmp_path: Path) -> None:
    with tailscaled(tmp_path, canned=status_route(), serve_config={}) as fake:
        code, text = run(Settings(state_dir=tmp_path / "state"), fake.api, publish=False)
        assert fake.serve_config == {}
        assert not [seen for seen in fake.requests if seen.method == "POST"]
    assert code == 1
    assert "nothing changed" in text


def test_an_unpinned_login_is_warned_about(tmp_path: Path) -> None:
    with tailscaled(tmp_path, canned=status_route(), serve_config={}) as fake:
        _, text = run(Settings(state_dir=tmp_path / "state"), fake.api)
    assert "HP_TAILNET_LOGIN is empty" in text


def test_no_tailscaled_is_an_actionable_failure_from_the_command_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HP_TAILSCALED_SOCKET", str(tmp_path / "absent.sock"))
    monkeypatch.setenv("HP_STATE_DIR", str(tmp_path / "state"))
    assert main(["pair"]) == 1
