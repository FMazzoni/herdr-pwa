"""What the engine is told by whoever builds it; it reads no environment of its own."""

from dataclasses import dataclass
from pathlib import Path

from herdr_pwa.protocol import EXPECTED_PROTOCOL


@dataclass(frozen=True, slots=True)
class EngineSettings:
    """Where the runner is reached, and how its reads and writes are bounded."""

    socket_path: Path
    command_timeout: float = 5.0
    expected_protocol: int = EXPECTED_PROTOCOL
    # One snapshot must not licence every later write for the life of the process.
    protocol_max_age: float = 10.0
    protocol_drift_fails_writes: bool = True
    read_source: str = "visible"
    read_lines: int = 200
    # Measured on herdr 0.8.0: 25 lines 2ms, 50 lines 1213ms, 200 lines 6905ms.
    recent_max_lines: int = 25
