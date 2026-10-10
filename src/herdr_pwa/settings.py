"""Where the app listens, keeps its state, and finds herdr; every field overridable by env."""

from pathlib import Path

from pydantic import Field
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
