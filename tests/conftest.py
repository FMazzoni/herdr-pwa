import os
from pathlib import Path

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    # Read at startup, before the isolation fixture below strips HP_* from every test.
    parser.addoption(
        "--foreign-root",
        default=os.environ.get("HP_FOREIGN_CAPTURES"),
        help="directory holding the pinned foreign capture clones (default $HP_FOREIGN_CAPTURES)",
    )


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
