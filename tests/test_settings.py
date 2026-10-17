from pathlib import Path

import pytest

from herdr_pwa.settings import Settings


def test_defaults_stay_off_the_live_port_and_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HP_PORT", raising=False)
    monkeypatch.delenv("HP_STATE_DIR", raising=False)
    settings = Settings()
    assert settings.port == 8772
    assert settings.state_dir.name == "herdr-pwa-v2"


def test_env_overrides(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HP_PORT", "8770")
    monkeypatch.setenv("HP_STATE_DIR", str(tmp_path))
    settings = Settings()
    assert settings.port == 8770
    assert settings.state_dir == tmp_path


def test_access_and_push_defaults_fail_safe() -> None:
    settings = Settings()
    assert settings.state_dir != Path.home() / ".config" / "herdr-pwa"
    assert (settings.tailnet_login, settings.assume_login, settings.push_subject) == ("", "", "")
    assert (settings.audit_max_bytes, settings.audit_payload_chars) == (5 * 1024 * 1024, 512)
    assert settings.push_ledger_max_age == 900


def test_logins_and_the_contact_are_trimmed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HP_TAILNET_LOGIN", "  you@example.com \n")
    monkeypatch.setenv("HP_PUSH_SUBJECT", " mailto:you@example.org ")
    settings = Settings()
    assert settings.tailnet_login == "you@example.com"
    assert settings.push_subject == "mailto:you@example.org"


@pytest.mark.parametrize(
    ("typed", "declared"),
    [
        ("", ()),
        ("  ", ()),
        ("opencode/big-pickle", ("opencode/big-pickle",)),
        (
            " opencode/big-pickle , anthropic/claude-sonnet-4-5,",
            ("opencode/big-pickle", "anthropic/claude-sonnet-4-5"),
        ),
    ],
)
def test_opencode_models_are_a_comma_separated_list(
    monkeypatch: pytest.MonkeyPatch, typed: str, declared: tuple[str, ...]
) -> None:
    monkeypatch.setenv("HP_OPENCODE_MODELS", typed)
    assert Settings().opencode_models == declared


def test_no_opencode_models_are_declared_unless_the_operator_says() -> None:
    assert Settings().opencode_models == ()


@pytest.mark.parametrize("typed", ["big pickle", "-v", "a;rm -rf /", '["a"]'])
def test_an_opencode_model_that_could_never_be_launched_stops_the_start(
    monkeypatch: pytest.MonkeyPatch, typed: str
) -> None:
    monkeypatch.setenv("HP_OPENCODE_MODELS", typed)
    with pytest.raises(ValueError, match="opencode_models"):
        _ = Settings()
