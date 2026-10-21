"""The static tree, read once at startup, addressed by content hash."""

import hashlib
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Final

STATIC_DIR: Final = Path(__file__).parent / "static"
STATIC_PREFIX: Final = "/static/"
_DIGEST_CHARS: Final = 12
# The page's scripts in run order, deferred after htmx; the first defines the shared window.hp.
SCRIPTS: Final = (
    "hp.js",
    "drafts.js",
    "backnav.js",
    "sendhold.js",
    "stream.js",
    "geometry.js",
    "interrupt.js",
    "swipe.js",
    "folds.js",
    "chatend.js",
    "voice.js",
    "status.js",
    "bubble.js",
    "app.js",
)


@dataclass(frozen=True, slots=True)
class Asset:
    """One static file, its bytes held in memory and its content digest."""

    body: bytes
    digest: str
    media_type: str


@dataclass(frozen=True, slots=True)
class Assets:
    """Every static file by name, plus a build stamp hashed from all of them."""

    files: dict[str, Asset]
    build: str

    @classmethod
    def load(cls, directory: Path) -> "Assets":
        """Read and hash every file under ``directory``; names are flat, never paths."""
        files: dict[str, Asset] = {}
        whole = hashlib.sha256()
        for path in sorted(p for p in directory.iterdir() if p.is_file()):
            body = path.read_bytes()
            digest = hashlib.sha256(body).hexdigest()[:_DIGEST_CHARS]
            kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            files[path.name] = Asset(body, digest, kind)
            whole.update(path.name.encode() + b"\0" + digest.encode())
        return cls(files, whole.hexdigest()[:_DIGEST_CHARS])

    def url(self, name: str) -> str:
        """The cache-busted URL a template links; a missing file is a bug, so it raises."""
        return f"{STATIC_PREFIX}{name}?v={self.files[name].digest}"
