from importlib.metadata import version
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from herdr_pwa import cli
from herdr_pwa.settings import Settings
from herdr_pwa.web.app import create_app
from herdr_pwa.web.seams import Seams


def test_health_answers(tmp_path: Path) -> None:
    settings = Settings(socket_path=tmp_path / "no-runner.sock", state_dir=tmp_path)
    response = TestClient(create_app(settings, Seams())).get("/healthz")
    assert response.json() == {"status": "ok"}


def test_version_is_the_installed_distributions(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exited:
        cli.main(["--version"])
    assert exited.value.code == 0
    assert capsys.readouterr().out.strip() == f"herdr-pwa {version('herdr-pwa')}"


def test_every_subcommand_says_what_it_does(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    listed = capsys.readouterr().out
    for command in ("serve", "doctor", "pair", "device-report", "ping"):
        # argparse lists each subcommand on its own line, its help after it.
        words = next(row.split() for row in listed.splitlines() if row.split()[:1] == [command])
        assert len(words) > 1, f"{command} has no help"
