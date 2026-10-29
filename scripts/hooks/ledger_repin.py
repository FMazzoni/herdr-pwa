#!/usr/bin/env python3
"""Refuse a commit that re-pins the ledger's hash without adding to `meta.rehashed`."""

# Runs as pre-commit's hook on the two files below, comparing the index with HEAD: a pytest
# can't see the previous state, since CI clones shallow. See .pre-commit-config.yaml.

import json
import re
import subprocess
import sys
from typing import Final

TEST: Final = "tests/test_ledger.py"
LEDGER: Final = "tests/fixtures/ledger.json"
PIN: Final = re.compile(r'^LEDGER_SHA256 = "(?P<pin>\w+)"', re.MULTILINE)
RULE: Final = (
    "Re-pin an input hash (a row's `sha256`, and `LEDGER_SHA256`, the test's hash of the ledger file) "
    "only when a capture was scrubbed or replaced and no verdict changes; note it in `meta.rehashed`."
)


def main() -> int:
    """Refuse the staged commit when LEDGER_SHA256 changed and `meta.rehashed` did not grow."""
    before, after = _show(f"HEAD:{TEST}"), _show(f":{TEST}")
    if before is None or _pin(before) == _pin(after):
        return 0
    if len(_note(_show(f":{LEDGER}"))) > len(_note(_show(f"HEAD:{LEDGER}"))):
        return 0
    print(
        f'ledger-repin refused: add a sentence to meta.rehashed in {LEDGER}, as AGENTS.md says: "{RULE}"',
        file=sys.stderr,
    )
    return 1


def _show(spec: str) -> str | None:
    # A path missing at HEAD (the first commit) or in the index reads as None.
    done = subprocess.run(["git", "show", spec], capture_output=True, text=True, check=False)
    return done.stdout if done.returncode == 0 else None


def _pin(text: str | None) -> str | None:
    found = PIN.search(text or "")
    return found["pin"] if found else None


def _note(text: str | None) -> str:
    note: object = json.loads(text).get("meta", {}).get("rehashed", "") if text else ""
    return note if isinstance(note, str) else ""


if __name__ == "__main__":
    sys.exit(main())
