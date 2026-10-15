"""Clone or fetch the foreign capture repos at the ledger's pinned commits, then check them."""

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests._foreign import ORIGINS, default_root, explain, head, problems, sources  # noqa: E402


def git(clone: Path, *args: str) -> bool:
    """Run one git command in ``clone``, echoing it; True when it succeeded."""
    print(f"corpus: git -C {clone} {' '.join(args)}", flush=True)
    return subprocess.run(["git", "-C", str(clone), *args], check=False).returncode == 0


def pin(clone: Path, url: str, commit: str) -> None:
    """Bring one clone to ``commit``, fetching only that commit; never deletes anything."""
    if head(clone) == commit:
        return
    if clone.exists() and head(clone) is None:
        # Not ours to clear: the check below names it and the operator decides.
        return
    clone.mkdir(parents=True, exist_ok=True)
    if head(clone) is None and not git(clone, "init", "--quiet"):
        return
    if git(clone, "fetch", "--quiet", "--depth", "1", url, commit):
        git(clone, "-c", "advice.detachedHead=false", "checkout", "--quiet", "--detach", commit)


def main(argv: list[str] | None = None) -> int:
    """Pin every clone under the corpus root (or only check, with --check)."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--check", action="store_true", help="check the pins, fetch nothing")
    parser.add_argument("--root", type=Path, default=None, help="default $HP_FOREIGN_CAPTURES")
    args = parser.parse_args(argv)
    root: Path = args.root or default_root()
    if not args.check:
        for name, source in sources().items():
            pin(root / name, ORIGINS[name], source["commit"])
    found = problems(root)
    if found:
        print(explain(root, found), file=sys.stderr)
        return 1
    print(f"corpus: {', '.join(sorted(sources()))} at the pinned commits under {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
