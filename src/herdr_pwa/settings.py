"""Where the app listens, keeps its state, and finds herdr; every field overridable by env."""

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from herdr_pwa.engine.config import EngineSettings
from herdr_pwa.protocol import DEFAULT_SOCKET_PATH


def _default_state_dir() -> Path:
    return Path.home() / ".config" / "herdr-pwa-v2"


class Settings(BaseSettings):
    """Runtime configuration read from ``HP_*`` environment variables."""

    model_config = SettingsConfigDict(env_prefix="HP_", extra="ignore")

    host: str = "127.0.0.1"
    # 8770 belongs to the live bridge until cutover; 8771 is legacy's test port.
    port: int = 8772
    state_dir: Path = Field(default_factory=_default_state_dir)
    socket_path: Path | None = None
    command_timeout: float = Field(default=5.0, gt=0)
    # Every concurrent poll shares one fetch inside this window.
    snapshot_cache_ttl: float = Field(default=2.0, gt=0)
    # Empty means each harness's own default location.
    transcript_roots: tuple[str, ...] = ()
    # A page of chat counts messages (prompts and agent prose); tool calls ride along inside it.
    chat_messages: int = Field(default=20, gt=0)

    # The event stream: how often the herd is diffed, how long a quiet stream waits before a
    # heartbeat keeps the proxy from buffering or closing it, and how many pages may hold one.
    events_interval: float = Field(default=1.0, gt=0)
    events_heartbeat: float = Field(default=15.0, gt=0)
    events_max_clients: int = Field(default=16, gt=0)
    # A stream is closed after this long and the page reconnects, so no half-open one lingers.
    events_lifetime: float = Field(default=600.0, gt=0)
    # How often a page polls while its stream is down.
    events_fallback: int = Field(default=20, gt=0)

    # Empty means any tailnet user may drive every agent; startup warns.
    tailnet_login: str = ""
    # Waives the identity header for this named login, confined to a loopback Host.
    assume_login: str = ""
    tailscaled_socket: Path = Path("/var/run/tailscale/tailscaled.sock")

    push_enabled: bool = True
    # Off leaves the routes up but never polls; tests run without the background loop.
    push_notifier: bool = True
    # How often the notifier reads the herd; an event only ever costs latency, never a buzz.
    push_interval: float = Field(default=2.0, gt=0)
    # No default: Apple answers 403 BadJwtToken to a placeholder contact and drops every push.
    push_subject: str = ""
    # A push is a claim about live state, so a late delivery is a confident lie.
    push_ttl: int = Field(default=900, ge=0)
    push_debounce: float = Field(default=30.0, ge=0)
    push_desk_window: float = Field(default=60.0, ge=0)
    push_ledger_max_age: float = Field(default=900.0, gt=0)
    push_unseen_finish: bool = True
    push_quiet_managed: bool = True

    # Debug only: every page reports the phone's real geometry and navigation to the state dir.
    device_report: bool = False

    audit_max_bytes: int = Field(default=5 * 1024 * 1024, ge=4096)
    audit_payload_chars: int = Field(default=512, ge=0)

    @field_validator("tailnet_login", "assume_login", "push_subject")
    @classmethod
    def _trimmed(cls, value: str) -> str:
        return value.strip()

    def engine(self) -> EngineSettings:
        """The engine's view of these settings."""
        socket = self.socket_path or Path(DEFAULT_SOCKET_PATH).expanduser()
        return EngineSettings(socket_path=socket, command_timeout=self.command_timeout)
