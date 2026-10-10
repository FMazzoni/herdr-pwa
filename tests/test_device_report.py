"""The debug-only device report: absent when off, a gated 0600 JSONL sink when on."""

import json
import stat
from pathlib import Path

import pytest

from herdr_pwa import cli, devreport, jsonio
from herdr_pwa.settings import Settings
from tests.webfakes import IDENTITY, build_rig

SAME_ORIGIN = {**IDENTITY, "Origin": "http://testserver"}
SCRIPT = "/static/devreport.js"


def _batch(session: str = "abc-123", kind: str = "load") -> dict[str, object]:
    return {"session": session, "page": 1, "seq": 1, "events": [{"t": 0, "page": 1, "kind": kind}]}


def test_off_by_default_has_no_route_and_no_script(tmp_path: Path) -> None:
    rig = build_rig(tmp_path)
    assert rig.settings.device_report is False
    assert SCRIPT not in rig.http.get("/").text
    answer = rig.http.post("/device-report", headers=SAME_ORIGIN, json=_batch())
    assert answer.status_code == 404
    assert not (rig.settings.state_dir / devreport.REPORT_FILENAME).exists()


def test_on_links_the_script_and_appends_a_0600_line(tmp_path: Path) -> None:
    rig = build_rig(tmp_path, device_report=True)
    assert SCRIPT in rig.http.get("/").text
    answer = rig.http.post("/device-report", headers=SAME_ORIGIN, json=_batch())
    assert answer.status_code == 204
    _ = rig.http.post("/device-report", headers=SAME_ORIGIN, json=_batch(kind="pagehide"))
    path = rig.settings.state_dir / devreport.REPORT_FILENAME
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    lines = path.read_text().splitlines()
    assert len(lines) == 2
    first = jsonio.as_dict(jsonio.loads(lines[0]))
    assert first is not None
    assert first["session"] == "abc-123"
    assert "received" in first


@pytest.mark.parametrize(
    ("content", "headers", "status"),
    [
        (b"x" * (70 * 1024), {"Content-Type": "application/json"}, 413),
        (b"not json", {"Content-Type": "application/json"}, 400),
        (b"[1, 2]", {"Content-Type": "application/json"}, 400),
        (json.dumps(_batch()).encode(), {"Content-Type": "text/plain"}, 415),
        (json.dumps(_batch(session="../bad")).encode(), {"Content-Type": "application/json"}, 400),
    ],
)
def test_on_refuses_what_is_not_a_report(
    tmp_path: Path, content: bytes, headers: dict[str, str], status: int
) -> None:
    rig = build_rig(tmp_path, device_report=True)
    answer = rig.http.post("/device-report", headers={**SAME_ORIGIN, **headers}, content=content)
    assert answer.status_code == status
    assert not (rig.settings.state_dir / devreport.REPORT_FILENAME).exists()


def test_on_refuses_no_identity_and_a_foreign_origin(tmp_path: Path) -> None:
    anonymous = build_rig(tmp_path, headers={}, device_report=True)
    answer = anonymous.http.post(
        "/device-report", headers={"Origin": "http://testserver"}, json=_batch()
    )
    assert answer.status_code == 401
    rig = build_rig(tmp_path / "foreign", device_report=True)
    foreign = {**IDENTITY, "Origin": "https://evil.example"}
    assert rig.http.post("/device-report", headers=foreign, json=_batch()).status_code == 403
    assert rig.http.post("/device-report", headers=IDENTITY, json=_batch()).status_code == 403
    for state in (anonymous.settings.state_dir, rig.settings.state_dir):
        assert not (state / devreport.REPORT_FILENAME).exists()


def test_cli_prints_the_latest_session_in_order(tmp_path: Path) -> None:
    store = devreport.ReportStore(tmp_path, clock=lambda: "2026-10-10T00:00:00.000Z")
    store.append(_batch(session="old"))
    store.append(_batch(session="new", kind="load"))
    store.append(_batch(session="new", kind="popstate"))
    out: list[str] = []
    assert cli.device_report(Settings(state_dir=tmp_path), write=out.append) == 0
    text = "".join(out)
    assert text.startswith("session new: 2 event(s)")
    assert text.index("load") < text.index("popstate")
    named: list[str] = []
    _ = cli.device_report(Settings(state_dir=tmp_path), "old", write=named.append)
    assert "session old: 1 event(s)" in "".join(named)


def test_cli_says_when_there_is_nothing(tmp_path: Path) -> None:
    out: list[str] = []
    _ = cli.device_report(Settings(state_dir=tmp_path), write=out.append)
    assert "no device report" in "".join(out)
