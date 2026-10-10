"""Where the app listens, keeps its state, and finds herdr; every field overridable by env."""

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # Empty means any tailnet user may drive every agent; startup warns.
    tailnet_login: str = ""
    # Waives the identity header for this named login, confined to a loopback Host.
    assume_login: str = ""
    tailscaled_socket: Path = Path("/var/run/tailscale/tailscaled.sock")

    push_enabled: bool = True
    # No default: Apple answers 403 BadJwtToken to a placeholder contact and drops every push.
    push_subject: str = ""
    # A push is a claim about live state, so a late delivery is a confident lie.
    push_ttl: int = Field(default=900, ge=0)
    push_debounce: float = Field(default=30.0, ge=0)
    push_desk_window: float = Field(default=60.0, ge=0)
    push_ledger_max_age: float = Field(default=900.0, gt=0)
    push_unseen_finish: bool = True
    push_quiet_managed: bool = True

    audit_max_bytes: int = Field(default=5 * 1024 * 1024, ge=4096)
    audit_payload_chars: int = Field(default=512, ge=0)

    @field_validator("tailnet_login", "assume_login", "push_subject")
    @classmethod
    def _trimmed(cls, value: str) -> str:
        return value.strip()
