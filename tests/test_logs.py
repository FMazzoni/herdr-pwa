import io
import logging
import logging.config
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from herdr_pwa import cli, logs
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
    assert shown.endswith("failed: FileNotFoundError")
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
    assert shown.endswith("FileNotFoundError")


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
    assert shown.rstrip().endswith("FileNotFoundError")
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
