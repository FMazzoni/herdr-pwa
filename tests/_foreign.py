"""Where the foreign capture clones live, which commits they must be at, and how to tell."""

import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

REPO = Path(__file__).resolve().parents[1]
LEDGER_PATH = REPO / "tests" / "fixtures" / "ledger.json"
# Outside every checkout, so each worktree reads the one set of clones the main checkout does.
DEFAULT_ROOT = Path.home() / ".cache" / "herdr-pwa" / "foreign-captures"
ENV = "HP_FOREIGN_CAPTURES"
RECIPE = "just corpus"
# The pinned commits live in the ledger's meta, under its checksum; only the remotes live here.
ORIGINS = {
    "collie": "https://github.com/AltanS/collie",
    "relay": "https://github.com/0cv/herdr-mobile-relay",
}


def default_root() -> Path:
    """$HP_FOREIGN_CAPTURES when set, else the per-user cache."""
    named = os.environ.get(ENV)
    return Path(named) if named else DEFAULT_ROOT


class _Source(BaseModel):
    model_config = ConfigDict(extra="ignore")

    commit: str = ""
    captures: str = ""
    glob: str = "*"


class _Meta(BaseModel):
    model_config = ConfigDict(extra="ignore")

    foreign_sources: dict[str, _Source] = Field(default_factory=dict[str, _Source])


class _Ledger(BaseModel):
    """The ledger as far as the corpus pins go."""

    model_config = ConfigDict(extra="ignore")

    meta: _Meta = Field(default_factory=_Meta)


def sources() -> dict[str, dict[str, str]]:
    """Each clone's pinned commit, captures directory and file pattern, as the ledger recorded them."""
    found = _Ledger.model_validate_json(LEDGER_PATH.read_bytes()).meta.foreign_sources
    return {
        name: {"commit": s.commit, "captures": s.captures, "glob": s.glob}
        for name, s in found.items()
    }


def head(clone: Path) -> str | None:
    """The commit a clone has checked out, or None when it is not a clone."""
    path = clone / ".git" / "HEAD"
    return path.read_text(encoding="utf-8").strip() if path.is_file() else None


def problems(root: Path) -> list[str]:
    """Every way ``root`` falls short of the pinned corpus; empty when it is the corpus."""
    found: list[str] = []
    for name, source in sources().items():
        clone = root / name
        at = head(clone)
        if at is None:
            found.append(f"{clone} is not a clone")
        elif at != source["commit"]:
            # A clone at another commit is a different screen set wearing the same file names.
            found.append(f"{clone} is at {at}, not the pinned {source['commit']}")
        elif not (clone / source["captures"]).is_dir():
            found.append(f"{clone / source['captures']} is missing")
    return found


def explain(root: Path, found: list[str]) -> str:
    """The failure a test or the recipe prints, naming the command that fixes it."""
    lines = "\n".join(f"  - {line}" for line in found)
    return (
        f"foreign corpus at {root} is not the pinned one:\n{lines}\n"
        f"run `{RECIPE}` to clone or fetch it (the directory is ${ENV}, default {DEFAULT_ROOT})"
    )
