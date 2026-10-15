import os
from pathlib import Path

import pytest

from herdr_pwa.writes import prompt, resolve
from tests._foreign import default_root


def pytest_addoption(parser: pytest.Parser) -> None:
    # Read at startup, before the isolation fixture below strips HP_* from every test.
    parser.addoption(
        "--foreign-root",
        default=str(default_root()),
        help="directory holding the pinned foreign capture clones "
        "(default $HP_FOREIGN_CAPTURES, else ~/.cache/herdr-pwa/foreign-captures)",
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


@pytest.fixture(autouse=True)
def _fresh_write_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    # Which writes are in flight, and which may have landed, is per process; never across tests.
    monkeypatch.setattr(resolve, "IN_FLIGHT", resolve.InFlight())
    monkeypatch.setattr(prompt, "UNSURE", prompt.Unsure())
