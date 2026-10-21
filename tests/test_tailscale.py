import ast
import inspect
import os
from pathlib import Path

import pytest

from herdr_pwa import auth, pairing, tailscale
from herdr_pwa.settings import Settings
from herdr_pwa.tailscale import STATUS_PATH, LocalApi, LocalApiError, magicdns
from tests.fakes import GOOD_STATUS, NODE, Canned, status_route, tailscaled


def test_magicdns_strips_the_trailing_dot_and_reads_only_status(tmp_path: Path) -> None:
    with tailscaled(tmp_path, canned=status_route()) as fake:
        assert magicdns(fake.api) == NODE
        assert [(r.method, r.path) for r in fake.requests] == [("GET", STATUS_PATH)]
        # LocalAPI's CSRF guard: anything else is refused by tailscaled.
        assert fake.requests[0].headers["host"] == "local-tailscaled.sock"


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ({**GOOD_STATUS, "BackendState": "Stopped"}, "not running"),
        ({**GOOD_STATUS, "BackendState": "NeedsLogin"}, "not running"),
        ({"BackendState": "Running"}, "Self"),
        ({**GOOD_STATUS, "Self": "nope"}, "Self"),
        ({**GOOD_STATUS, "Self": {"DNSName": ""}}, "MagicDNS"),
        ({**GOOD_STATUS, "Self": {"DNSName": "."}}, "MagicDNS"),
        ("not json", "not JSON"),
        ("[1]", "not a JSON object"),
    ],
)
def test_a_status_without_a_usable_name_is_an_actionable_error(
    tmp_path: Path, document: object, message: str
) -> None:
    with (
        tailscaled(tmp_path, canned=status_route(document)) as fake,
        pytest.raises(LocalApiError, match=message),
    ):
        _ = magicdns(fake.api)


@pytest.mark.parametrize(
    ("status", "message"), [(403, "operator"), (401, "operator"), (500, "HTTP 500")]
)
def test_an_error_status_is_named(tmp_path: Path, status: int, message: str) -> None:
    with (
        tailscaled(tmp_path, canned=status_route(status=status)) as fake,
        pytest.raises(LocalApiError, match=message),
    ):
        _ = magicdns(fake.api)


def test_a_missing_socket_says_install_tailscale(tmp_path: Path) -> None:
    with pytest.raises(LocalApiError, match="Install Tailscale"):
        _ = magicdns(LocalApi(tmp_path / "missing.sock"))


def test_a_refused_socket_says_start_tailscaled(tmp_path: Path) -> None:
    with tailscaled(tmp_path) as fake:
        path = fake.socket_path
    path.touch(exist_ok=True)
    with pytest.raises(LocalApiError, match="Nothing is listening"):
        _ = magicdns(LocalApi(path))


def test_a_hung_daemon_times_out(tmp_path: Path) -> None:
    canned = {("GET", STATUS_PATH): Canned(hang=True)}
    with (
        tailscaled(tmp_path, canned=canned) as fake,
        pytest.raises(LocalApiError, match="Timed out"),
    ):
        _ = magicdns(LocalApi(fake.socket_path, timeout=0.1))


def test_garbage_on_the_wire_is_an_error_not_a_traceback(tmp_path: Path) -> None:
    canned = {("GET", STATUS_PATH): Canned(raw=b"this is not http\r\n\r\n")}
    with tailscaled(tmp_path, canned=canned) as fake, pytest.raises(LocalApiError):
        _ = magicdns(fake.api)


def test_permission_denied_names_the_operator_fix(tmp_path: Path) -> None:
    with tailscaled(tmp_path, canned=status_route()) as fake:
        fake.socket_path.chmod(0o000)
        if os.access(fake.socket_path, os.W_OK):
            pytest.skip("running as root: socket permissions are not enforced")
        with pytest.raises(LocalApiError, match="operator"):
            _ = magicdns(fake.api)
        fake.socket_path.chmod(0o600)


def test_the_default_socket_is_tailscaleds_and_no_test_reaches_it() -> None:
    assert Settings().tailscaled_socket == Path("/var/run/tailscale/tailscaled.sock")


ACCESS_MODULES = (tailscale, auth, pairing)


def test_nothing_shells_out_or_reaches_the_cloud_api() -> None:
    for module in ACCESS_MODULES:
        source = inspect.getsource(module)
        for word in ("subprocess", "os.system", "api.tailscale.com", "requests"):
            assert word not in source, f"{module.__name__}: {word}"


def test_pairing_mints_no_token() -> None:
    for module in ACCESS_MODULES:
        source = inspect.getsource(module)
        assert "secrets" not in source, module.__name__
        assert "token_" not in source, module.__name__


def _method_literals(module: object) -> list[tuple[str, str]]:
    # (function, method) for every .call/.exchange whose first argument is a literal.
    found: list[tuple[str, str]] = []
    tree = ast.parse(inspect.getsource(module))  # pyright: ignore[reportArgumentType]
    for func in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)):
        for node in ast.walk(func):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr in {"call", "exchange"} and node.args:
                first = node.args[0]
                method = first.value if isinstance(first, ast.Constant) else "<variable>"
                found.append((func.name, str(method)))
    return found


def test_exactly_one_function_writes_and_its_method_and_path_are_literal() -> None:
    calls = [call for module in ACCESS_MODULES for call in _method_literals(module)]
    writers = [call for call in calls if call[1] != "GET"]
    # tailscale.LocalApi.call forwards its own argument; it is the transport, not a writer.
    assert writers == [("call", "<variable>"), ("_write_serve_config", "POST")]
    source = inspect.getsource(pairing._write_serve_config)  # pyright: ignore[reportPrivateUsage]
    assert '"/localapi/v0/serve-config"' in source
