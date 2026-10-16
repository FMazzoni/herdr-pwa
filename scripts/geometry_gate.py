"""Run the geometry checks only when something they measure has changed, and record a pass."""

import argparse
import fcntl
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import IO

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.firefox import available, version  # noqa: E402
from tests.catalogue import CAPTURES  # noqa: E402

# Per user rather than per worktree: a fresh worktree whose guarded files match a tree already
# measured reuses that receipt. A cache of a result, never a record anyone reviews.
CACHE = Path.home() / ".cache" / "herdr-pwa-v2"
RECEIPTS = CACHE / "geometry"
# Per user too, so parallel agents queue for one Firefox instead of five.
LOCK = CACHE / "geometry.lock"

# Everything that decides what the browser lays out, plus the harness itself.
GUARDED = (
    "src/herdr_pwa/web",
    "src/herdr_pwa/panetext.py",
    "tests/webfakes.py",
    "tests/catalogue.py",
    "tests/fixtures/snapshot.example.json",
    # Every captured screen a catalogue state draws.
    *(f"tests/fixtures/{name}/detection.txt" for name in CAPTURES),
    "scripts/firefox.py",
    "scripts/geometry.py",
    "scripts/backnav.py",
    "scripts/chatend.py",
    "scripts/feedhold.py",
    "scripts/sendhold.py",
    "scripts/voiceover.py",
    "scripts/settle.py",
    "tests/waiting.py",
    "tests/_fake_herd.py",
    "scripts/geometry_gate.py",
)
SKIP = ("__pycache__",)


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


def receipt(current: str) -> Path:
    """Where a green run for ``current`` leaves its receipt."""
    return RECEIPTS / f"{current}.json"


def held() -> IO[str]:
    """Take the per-user browser lock, saying so when another run already holds it."""
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    handle = LOCK.open("w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("geometry: waiting for another run to release the browser lock", flush=True)
        fcntl.flock(handle, fcntl.LOCK_EX)
    return handle


def run(current: str) -> int:
    """Run the checks in this interpreter and write the receipt only if every one passed."""
    # Imported late so a receipt hit skips loading it; its browsers live in its own workers.
    import geometry  # noqa: PLC0415

    started = time.monotonic()
    code = geometry.main([])
    if code != 0:
        print(f"geometry: RED for {current}; no receipt written", file=sys.stderr)
        return code
    seconds = round(time.monotonic() - started, 1)
    RECEIPTS.mkdir(parents=True, exist_ok=True)
    body = {"fingerprint": current, "seconds": seconds, "files": len(guarded_files())}
    receipt(current).write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    print(f"geometry green for {current} in {seconds}s; receipt written")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Skip on a fresh receipt, else take the lock and run."""
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
        print(f"geometry green for {current}")
        return 0
    with held():
        if receipt(current).is_file() and not args.force:
            print(f"geometry green for {current} (another run covered it while we waited)")
            return 0
        return run(current)


if __name__ == "__main__":
    raise SystemExit(main())
