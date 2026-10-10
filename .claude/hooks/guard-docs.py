#!/usr/bin/env python3
"""PreToolUse guard: agents may not write under <repo>/docs/ unless HP_DOCS_WRITE=1."""

import json
import os
import sys
from pathlib import Path


def repo_root(path: Path) -> Path | None:
    for parent in path.parents:
        if (parent / ".git").exists():  # a directory in the main checkout, a file in a worktree
            return parent
    return None


def main() -> int:
    if os.environ.get("HP_DOCS_WRITE") == "1":
        return 0
    payload = json.load(sys.stdin)
    tool_input = payload.get("tool_input", {})
    raw = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not raw:
        return 0
    path = Path(raw)
    if not path.is_absolute():
        path = Path(payload.get("cwd") or Path.cwd()) / path
    path = Path(os.path.normpath(path))
    root = repo_root(path)
    if root is None or path.relative_to(root).parts[:1] != ("docs",):
        return 0
    sys.stderr.write(
        f"Blocked: {path.relative_to(root)} is project documentation. Put notes in "
        ".agents/ instead. A real docs change is launched with HP_DOCS_WRITE=1.\n"
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
