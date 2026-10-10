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
