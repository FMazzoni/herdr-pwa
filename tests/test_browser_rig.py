"""One browser job per machine: every launch queues on the gate's lock, and a dead worker is said."""

import fcntl
import io
import json
import logging
import subprocess
import sys
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import ClassVar

import pytest
from starlette.applications import Starlette
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse
from starlette.routing import Route
from starlette.testclient import TestClient

# The browser scripts import each other as top-level modules, as they do when run directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from scripts import geometry, shots
from tests.catalogue import STATES
from tests.waiting import eventually, holds

# Long enough that a launch which ignored the lock would have started inside it.
QUEUED = 0.5
CEILING = 10.0


class _StopError(Exception):
    """Raised where a browser would have started, so nothing real is launched."""


def _rig() -> ModuleType:
    """The firefox module the scripts import, which is not the scripts.firefox copy."""
    return sys.modules["firefox"]


@pytest.fixture
def lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "geometry.lock"
    monkeypatch.setattr(_rig(), "LOCK", path)
    monkeypatch.delenv("HP_BROWSER_LOCK_FD", raising=False)
    return path


@contextmanager
def _other_job(path: Path) -> Generator[None]:
    """Another browser job holding the lock, as a second run on the machine would."""
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.write("pid 4242 (scripts/shots.py idle) in /elsewhere\n")
        handle.flush()
        yield
        handle.truncate(0)


def _queues_behind(path: Path, launched: threading.Event, start: Callable[[], None]) -> None:
    """``start`` must not launch while another job holds the lock, and must once it lets go."""
    failures: list[BaseException] = []

    def go() -> None:
        try:
            start()
        except _StopError:
            pass
        except BaseException as exc:  # noqa: BLE001 - carried to the test thread, which fails on it
            failures.append(exc)

    with _other_job(path):
        thread = threading.Thread(target=go, daemon=True)
        thread.start()
        assert holds(lambda: not launched.is_set(), QUEUED), "launched beside another job"
    assert eventually(launched.is_set, CEILING), "never launched after the lock was let go"
    thread.join(CEILING)
    assert not failures, failures


def test_a_firefox_launch_waits_for_the_job_holding_the_lock(
    lock: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launched = threading.Event()

    def popen(*_args: object, **_kwargs: object) -> None:
        launched.set()
        raise _StopError

    monkeypatch.setattr(subprocess, "Popen", popen)

    def start() -> None:
        with geometry.browser():
            pass

    _queues_behind(lock, launched, start)


def test_a_webkit_launch_for_shots_waits_for_the_job_holding_the_lock(
    lock: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launched = threading.Event()

    def playwright() -> None:
        launched.set()
        raise _StopError

    monkeypatch.setattr(shots, "sync_playwright", playwright)

    def start() -> None:
        with shots.webkit():
            pass

    _queues_behind(lock, launched, start)


def test_a_direct_geometry_run_holds_the_lock_over_all_its_workers(
    lock: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launched = threading.Event()

    def drive(_work: object, _jobs: int) -> geometry.Tally:
        launched.set()
        raise _StopError

    monkeypatch.setattr(geometry, "drive", drive)

    monkeypatch.setattr(sys, "argv", ["geometry.py", "--only", geometry.HISTORY])

    def start() -> None:
        geometry.main()

    # A direct run quietens the app's logger; it must not stay quiet for the next test's caplog.
    logger = logging.getLogger("herdr_pwa")
    level = logger.level
    try:
        _queues_behind(lock, launched, start)
    finally:
        logger.setLevel(level)


def test_the_waiting_job_is_told_who_holds_the_lock(
    lock: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    launched = threading.Event()

    def playwright() -> None:
        launched.set()
        raise _StopError

    monkeypatch.setattr(shots, "sync_playwright", playwright)

    def start() -> None:
        with shots.webkit():
            pass

    _queues_behind(lock, launched, start)
    said = capsys.readouterr().err
    assert "pid 4242 (scripts/shots.py idle) in /elsewhere" in said


def test_a_job_and_the_workers_it_starts_hold_the_lock_once(lock: Path) -> None:
    fds: list[int] = []
    # Nested, as the gate's run enters geometry's: the job already holds it, so no wait.
    with geometry.held(), geometry.held():
        fds += geometry.inherited()
    assert len(fds) == 1
    # Released: a job anywhere else on the machine can take it now.
    with lock.open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)


class _Pipe(io.StringIO):
    """A worker's stdin: what was written, never read back."""


class _Worker:
    """A stand-in geometry worker: answers each task, or dies before answering when told to."""

    deaths: ClassVar[list[bool]] = []
    spawned: int = 0

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        type(self).spawned += 1
        self.stdin = _Pipe()
        self.stdout = self
        self.returncode: int | None = None

    # drive() annotates its workers as Popen[str], which is evaluated where it is defined.
    def __class_getitem__(cls, _item: object) -> type["_Worker"]:
        return cls

    def readline(self) -> str:
        if self.deaths and self.deaths.pop(0):
            self.returncode = -9
            return ""
        return json.dumps({"problems": [], "used": {}, "taps": 3}) + "\n"

    def wait(self) -> int:
        self.returncode = self.returncode if self.returncode is not None else 0
        return self.returncode

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9


def _drive(monkeypatch: pytest.MonkeyPatch, deaths: list[bool]) -> tuple[geometry.Tally, int]:
    class Worker(_Worker):
        pass

    Worker.deaths = deaths
    monkeypatch.setattr(subprocess, "Popen", Worker)
    task: geometry.Task = {"kind": "walk", "module": geometry.HISTORY}
    return geometry.drive([task], 1), Worker.spawned


def test_a_worker_that_dies_has_its_task_run_once_more_and_the_retry_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tally, spawned = _drive(monkeypatch, [True, False])
    assert tally.problems == []
    assert tally.taps == 3
    assert spawned == 2
    assert tally.retries == [f"walk {geometry.HISTORY}"]


def test_a_task_whose_worker_dies_twice_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    tally, spawned = _drive(monkeypatch, [True, True])
    assert spawned == 2
    assert any("died" in p and geometry.HISTORY in p for p in tally.problems), tally.problems


class _Driver(geometry.Marionette):
    """A browser that is never started; the states' own runs are stood in for."""

    def __init__(self) -> None:
        pass

    def send(self, command: str, params: dict[str, object]) -> object:
        del command, params
        return None

    def handles(self) -> list[str]:
        return ["opener"]


def test_one_state_whose_browser_fails_reports_itself_and_the_rest_of_its_chunk_still_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chunk = [s.name for s in STATES[:4]]
    bad = chunk[1]
    measured: list[str] = []

    def run_state(*args: object) -> None:
        state = args[1]
        assert isinstance(state, geometry.State)
        if state.name == bad:
            raise geometry.BrowserError("getComputedStyle: Argument 1 is not an object")
        measured.append(state.name)

    def phone_window(*_args: object) -> str:
        return "phone"

    def install(*_args: object) -> list[str]:
        return []

    monkeypatch.setattr(geometry, "phone_window", phone_window)
    monkeypatch.setattr(geometry, "install", install)
    monkeypatch.setattr(geometry, "run_state", run_state)
    task: geometry.Task = {"kind": "states", "width": 390, "scale": 1.0, "names": chunk}
    switch = geometry.Switch()
    bench = geometry.Bench(_Driver(), "http://x", switch.mount, tmp_path)
    tally = geometry._do(task, bench, switch)  # pyright: ignore[reportPrivateUsage]
    assert measured == [n for n in chunk if n != bad]
    assert len(tally.problems) == 1
    assert f"390px {bad}" in tally.problems[0]
    assert "Argument 1 is not an object" in tally.problems[0]


# Over the gzip floor, so the page comes back compressed as the real app sends it.
_PAGE = (
    '<!doctype html>\n<html><head><meta charset="utf-8">'
    '<script src="/static/app.js" defer></script></head><body>' + "x" * 2000 + "</body></html>"
)
_FRAGMENT = '<div id="card">' + "y" * 2000 + "</div>"


def _site() -> tuple[Starlette, list[str]]:
    """A stand-in app that serves a whole page and a fragment, and says which paths it was asked."""
    asked: list[str] = []

    async def page(request: Request) -> HTMLResponse:
        asked.append(request.url.path)
        return HTMLResponse(_PAGE)

    async def fragment(request: Request) -> HTMLResponse:
        asked.append(request.url.path)
        return HTMLResponse(_FRAGMENT)

    app = Starlette(routes=[Route("/", page), Route("/fragment", fragment)])
    app.add_middleware(GZipMiddleware, minimum_size=500)
    return app, asked


def _switched() -> tuple[geometry.Switch, TestClient, list[str]]:
    switch = geometry.Switch()
    app, asked = _site()
    switch.mount(app)
    return switch, TestClient(switch), asked


def test_a_whole_page_runs_the_error_collector_before_any_script_of_its_own() -> None:
    _, client, _ = _switched()
    page = client.get("/", headers={"Accept-Encoding": "gzip"})
    head = page.text.split("</head>")[0]
    collector = head.find(f'<script src="{geometry.COLLECTOR_PATH}"></script>')
    assert 0 <= collector < head.find("/static/app.js")
    assert page.text.endswith("</body></html>")
    script = client.get(geometry.COLLECTOR_PATH)
    assert script.headers["content-type"].startswith("text/javascript")
    assert "unhandledrejection" in script.text


def test_a_fragment_is_passed_through_as_the_app_sent_it() -> None:
    _, client, _ = _switched()
    fragment = client.get("/fragment", headers={"Accept-Encoding": "gzip"})
    assert fragment.headers["content-encoding"] == "gzip"
    assert fragment.text == _FRAGMENT


def test_a_reported_page_error_is_read_back_once_with_its_message_and_source() -> None:
    switch, client, asked = _switched()
    error = {
        "kind": "error",
        "message": "ReferenceError: armTimers is not defined",
        "source": "http://x/static/chatend.js:253:5",
        "page": "http://x/agent/1",
    }
    reply = client.post(geometry.REPORT_PATH, json=error)
    assert reply.status_code == 204
    assert asked == []
    assert switch.writing == 0
    (said,) = switch.drain()
    assert "ReferenceError: armTimers is not defined" in said
    assert "chatend.js:253:5" in said
    assert "/agent/1" in said
    assert switch.drain() == []


def test_a_page_error_fails_the_state_and_the_walk_it_was_thrown_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    switch = geometry.Switch()
    thrown = {"kind": "error", "message": "boom", "source": "s.js:1:1", "page": "http://x/"}

    def run_state(*_args: object) -> None:
        switch.errors.append(thrown)

    def walk(*_args: object) -> list[str]:
        switch.errors.append(thrown)
        return []

    def phone_window(*_args: object) -> str:
        return "phone"

    def install(*_args: object) -> list[str]:
        return []

    monkeypatch.setattr(geometry, "phone_window", phone_window)
    monkeypatch.setattr(geometry, "install", install)
    monkeypatch.setattr(geometry, "run_state", run_state)
    monkeypatch.setattr(geometry.backnav, "run", walk)
    bench = geometry.Bench(_Driver(), "http://x", switch.mount, tmp_path)
    name = STATES[0].name
    states: geometry.Task = {"kind": "states", "width": 390, "scale": 1.0, "names": [name]}
    tally = geometry._do(states, bench, switch)  # pyright: ignore[reportPrivateUsage]
    assert len(tally.problems) == 1
    assert f"390px {name}" in tally.problems[0]
    assert "boom" in tally.problems[0]
    walked: geometry.Task = {"kind": "walk", "module": geometry.HISTORY}
    tally = geometry._do(walked, bench, switch)  # pyright: ignore[reportPrivateUsage]
    assert len(tally.problems) == 1
    assert f"walk {geometry.HISTORY}" in tally.problems[0]
    assert "s.js:1:1" in tally.problems[0]


def test_one_throw_on_many_page_loads_is_said_once_with_how_many() -> None:
    switch = geometry.Switch()
    for path in ("/", "/agent/1", "/files"):
        switch.errors.append(
            {"kind": "error", "message": "boom", "source": "s.js:1:1", "page": path}
        )
    switch.errors.append(
        {"kind": "unhandledrejection", "message": "nope", "source": "?", "page": "/"}
    )
    said = switch.drain()
    assert said == [
        "uncaught error: boom at s.js:1:1, on / and 2 more page loads",
        "uncaught unhandledrejection: nope at ?, on /",
    ]
