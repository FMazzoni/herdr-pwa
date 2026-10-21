"""Run the geometry checks only when something they measure has changed, and record a pass."""

import argparse
import hashlib
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final

ROOT: Final = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.firefox import available, held, version  # noqa: E402 - after the sys.path line that makes it importable

# Per user rather than per worktree: a fresh worktree whose guarded files match a tree already
# measured reuses that receipt. A cache of a result, never a record anyone reviews.
CACHE: Final = Path.home() / ".cache" / "herdr-pwa"


@dataclass(frozen=True, slots=True)
class GateConfig:
    """Where the gate keeps its receipts: one per green run, and one per browser test that passed."""

    receipts: Path = CACHE / "geometry"

    @property
    def walks(self) -> Path:
        """One receipt per browser test that passed, under the tree's fingerprint, so pytest reuses it."""
        return self.receipts / "walks"


CONFIG: Final = GateConfig()

# All the browser lays out from, and the harness: too wide costs a re-run, too narrow a false green.
GUARDED: Final = (
    "src/herdr_pwa",
    "pyproject.toml",
    "uv.lock",
    "tests/conftest.py",
    "tests/fixtures",
    "tests/webfakes.py",
    "tests/catalogue.py",
    "scripts/firefox.py",
    "scripts/geometry.py",
    "scripts/affordance.py",
    "scripts/backnav.py",
    "scripts/chatend.py",
    "scripts/feedhold.py",
    "scripts/sendhold.py",
    "scripts/voiceover.py",
    "scripts/settle.py",
    "tests/waiting.py",
    "tests/_fake_herd.py",
    "scripts/geometry_gate.py",
    "tests/browser",
)
SKIP: Final = ("__pycache__",)


def guarded_files() -> list[Path]:
    """Every guarded file, sorted, with bytecode caches dropped."""
    found: list[Path] = []
    for name in GUARDED:
        path = ROOT / name
        if path.is_file():
            found.append(path)
            continue
        found += [p for p in path.rglob("*") if p.is_file() and not set(p.parts) & set(SKIP)]
    return sorted(found)


def fingerprint() -> str:
    """One hash over the path and bytes of every guarded file and the browser's version."""
    digest = hashlib.sha256(version().encode())
    for path in guarded_files():
        digest.update(str(path.relative_to(ROOT)).encode() + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()[:16]


def receipt(current: str, config: GateConfig = CONFIG) -> Path:
    """Where a green run for ``current`` leaves its receipt."""
    return config.receipts / f"{current}.json"


def walk_receipt(current: str, test: str, config: GateConfig = CONFIG) -> Path:
    """Where browser test ``test`` (a pytest node id) leaves its receipt for tree ``current``."""
    name = hashlib.sha256(test.encode()).hexdigest()[:16]
    return config.walks / current / f"{name}.json"


def record_walk(current: str, test: str, seconds: float, config: GateConfig = CONFIG) -> None:
    """Leave the receipt that lets ``test`` be reused until ``current`` changes."""
    path = walk_receipt(current, test, config)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"fingerprint": current, "test": test, "seconds": round(seconds, 1)}
    path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")


def run(current: str, config: GateConfig = CONFIG) -> int:
    """Run the checks in this interpreter and write the receipt only if every one passed."""
    # Imported late so a receipt hit skips loading it; its browsers live in its own workers.
    import geometry  # noqa: PLC0415 - late, so a receipt hit never loads it

    started = time.monotonic()
    code = geometry.main([])
    if code != 0:
        print(f"geometry: RED for {current}; no receipt written", file=sys.stderr)
        return code
    seconds = round(time.monotonic() - started, 1)
    config.receipts.mkdir(parents=True, exist_ok=True)
    body = {"fingerprint": current, "seconds": seconds, "files": len(guarded_files())}
    receipt(current, config).write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    print(f"geometry green for {current} in {seconds}s; receipt written")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Reuse a fresh receipt, else take the lock and run."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--force", action="store_true", help="run even if a receipt exists")
    args = parser.parse_args(argv)
    # A gate that skips itself when its tool is missing is not a gate.
    if not available():
        print(
            "geometry: firefox is not on PATH; install it, this gate never skips", file=sys.stderr
        )
        return 1
    current = fingerprint()
    if receipt(current).is_file() and not args.force:
        print(f"geometry reused the receipt for {current}")
        return 0
    with held():
        if receipt(current).is_file() and not args.force:
            print(f"geometry reused the receipt for {current}, written while we waited")
            return 0
        return run(current)


if __name__ == "__main__":
    raise SystemExit(main())
