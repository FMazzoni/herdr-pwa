import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> Path:
    # No test may reach the real ~/.config, nor inherit the operator's HP_* settings.
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    for name in [key for key in os.environ if key.startswith("HP_")]:
        monkeypatch.delenv(name)
    return home
