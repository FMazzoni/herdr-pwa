from fastapi.testclient import TestClient

from herdr_pwa.cli import create_app


def test_health_answers() -> None:
    response = TestClient(create_app()).get("/healthz")
    assert response.json() == {"status": "ok"}
