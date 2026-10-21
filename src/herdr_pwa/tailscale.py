"""tailscaled's LocalAPI over its unix socket: no CLI, no cloud API, no PyPI wrapper."""

import socket
from dataclasses import dataclass
from http import HTTPStatus
from http.client import HTTPConnection, HTTPException
from pathlib import Path
from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, ValidationError

from herdr_pwa.validation import OR_NONE, Text, is_not_json

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
        socket = repr(str(self.socket_path))
        where = f"the tailscaled socket {socket}"
        if isinstance(exc, FileNotFoundError):
            return (
                f"No tailscaled socket at {socket}. "
                "Install Tailscale, start tailscaled, then run 'tailscale up'."
            )
        if isinstance(exc, ConnectionRefusedError):
            return f"Nothing is listening on {where}; start tailscaled, then 'tailscale up'."
        if isinstance(exc, PermissionError):
            return (
                f"Permission denied opening {where}. Grant this user access with "
                "'sudo tailscale set --operator=<your-username>'."
            )
        if isinstance(exc, TimeoutError):
            # :g keeps a sub-second budget from reading as "0s".
            return f"Timed out after {self.timeout:g}s talking to tailscaled at {where}."
        return f"Could not reach {path} on {where}: {exc}"


class _Node(BaseModel):
    """This node, as the status names it."""

    model_config = ConfigDict(extra="ignore")

    dns_name: Text = Field(default=None, validation_alias="DNSName")


class TailnetStatus(BaseModel):
    """tailscaled's status, holding only what this app reads of it."""

    model_config = ConfigDict(extra="ignore")

    backend_state: Text = Field(default=None, validation_alias="BackendState")
    node: Annotated[_Node | None, OR_NONE] = Field(default=None, validation_alias="Self")


def status(api: LocalApi) -> TailnetStatus:
    """The parsed status, refusing when the tailnet is not up."""
    raw = api.read(STATUS_PATH).body
    try:
        parsed = TailnetStatus.model_validate_json(raw)
    except ValidationError as exc:
        if is_not_json(exc):
            raise LocalApiError(f"tailscaled's status was not JSON: {exc}") from exc
        raise LocalApiError("tailscaled's status was not a JSON object.") from exc
    if parsed.backend_state != "Running":
        raise LocalApiError(
            f"Tailscale is not running (BackendState={parsed.backend_state!r}). "
            "Run 'tailscale up' and sign in."
        )
    return parsed


def magicdns(api: LocalApi) -> str:
    """This node's MagicDNS name without the trailing dot."""
    node = status(api).node
    if node is None:
        raise LocalApiError("tailscaled's status has no 'Self' node object.")
    name = node.dns_name or ""
    if not name.strip(". "):
        raise LocalApiError(
            "This node has no MagicDNS name. Enable MagicDNS for the tailnet in the admin "
            "console, then run 'tailscale up' again."
        )
    return name.strip().rstrip(".")


# The serve config is kept whole: pairing writes it back, so a section it does not know survives.
type ServeConfig = dict[str, JsonValue]

_SERVE_CONFIG: Final[TypeAdapter[dict[str, JsonValue] | None]] = TypeAdapter(
    dict[str, JsonValue] | None
)


def serve_config(body: str) -> ServeConfig:
    """tailscaled's serve config, empty when it has none; raises ``ValidationError`` otherwise."""
    # An empty body is not "no config": tailscaled says that with `null`.
    return _SERVE_CONFIG.validate_json(body.strip()) or {}


def is_serving(config: ServeConfig) -> bool:
    """Whether any section, known or not, carries anything at all."""
    # An allow-list of keys once read a serve as none.
    return any(_has_content(value) for value in config.values())


def root_handler(config: ServeConfig, site: str) -> dict[str, JsonValue] | None:
    """The handler serving ``/`` on *site* (``host:port``), or ``None``."""
    handler: JsonValue = config
    for key in ("Web", site, "Handlers", "/"):
        handler = handler.get(key) if isinstance(handler, dict) else None
    return handler if isinstance(handler, dict) else None


def _has_content(value: JsonValue) -> bool:
    """Whether one serve-config section carries anything at all."""
    if value is None or value is False:
        return False
    if isinstance(value, str | dict | list):
        return bool(value)
    return True
