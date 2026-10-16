import ast
import inspect
import os
from pathlib import Path

import pytest

from herdr_pwa import auth
from herdr_pwa.auth import (
    Access,
    AssumedLoginUnsafeError,
    AuthError,
    LoginNotAllowedError,
    MissingIdentityError,
    MissingOriginError,
    NonLoopbackHostError,
    OriginMismatchError,
    assert_assumed_login_safe,
    authorize,
    check_same_origin,
    identify,
    serve_is_active,
    unpinned_warning,
)
from herdr_pwa.tailscale import SERVE_CONFIG_PATH, LocalApi
from tests.fakes import NODE, Canned, tailscaled

LOGIN = "you@example.com"
PINNED = Access(tailnet_login=LOGIN)
ASSUMED = Access(assume_login=LOGIN)
HOST = NODE
ORIGIN = f"https://{NODE}"


def through_ingress(**extra: str) -> dict[str, str]:
    return {"Tailscale-User-Login": LOGIN, "Host": HOST, **extra}


def test_a_locally_forged_header_is_accepted_and_the_module_says_so() -> None:
    # tailscale serve proxies from loopback, so a local curl -H is byte-for-byte the ingress.
    assert identify({"Tailscale-User-Login": LOGIN}, PINNED).login == LOGIN
    assert "local uid can forge" in (auth.__doc__ or "")


def test_the_module_does_not_trust_the_peer_or_forwarded_for() -> None:
    source = inspect.getsource(auth)
    for word in ("client.host", "x-forwarded-for", "peername", "is_loopback", "ipaddress"):
        assert word not in source.lower(), word


def test_identity_is_the_header_with_its_name_and_picture() -> None:
    who = identify(
        through_ingress(**{"Tailscale-User-Name": "You", "Tailscale-User-Profile-Pic": "p"}), PINNED
    )
    assert (who.login, who.display_name, who.profile_pic, who.via) == (
        LOGIN,
        "You",
        "p",
        "tailscale",
    )
    assert not who.is_assumed


@pytest.mark.parametrize("value", ["", "   "])
def test_no_identity_is_a_401_and_never_a_read_only_downgrade(value: str) -> None:
    for method in ("GET", "HEAD", "POST"):
        with pytest.raises(MissingIdentityError) as caught:
            _ = authorize(
                {"Tailscale-User-Login": value, "Host": HOST}, access=PINNED, method=method
            )
        assert caught.value.status_code == 401


def test_a_pinned_login_compares_case_insensitively() -> None:
    assert identify({"tailscale-user-login": LOGIN.upper()}, PINNED).login == LOGIN.upper()


def test_another_tailnet_user_is_a_403_and_only_the_operator_learns_who() -> None:
    with pytest.raises(LoginNotAllowedError) as caught:
        _ = identify({"Tailscale-User-Login": "mallory@example.com"}, PINNED)
    assert caught.value.status_code == 403
    assert LOGIN not in str(caught.value)
    assert "mallory@example.com" in caught.value.log_detail
    assert LOGIN in caught.value.log_detail


def test_the_login_comparison_is_constant_time() -> None:
    assert "hmac.compare_digest" in inspect.getsource(auth)


def test_an_empty_pin_admits_any_tailnet_user_and_warns() -> None:
    assert identify({"Tailscale-User-Login": "anyone@x.org"}, Access()).login == "anyone@x.org"
    assert unpinned_warning(Access()) is not None
    assert unpinned_warning(PINNED) is None


@pytest.mark.parametrize(
    ("origin", "host"),
    [
        (ORIGIN, HOST),
        (f"{ORIGIN}:443", HOST),
        ("http://127.0.0.1:8772", "127.0.0.1:8772"),
    ],
)
def test_a_same_origin_write_passes(origin: str, host: str) -> None:
    check_same_origin({"Origin": origin, "Host": host})


@pytest.mark.parametrize(
    ("origin", "host", "error"),
    [
        ("https://evil.example", HOST, OriginMismatchError),
        (f"{ORIGIN}:8443", HOST, OriginMismatchError),
        (f"https://{NODE}.evil.example", HOST, OriginMismatchError),
        ("null", HOST, OriginMismatchError),
        ("not a url", HOST, OriginMismatchError),
        (ORIGIN, "", OriginMismatchError),
        ("", HOST, MissingOriginError),
    ],
)
def test_a_cross_origin_or_unprovable_write_is_refused(
    origin: str, host: str, error: type[AuthError]
) -> None:
    with pytest.raises(error):
        check_same_origin({"Origin": origin, "Host": host})


def test_origin_is_compared_with_host_not_forwarded_host() -> None:
    with pytest.raises(OriginMismatchError):
        check_same_origin(
            {"Origin": "https://evil.example", "Host": HOST, "X-Forwarded-Host": "evil.example"}
        )


def test_the_origin_scheme_must_match_the_forwarded_proto() -> None:
    with pytest.raises(OriginMismatchError):
        check_same_origin({"Origin": f"http://{NODE}", "Host": HOST, "X-Forwarded-Proto": "https"})


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_reads_need_no_origin(method: str) -> None:
    assert authorize(through_ingress(), access=PINNED, method=method).login == LOGIN


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "PROPFIND"])
def test_every_write_method_and_any_unknown_one_needs_an_origin(method: str) -> None:
    with pytest.raises(MissingOriginError):
        _ = authorize(through_ingress(), access=PINNED, method=method)
    assert authorize(through_ingress(Origin=ORIGIN), access=PINNED, method=method).login == LOGIN


def test_identity_is_checked_before_origin() -> None:
    with pytest.raises(MissingIdentityError):
        _ = authorize(
            {"Origin": "https://evil.example", "Host": HOST}, access=PINNED, method="POST"
        )


def test_the_assumed_login_is_a_named_actor_recorded_as_config() -> None:
    who = authorize({"Host": "127.0.0.1:8772"}, access=ASSUMED)
    assert (who.login, who.via, who.is_assumed) == (LOGIN, "config", True)


def test_a_real_header_still_wins_and_is_still_pinned_while_a_login_is_assumed() -> None:
    access = Access(tailnet_login=LOGIN, assume_login=LOGIN)
    with pytest.raises(LoginNotAllowedError):
        _ = identify({"Tailscale-User-Login": "mallory@example.com"}, access)


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.1:8772", "localhost:8772", "[::1]:8772"])
def test_an_assumed_login_works_over_a_literal_loopback_host(host: str) -> None:
    assert authorize({"Host": host}, access=ASSUMED).is_assumed


@pytest.mark.parametrize(
    "host", ["", "evil.example", "evil.example:8772", "10.0.0.1:8772", "127.0.0.1.nip.io"]
)
def test_an_assumed_login_refuses_any_other_host_reads_included(host: str) -> None:
    for method in ("GET", "POST"):
        with pytest.raises(NonLoopbackHostError):
            _ = authorize({"Host": host, "Origin": f"http://{host}"}, access=ASSUMED, method=method)


def test_an_assumed_login_write_is_still_origin_checked() -> None:
    with pytest.raises(OriginMismatchError):
        _ = authorize(
            {"Host": "127.0.0.1:8772", "Origin": "https://evil.example"},
            access=ASSUMED,
            method="POST",
        )


def test_the_pinned_path_is_not_confined_to_loopback() -> None:
    assert authorize(through_ingress(), access=PINNED).login == LOGIN


SERVE_SHAPES: list[tuple[str, object, bool]] = [
    ("empty object", {}, False),
    ("json null", None, False),
    ("known sections empty", {"TCP": {}, "Web": {}, "AllowFunnel": {}}, False),
    ("empty values of every type", {"TCP": {}, "Web": [], "X": "", "Y": None}, False),
    ("an explicit false", {"AllowFunnel": False}, False),
    ("a real serve", {"TCP": {"443": {"HTTPS": True}}, "Web": {f"{NODE}:443": {}}}, True),
    ("a declared port with no handlers", {"TCP": {"443": {}}}, True),
    ("foreground serve", {"Foreground": {"s": {"TCP": {"443": {}}}}}, True),
    ("an unknown section alone", {"SomeFutureKey": {"x": 1}}, True),
    ("mixed: empty known beside unknown", {"TCP": {}, "Web": {}, "FutureProxy": {"n": "x"}}, True),
    ("mixed: unknown list", {"Web": {}, "FutureRoutes": ["a"]}, True),
    ("an unknown scalar", {"FutureMode": 7}, True),
]


@pytest.mark.parametrize(
    ("config", "serving"), [pytest.param(c, s, id=n) for n, c, s in SERVE_SHAPES]
)
def test_serve_detection_counts_every_section_known_or_not(
    tmp_path: Path, config: object, serving: bool
) -> None:
    with tailscaled(tmp_path, serve_config=config) as fake:
        assert serve_is_active(fake.api) is serving
        assert [(r.method, r.path) for r in fake.requests] == [("GET", SERVE_CONFIG_PATH)]


def test_no_socket_and_a_dead_socket_are_the_only_not_serving_failures(tmp_path: Path) -> None:
    assert serve_is_active(LocalApi(tmp_path / "missing.sock")) is False
    dead = tmp_path / "dead.sock"
    with tailscaled(tmp_path) as fake:
        dead = fake.socket_path
    # The socket file remains but nothing listens: connection refused.
    dead.touch(exist_ok=True)
    assert serve_is_active(LocalApi(dead)) is False


def test_only_absent_and_refused_are_swallowed_as_not_serving() -> None:
    tree = ast.parse(inspect.getsource(auth))
    swallowed: set[str] = set()
    serve = {"serve_is_active", "_serve_config_body"}
    funcs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name in serve]
    assert len(funcs) == len(serve)
    for func in funcs:
        for handler in (h for h in ast.walk(func) if isinstance(h, ast.ExceptHandler)):
            body = ast.Module(body=handler.body, type_ignores=[])
            if handler.type is None or any(isinstance(n, ast.Raise) for n in ast.walk(body)):
                continue
            swallowed |= {n.id for n in ast.walk(handler.type) if isinstance(n, ast.Name)}
    assert swallowed == {"FileNotFoundError", "ConnectionRefusedError"}


@pytest.mark.parametrize(
    "canned",
    [
        pytest.param(Canned(status=403, body="{}"), id="403 not the operator"),
        pytest.param(Canned(status=500, body="{}"), id="500"),
        pytest.param(Canned(body=""), id="200 empty"),
        pytest.param(Canned(body="  "), id="200 whitespace"),
        pytest.param(Canned(body="<html>"), id="200 not json"),
        pytest.param(Canned(body='{"TCP":'), id="200 truncated"),
        pytest.param(Canned(body="[1]"), id="200 array"),
        pytest.param(Canned(body="true"), id="200 scalar"),
        pytest.param(Canned(raw=b"garbage\r\n\r\n"), id="not http"),
        pytest.param(Canned(hang=True), id="hung daemon"),
    ],
)
def test_every_undecidable_answer_refuses_an_assumed_login(tmp_path: Path, canned: Canned) -> None:
    with tailscaled(tmp_path, canned={("GET", SERVE_CONFIG_PATH): canned}) as fake:
        api = LocalApi(fake.socket_path, timeout=0.1)
        with pytest.raises(AssumedLoginUnsafeError) as caught:
            assert_assumed_login_safe(ASSUMED, api)
    assert caught.value.status_code == 500


def test_an_unreadable_socket_is_undecidable_not_safe(tmp_path: Path) -> None:
    with tailscaled(tmp_path, serve_config={}) as fake:
        fake.socket_path.chmod(0o000)
        if os.access(fake.socket_path, os.W_OK):
            pytest.skip("running as root: socket permissions are not enforced")
        with pytest.raises(AssumedLoginUnsafeError):
            _ = serve_is_active(fake.api)
        fake.socket_path.chmod(0o600)


def test_an_assumed_login_starts_when_nothing_is_served(tmp_path: Path) -> None:
    with tailscaled(tmp_path, serve_config={}) as fake:
        assert_assumed_login_safe(ASSUMED, fake.api)


def test_an_assumed_login_refuses_to_start_while_serve_is_active(tmp_path: Path) -> None:
    with tailscaled(tmp_path, serve_config={"TCP": {"443": {"HTTPS": True}}}) as fake:
        with pytest.raises(AssumedLoginUnsafeError):
            assert_assumed_login_safe(ASSUMED, fake.api)
        assert "serve reset" not in inspect.getsource(auth)


def test_without_an_assumed_login_tailscaled_is_never_asked(tmp_path: Path) -> None:
    with tailscaled(tmp_path, serve_config={"TCP": {"443": {"HTTPS": True}}}) as fake:
        assert_assumed_login_safe(PINNED, fake.api)
        assert fake.requests == []
