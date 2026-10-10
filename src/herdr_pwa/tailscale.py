"""tailscaled's LocalAPI over its unix socket: no CLI, no cloud API, no PyPI wrapper."""

import json
import socket
from dataclasses import dataclass
from http import HTTPStatus
from http.client import HTTPConnection, HTTPException
from pathlib import Path

from herdr_pwa import jsonio

# LocalAPI refuses any request whose Host is not this exact name (its CSRF guard).
LOCALAPI_HOST = "local-tailscaled.sock"
STATUS_PATH = "/localapi/v0/status"
SERVE_CONFIG_PATH = "/localapi/v0/serve-config"
TIMEOUT = 10.0
_MAX_BODY = 8 * 1024 * 1024
REFUSED = frozenset({HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN})


class LocalApiError(RuntimeError):
    """tailscaled could not be asked, or did not answer usefully; the message says what to do."""


class UnixHTTPConnection(HTTPConnection):
    """An ``HTTPConnection`` that dials an ``AF_UNIX`` socket."""

    def __init__(self, socket_path: Path, *, timeout: float = TIMEOUT) -> None:
        super().__init__(LOCALAPI_HOST, timeout=timeout)
        self.socket_path: Path = socket_path

    def connect(self) -> None:
        """Open the unix socket instead of a TCP connection."""
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(str(self.socket_path))
        except BaseException:
            sock.close()
            raise
        self.sock: socket.socket = sock


@dataclass(frozen=True, slots=True)
class Reply:
    """One LocalAPI answer, whatever its status."""

    status: int
    reason: str
    body: str
    etag: str


@dataclass(frozen=True, slots=True)
class LocalApi:
    """Where tailscaled listens and how long to wait for it; tests point it at a fake socket."""

    socket_path: Path
    timeout: float = TIMEOUT

    def exchange(
        self,
        method: str,
        path: str,
        *,
        body: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> Reply:
        """Send one request and return the raw reply; transport failures raise as they are."""
        conn = UnixHTTPConnection(self.socket_path, timeout=self.timeout)
        try:
            conn.request(
                method, path, body=body, headers={"Host": LOCALAPI_HOST, **(headers or {})}
            )
            response = conn.getresponse()
            text = response.read(_MAX_BODY).decode("utf-8", errors="replace")
            return Reply(
                status=response.status,
                reason=response.reason or "",
                body=text,
                etag=response.getheader("Etag") or "",
            )
        finally:
            conn.close()

    def call(
        self,
        method: str,
        path: str,
        *,
        body: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> Reply:
        """``exchange`` with every transport failure turned into an actionable LocalApiError."""
        try:
            return self.exchange(method, path, body=body, headers=headers)
        except (OSError, HTTPException) as exc:
            raise LocalApiError(self._transport_hint(exc, path)) from exc

    def read(self, path: str) -> Reply:
        """GET a LocalAPI path and insist on a 200."""
        reply = self.call("GET", path)
        if reply.status == HTTPStatus.OK:
            return reply
        detail = reply.body.strip()
        hint = f" Details: {detail[:200]}" if detail else ""
        if reply.status in REFUSED:
            raise LocalApiError(
                f"tailscaled refused {path} (HTTP {reply.status}). This user is not the Tailscale "
                f"operator; try 'sudo tailscale set --operator=<your-username>'.{hint}"
            )
        raise LocalApiError(
            f"tailscaled returned HTTP {reply.status} {reply.reason} for {path}.{hint}"
        )

    def _transport_hint(self, exc: Exception, path: str) -> str:
        where = f"the tailscaled socket {str(self.socket_path)!r}"
        if isinstance(exc, FileNotFoundError):
            return f"No {where}. Install Tailscale, start tailscaled, then run 'tailscale up'."
        if isinstance(exc, ConnectionRefusedError):
            return f"Nothing is listening on {where}; start tailscaled, then 'tailscale up'."
        if isinstance(exc, PermissionError):
            return (
                f"Permission denied opening {where}. Grant this user access with "
                "'sudo tailscale set --operator=<your-username>'."
            )
        if isinstance(exc, TimeoutError):
            return f"Timed out after {self.timeout:.0f}s talking to tailscaled at {where}."
        return f"Could not reach {path} on {where}: {exc}"


def status(api: LocalApi) -> dict[str, object]:
    """The parsed status object, refusing when the tailnet is not up."""
    raw = api.read(STATUS_PATH).body
    try:
        parsed = jsonio.as_dict(jsonio.loads(raw))
    except json.JSONDecodeError as exc:
        raise LocalApiError(f"tailscaled's status was not JSON: {exc}") from exc
    if parsed is None:
        raise LocalApiError("tailscaled's status was not a JSON object.")
    state = parsed.get("BackendState")
    if state != "Running":
        raise LocalApiError(
            f"Tailscale is not running (BackendState={state!r}). Run 'tailscale up' and sign in."
        )
    return parsed


def magicdns(api: LocalApi) -> str:
    """This node's MagicDNS name without the trailing dot."""
    found = status(api)
    if jsonio.as_dict(found.get("Self")) is None:
        raise LocalApiError("tailscaled's status has no 'Self' node object.")
    name = jsonio.as_str(jsonio.child(found.get("Self"), "DNSName")) or ""
    if not name.strip(". "):
        raise LocalApiError(
            "This node has no MagicDNS name. Enable MagicDNS for the tailnet in the admin "
            "console, then run 'tailscale up' again."
        )
    return name.strip().rstrip(".")


def self_login(api: LocalApi) -> str:
    """The login the ingress would assert for this node's own user, e.g. to pin it."""
    found = status(api)
    user_id = jsonio.child(found.get("Self"), "UserID")
    # Go marshals the map[UserID] keys as strings while Self.UserID stays a number.
    user = jsonio.child(found.get("User"), str(user_id))
    login = jsonio.as_str(jsonio.child(user, "LoginName")) or ""
    if not login.strip():
        raise LocalApiError("tailscaled's status carries no LoginName for this node's user.")
    return login.strip()


def has_content(value: object) -> bool:
    """Whether one serve-config section carries anything at all."""
    if value is None or value is False:
        return False
    if isinstance(value, str):
        return bool(value)
    mapping = jsonio.as_dict(value)
    if mapping is not None:
        return bool(mapping)
    items = jsonio.as_list(value)
    return True if items is None else bool(items)
