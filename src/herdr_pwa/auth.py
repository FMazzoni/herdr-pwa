"""Identity gate: any local uid can forge the header and is trusted; a browser needs this Host."""

import hmac
from collections.abc import Mapping
from dataclasses import dataclass
from http import HTTPStatus
from http.client import HTTPException
from typing import ClassVar, Final, Literal
from urllib.parse import SplitResult, urlsplit

from pydantic import ValidationError

from herdr_pwa import tailscale
from herdr_pwa.validation import is_not_json

IDENTITY_HEADER: Final = "tailscale-user-login"
SAFE_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS"})
# Who vouched for a login: the ingress, the config assuming one, or the uid running the CLI.
type Via = Literal["tailscale", "config", "cli"]
VIA_TAILSCALE: Final = "tailscale"
VIA_CONFIG: Final = "config"
VIA_CLI: Final = "cli"
# Names, not addresses: what the caller typed is what DNS rebinding cannot forge.
LOOPBACK_HOSTS: Final = frozenset({"127.0.0.1", "::1", "localhost", "localhost."})


class AuthError(Exception):
    """Every refusal; ``str(exc)`` goes to the caller, ``log_detail`` only to the operator."""

    status_code: ClassVar[int] = 403
    reason: ClassVar[str] = "forbidden"

    def __init__(self, message: str, *, log_detail: str = "") -> None:
        super().__init__(message)
        self.log_detail: str = log_detail


class MissingIdentityError(AuthError):
    """No identity header: the request did not come through the ingress."""

    status_code: ClassVar[int] = 401
    reason: ClassVar[str] = "missing_identity"


class LoginNotAllowedError(AuthError):
    """A tailnet user other than the pinned login."""

    reason: ClassVar[str] = "login_not_allowed"


class NoLoginPinnedError(AuthError):
    """No login is pinned and any-user mode was not chosen, so no tailnet user is trusted."""

    reason: ClassVar[str] = "no_login_pinned"


class DuplicateIdentityError(AuthError):
    """More than one identity header, which tailscaled never sends."""

    status_code: ClassVar[int] = 400
    reason: ClassVar[str] = "duplicate_identity"


class CrossSiteError(AuthError):
    """A read another site's page started, which only probes for what exists."""

    reason: ClassVar[str] = "cross_site"


class MissingOriginError(AuthError):
    """A write with no Origin header."""

    reason: ClassVar[str] = "missing_origin"


class OriginMismatchError(AuthError):
    """A write whose Origin is not the Host it was addressed to."""

    reason: ClassVar[str] = "origin_mismatch"


class NonLoopbackHostError(AuthError):
    """An assumed-login request addressed by a name that is not loopback."""

    reason: ClassVar[str] = "non_loopback_host"


class WrongHostError(AuthError):
    """A header identity addressed by a name other than this node's served one."""

    reason: ClassVar[str] = "wrong_host"


class ServedHostUnknownError(AuthError):
    """A header identity while the served name is unknown, so no Host can be trusted."""

    reason: ClassVar[str] = "served_host_unknown"


class AssumedLoginUnsafeError(AuthError):
    """An assumed login where serve is active, or where that cannot be decided."""

    status_code: ClassVar[int] = 500
    reason: ClassVar[str] = "assumed_login_unsafe"


@dataclass(frozen=True, slots=True)
class Access:
    """Who may use the app: the pinned login, and the login assumed when no header came."""

    tailnet_login: str = ""
    assume_login: str = ""
    # With no login pinned, any tailnet user may enter; off, every header identity is refused.
    any_login: bool = False
    # The one Host a header identity may arrive on, as ``host_name`` gives it; empty refuses all.
    served_host: str = ""


@dataclass(frozen=True, slots=True)
class Identity:
    """The login a request acts as, and whether the ingress or the config supplied it."""

    login: str
    via: Via = VIA_TAILSCALE

    @property
    def is_assumed(self) -> bool:
        """True when nobody proved the login and the config supplied it."""
        return self.via == VIA_CONFIG


def _lower(headers: Mapping[str, str]) -> dict[str, str]:
    return {key.lower(): value for key, value in headers.items()}


def _same_login(expected: str, actual: str) -> bool:
    return hmac.compare_digest(expected.casefold().encode(), actual.casefold().encode())


def identify(headers: Mapping[str, str], access: Access) -> Identity:
    """Return the identity the ingress asserted, the assumed one, or refuse."""
    login = _identity_header(headers)
    if not login:
        if access.assume_login:
            return Identity(login=access.assume_login, via=VIA_CONFIG)
        raise MissingIdentityError(
            "No Tailscale identity on this request. Reach the app through its MagicDNS "
            "name, not the loopback port."
        )
    pin = access.tailnet_login
    if not pin and not access.any_login:
        raise NoLoginPinnedError(
            "This app has no login pinned, so it trusts no tailnet user. Set HP_TAILNET_LOGIN.",
            log_detail=f"refused {login!r}: HP_TAILNET_LOGIN and HP_ALLOW_ANY_TAILNET_USER unset",
        )
    if pin and not _same_login(pin, login):
        raise LoginNotAllowedError(
            "This tailnet user is not the one this app is configured for.",
            # tailscale status truncates logins, so a wrong pin looks right at a glance.
            log_detail=f"login {login!r} is not the pinned HP_TAILNET_LOGIN {pin!r}",
        )
    return Identity(login=login)


def login_policy(access: Access) -> str:
    """Who may use the app, in one line, as startup and doctor say it."""
    if access.tailnet_login:
        return f"only {access.tailnet_login} (HP_TAILNET_LOGIN)"
    if access.any_login:
        return "any user your tailnet lets reach this node (HP_ALLOW_ANY_TAILNET_USER)"
    return "nobody: HP_TAILNET_LOGIN is not set"


def assert_login_chosen(access: Access) -> None:
    """At startup: refuse to serve with no pinned login unless any-user mode was chosen."""
    if access.tailnet_login or access.any_login:
        return
    raise NoLoginPinnedError(
        "Refusing to start: HP_TAILNET_LOGIN is not set. Set it to your Tailscale login, or set "
        "HP_ALLOW_ANY_TAILNET_USER=1 to let any user your tailnet admits drive every agent."
    )


def unpinned_warning(access: Access) -> str | None:
    """A startup warning when any tailnet user is let in, else None."""
    if access.tailnet_login or not access.any_login:
        return None
    return (
        "HP_ALLOW_ANY_TAILNET_USER is set and HP_TAILNET_LOGIN is not: any user your tailnet lets "
        "reach this node can drive every agent on this machine."
    )


def check_same_origin(headers: Mapping[str, str]) -> None:
    """Refuse a write whose Origin host is not the Host header (CSRF, not authentication)."""
    lowered = _lower(headers)
    origin = lowered.get("origin", "").strip()
    if not origin:
        raise MissingOriginError(
            "This write has no Origin header; a script must send one, e.g. "
            "-H 'Origin: https://<your-node>.ts.net'."
        )
    host = lowered.get("host", "").strip()
    if not host:
        raise OriginMismatchError("This write has an Origin but no Host header to compare it with.")
    parsed = urlsplit(origin)
    if origin.lower() == "null" or not parsed.hostname:
        raise OriginMismatchError(f"Refusing a write from opaque or unparseable Origin {origin!r}.")
    _check_scheme(parsed, lowered.get("x-forwarded-proto", "").strip().lower())
    if _authority(parsed.scheme, origin) != _authority(parsed.scheme, f"//{host}"):
        raise OriginMismatchError(
            f"Refusing a cross-origin write: Origin {origin!r} is not {host!r}."
        )


def check_same_site(headers: Mapping[str, str]) -> None:
    """Refuse a read another site started; Safari, Chrome and Firefox all send Sec-Fetch-Site."""
    if _lower(headers).get("sec-fetch-site", "").strip().lower() == "cross-site":
        raise CrossSiteError("Refusing a request another site's page started.")


def _identity_header(headers: Mapping[str, str]) -> str:
    # A Starlette Headers keeps every copy; one reader taking the first and another the last is ambiguity.
    found = [value for key, value in headers.items() if key.lower() == IDENTITY_HEADER]
    if len(found) > 1:
        raise DuplicateIdentityError("This request has more than one Tailscale identity header.")
    return found[0].strip() if found else ""


def _check_scheme(parsed: SplitResult, forwarded_proto: str) -> None:
    if forwarded_proto and parsed.scheme.lower() != forwarded_proto:
        raise OriginMismatchError(
            f"Refusing a write: Origin scheme {parsed.scheme!r} is not {forwarded_proto!r}."
        )


def _authority(scheme: str, url: str) -> str:
    parsed = urlsplit(url if "//" in url else f"//{url}")
    host = (parsed.hostname or "").lower()
    default = {"https": 443, "http": 80}.get(scheme.lower())
    try:
        port = parsed.port
    except ValueError:
        # A malformed port must never compare equal to a well-formed authority.
        return url.lower()
    if port is None or port == default:
        return host
    return f"{host}:{port}"


def check_loopback_host(headers: Mapping[str, str]) -> None:
    """Refuse an assumed-login request whose Host is not a literal loopback name."""
    host = _lower(headers).get("host", "").strip()
    if not host:
        raise NonLoopbackHostError(
            "This request has no Host header, so it is not provably loopback."
        )
    if (urlsplit(f"//{host}").hostname or "") not in LOOPBACK_HOSTS:
        raise NonLoopbackHostError(
            "Refusing a non-loopback name while a login is assumed; use 127.0.0.1 or localhost.",
            log_detail=f"Host {host!r} is not loopback; accepting it would allow DNS rebinding",
        )


def host_name(host: str) -> str:
    """A Host or DNS name as compared: lowercased, with no trailing dot and no ``:443``."""
    name = host.strip().lower().removesuffix(":443")
    return name.removesuffix(".")


def check_served_host(headers: Mapping[str, str], served_host: str) -> None:
    """Refuse a header identity whose Host is not this node's served name (DNS rebinding)."""
    if not served_host:
        raise ServedHostUnknownError(
            "This app does not know its own MagicDNS name, so it refuses every Tailscale "
            "identity. Set HP_PUBLIC_HOST or fix tailscaled, then restart it.",
            log_detail="the served name was not learned at startup and HP_PUBLIC_HOST is unset",
        )
    host = _lower(headers).get("host", "")
    if host_name(host) != served_host:
        raise WrongHostError(
            "Refusing a request addressed to a name that is not this app's; open it through its "
            "MagicDNS name.",
            log_detail=f"Host {host!r} is not the served {served_host!r}; maybe DNS rebinding",
        )


def authorize(headers: Mapping[str, str], *, access: Access, method: str = "GET") -> Identity:
    """Authenticate every request, reads included, then origin-check any write."""
    identity = identify(headers, access)
    if identity.is_assumed:
        check_loopback_host(headers)
    else:
        check_served_host(headers, access.served_host)
    if method.strip().upper() not in SAFE_METHODS:
        check_same_origin(headers)
    return identity


def served_host(configured: str, api: tailscale.LocalApi) -> str:
    """The name header identities must arrive on: ``HP_PUBLIC_HOST``, else this node's MagicDNS.

    Raises:
        LocalApiError: nothing is configured and tailscaled could not name this node.
    """
    if configured.strip():
        return host_name(configured)
    return host_name(tailscale.magicdns(api))


def _serve_config_body(api: tailscale.LocalApi) -> str:
    try:
        reply = api.exchange("GET", tailscale.SERVE_CONFIG_PATH)
    except (FileNotFoundError, ConnectionRefusedError):
        raise
    except (OSError, HTTPException) as exc:
        raise AssumedLoginUnsafeError(
            f"Refusing an assumed login: could not ask tailscaled whether serve is active ({exc})."
        ) from exc
    if reply.status != HTTPStatus.OK:
        raise AssumedLoginUnsafeError(
            f"Refusing an assumed login: tailscaled answered HTTP {reply.status} for its serve "
            "config, so whether this node is served is unknown."
        )
    return reply.body


def serve_is_active(api: tailscale.LocalApi) -> bool:
    """True when anything is served; False only when no tailscaled could be serving."""
    try:
        body = _serve_config_body(api)
    except (FileNotFoundError, ConnectionRefusedError):
        return False
    try:
        config = tailscale.serve_config(body)
    except ValidationError as exc:
        shape = "JSON" if is_not_json(exc) else "an object"
        raise AssumedLoginUnsafeError(
            f"Refusing an assumed login: tailscaled's serve config was not {shape}."
        ) from exc
    return tailscale.is_serving(config)


def assert_assumed_login_safe(access: Access, api: tailscale.LocalApi) -> None:
    """At startup: refuse an assumed login while serve is active or undecidable."""
    if not access.assume_login:
        return
    if serve_is_active(api):
        raise AssumedLoginUnsafeError(
            "Refusing to start: HP_ASSUME_LOGIN is set while 'tailscale serve' is active, which "
            "would publish an unauthenticated shell to the tailnet. Unset HP_ASSUME_LOGIN."
        )
