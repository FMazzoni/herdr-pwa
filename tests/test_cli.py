from pathlib import Path

from fastapi.testclient import TestClient

from herdr_pwa.settings import Settings
from herdr_pwa.web.app import create_app
from herdr_pwa.web.seams import Seams


def test_health_answers(tmp_path: Path) -> None:
    settings = Settings(socket_path=tmp_path / "no-runner.sock", state_dir=tmp_path)
    response = TestClient(create_app(settings, Seams())).get("/healthz")
    assert response.json() == {"status": "ok"}
