import asyncio
import errno
import gc
import io
import logging
import logging.config
import sys
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from herdr_pwa import cli, logs
from herdr_pwa.push import VapidKeyError
from herdr_pwa.settings import Settings
from herdr_pwa.web.seams import Seams

PRIVATE_PATH = "/home/user/secret.json"


def _record(
    arg: BaseException | Path | str, exc_info: logs.ExcInfo | None = None
) -> logging.LogRecord:
    return logging.LogRecord(
        "herdr_pwa.test", logging.ERROR, __file__, 1, "failed: %s", (arg,), exc_info
    )


def _rendered(record: logging.LogRecord) -> str:
    assert logs.ScrubFilter().filter(record)
    return logs.ScrubFormatter("%(levelprefix)s %(message)s", use_colors=False).format(record)


def _raise_oserror() -> None:
    raise FileNotFoundError(2, "No such file or directory", PRIVATE_PATH)


def _raise_chained() -> None:
    try:
        _raise_oserror()
    except OSError as exc:
        raise RuntimeError(PRIVATE_PATH) from exc


def _caught(raiser: Callable[[], None]) -> logs.ExcInfo:
    try:
        raiser()
    except Exception:  # noqa: BLE001 - each test names the class it expects
        return sys.exc_info()
    raise AssertionError(raiser.__name__)


def test_an_exception_argument_is_logged_as_its_class() -> None:
    shown = _rendered(_record(_caught(_raise_oserror)[1] or "none"))
    assert shown.endswith("failed: FileNotFoundError(ENOENT)")
    assert "secret" not in shown


def test_a_path_argument_is_logged_as_its_name() -> None:
    shown = _rendered(_record(Path(PRIVATE_PATH)))
    assert shown.endswith("failed: secret.json")
    assert "/home/" not in shown


def test_a_mapping_argument_is_scrubbed_too() -> None:
    record = logging.LogRecord("t", logging.ERROR, __file__, 1, "%(error)s", None, None)
    record.args = {"error": ValueError(PRIVATE_PATH)}
    assert _rendered(record).endswith("ValueError")


def test_the_unconfigured_formatter_prints_the_message_this_one_drops() -> None:
    record = _record("x", _caught(_raise_oserror))
    # The old shape: the standard formatter writes the message, path and all.
    assert PRIVATE_PATH in logging.Formatter().format(record)
    shown = _rendered(record)
    assert PRIVATE_PATH not in shown
    assert "secret" not in shown
    assert 'File "test_logs.py", line' in shown
    assert "in _raise_oserror" in shown
    assert shown.endswith("FileNotFoundError(ENOENT)")


def test_a_chained_exception_shows_both_classes_and_no_message() -> None:
    shown = _rendered(_record("x", _caught(_raise_chained)))
    assert "FileNotFoundError" in shown
    assert "direct cause" in shown
    assert shown.endswith("RuntimeError")
    assert "secret" not in shown


def test_no_frame_names_an_absolute_file() -> None:
    shown = _rendered(_record("x", _caught(_raise_oserror)))
    assert str(Path(__file__).parent) not in shown


def test_a_stack_keeps_only_short_file_names() -> None:
    stack = f'  File "{Path.home()}/x/site-packages/uvicorn/main.py", line 3, in run'
    assert logs.scrubbed_stack(stack) == '  File "uvicorn/main.py", line 3, in run'


@pytest.mark.parametrize("name", ["herdr_pwa.test", "uvicorn.error"])
def test_configured_loggers_write_through_the_scrubbing_formatter(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Conftest restores the root and uvicorn loggers after each test, so this config never leaks.
    stream = io.StringIO()
    monkeypatch.setattr("sys.stderr", stream)
    logging.config.dictConfig(logs.logging_config())
    try:
        _raise_oserror()
    except OSError:
        logging.getLogger(name).exception("serving failed")
    shown = stream.getvalue()
    assert shown.startswith("ERROR:    serving failed")
    assert shown.rstrip().endswith("FileNotFoundError(ENOENT)")
    assert "secret" not in shown


def _never_serves(*_args: object, **_kwargs: object) -> None:
    return None


def test_an_unlearned_host_logs_the_class_not_the_socket_path(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli.uvicorn, "run", _never_serves)
    settings = Settings(
        state_dir=tmp_path,
        tailscaled_socket=tmp_path / "tailscaled.sock",
        allow_any_tailnet_user=True,
        push_enabled=False,
    )
    cli.serve(settings, 0, Seams())
    shown = caplog.text
    assert "LocalApiError" in shown
    assert str(tmp_path) not in shown


def _configured(monkeypatch: pytest.MonkeyPatch) -> io.StringIO:
    # Conftest restores the loggers and excepthooks after each test, so none of this leaks.
    stream = io.StringIO()
    monkeypatch.setattr("sys.stderr", stream)
    logging.config.dictConfig(logs.logging_config())
    return stream


async def _fail() -> None:
    raise ValueError(PRIVATE_PATH)


def test_an_unretrieved_task_exception_keeps_asyncios_first_line_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _configured(monkeypatch)
    loop = asyncio.new_event_loop()
    try:
        task = loop.create_task(_fail())
        waited = loop.run_until_complete(asyncio.wait([task]))
        assert task.done()
        # The wait's result holds the task too; the warning comes when the last reference goes.
        del task, waited
        _ = gc.collect()
    finally:
        loop.close()
    shown = stream.getvalue()
    assert "Task exception was never retrieved" in shown
    assert shown.rstrip().endswith("ValueError")
    assert "secret" not in shown
    assert str(Path(__file__).parent) not in shown


def test_exceptions_and_paths_nested_in_containers_are_scrubbed() -> None:
    record = logging.LogRecord("t", logging.ERROR, __file__, 1, "%s %s %s", None, None)
    record.args = (
        [ValueError(PRIVATE_PATH)],
        {"where": Path(PRIVATE_PATH)},
        {(KeyError(PRIVATE_PATH),)},
    )
    shown = _rendered(record)
    assert shown.endswith("['ValueError'] {'where': 'secret.json'} [['KeyError']]")


def test_a_stack_drops_its_source_lines() -> None:
    stack = (
        "Stack (most recent call last):\n"
        f'  File "{Path.home()}/x/herdr_pwa/cli.py", line 3, in main\n'
        f'    open("{PRIVATE_PATH}")'
    )
    assert logs.scrubbed_stack(stack) == (
        'Stack (most recent call last):\n  File "herdr_pwa/cli.py", line 3, in main'
    )


def _raise_addr_in_use() -> None:
    raise OSError(errno.EADDRINUSE, "Address already in use")


def test_an_oserror_keeps_its_errno_symbol(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = _configured(monkeypatch)
    exc_info = _caught(_raise_addr_in_use)
    # Uvicorn logs a failed bind as the exception itself.
    logging.getLogger("uvicorn.error").error(exc_info[1])
    logging.getLogger("uvicorn.error").error("bind", exc_info=exc_info)
    shown = stream.getvalue()
    assert "ERROR:    OSError(EADDRINUSE)\n" in shown
    assert shown.rstrip().endswith("\nOSError(EADDRINUSE)")
    assert "already in use" not in shown


def _raise_in_thread() -> None:
    raise ValueError(PRIVATE_PATH)


def test_uncaught_exceptions_go_through_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = _configured(monkeypatch)
    logs.install_excepthooks()
    exc_info = _caught(_raise_chained)
    assert exc_info[0] is not None
    sys.excepthook(*exc_info)
    thread = threading.Thread(target=_raise_in_thread, name="worker")
    thread.start()
    thread.join()
    shown = stream.getvalue()
    assert "CRITICAL: uncaught exception\n" in shown
    assert "ERROR:    uncaught exception in worker\n" in shown
    assert "in _raise_in_thread" in shown
    assert "secret" not in shown


def _refuse_with_a_path(*_args: object, **_kwargs: object) -> None:
    try:
        _raise_oserror()
    except OSError as exc:
        raise VapidKeyError(f"cannot use {PRIVATE_PATH}: {exc}") from exc


def test_a_serve_refusal_naming_a_file_is_said_in_fixed_words(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "serve", _refuse_with_a_path)
    assert cli.main(["serve"]) == 1
    said = capsys.readouterr().err
    assert said.startswith("serve: the push key file in the state dir")
    assert "(FileNotFoundError(ENOENT))" in said
    assert "secret" not in said


def _raise_group() -> None:
    try:
        _raise_oserror()
    except OSError as exc:
        raise ExceptionGroup(PRIVATE_PATH, [exc, KeyError(PRIVATE_PATH)]) from None


def test_an_exception_group_shows_each_members_class_and_frames() -> None:
    shown = _rendered(_record("x", _caught(_raise_group)))
    assert "\nExceptionGroup\n" in shown
    assert "  +-- 1 of 2:" in shown
    assert '    File "test_logs.py", line' in shown
    assert "    FileNotFoundError(ENOENT)" in shown
    assert "    KeyError" in shown
    assert "secret" not in shown
