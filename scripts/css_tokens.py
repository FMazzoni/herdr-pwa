"""Fail when app.css carries a literal colour or px size outside its :root token block."""

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

ROOT: Final = Path(__file__).resolve().parents[1]
CSS: Final = ROOT / "src" / "herdr_pwa" / "web" / "static" / "app.css"
# One exception per line: the declaration exactly as app.css writes it, `  # `, then why.
ALLOW: Final = ROOT / "scripts" / "css_tokens.allow"

COMMENT: Final = re.compile(r"/\*.*?\*/", re.DOTALL)
ROOT_BLOCK: Final = re.compile(r":root\s*\{[^}]*\}")
DECLARATION: Final = re.compile(r"([\w-]+)\s*:\s*([^;{}]+)")
LITERAL: Final = re.compile(
    r"#[0-9a-fA-F]{3,8}\b"
    r"|\b(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch|color)\("
    r"|(?<![\w.-])\d*\.?\d+px\b"
)


@dataclass(frozen=True, slots=True)
class Finding:
    """One literal outside the token block: where, what, and the declaration it sits in."""

    line: int
    literal: str
    declaration: str

    def __str__(self) -> str:
        return f"app.css:{self.line}: {self.literal!r} in `{self.declaration}`; use a :root token"


def _blank(match: re.Match[str]) -> str:
    # Keeps newlines so line numbers still point at the source.
    return re.sub(r"[^\n]", " ", match.group(0))


def scan(css: str) -> list[Finding]:
    """Every literal colour or px value in ``css`` outside comments and the :root block."""
    text = ROOT_BLOCK.sub(_blank, COMMENT.sub(_blank, css))
    found: list[Finding] = []
    for decl in DECLARATION.finditer(text):
        declaration = f"{decl.group(1)}: {decl.group(2).strip()}"
        line = text.count("\n", 0, decl.start()) + 1
        found.extend(
            Finding(line, hit.group(0), declaration) for hit in LITERAL.finditer(decl.group(2))
        )
    return found


def check(css: str, allowed: dict[str, str]) -> list[str]:
    """The problems in ``css``: unallowed literals, then allowlist entries nothing uses."""
    findings = scan(css)
    problems = [str(f) for f in findings if f.declaration not in allowed]
    used = {f.declaration for f in findings}
    problems.extend(
        f"allowlist entry matches nothing, remove it: `{d}`" for d in allowed if d not in used
    )
    return problems


def read_allow(path: Path) -> dict[str, str]:
    """The allowlist file as declaration -> reason; a line with no reason is refused."""
    allowed: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip() or raw.startswith("#"):
            continue
        declaration, _, reason = raw.partition("  # ")
        if not reason.strip():
            raise SystemExit(f"{path.name}: give a reason after `  # `: {raw!r}")
        allowed[declaration.strip()] = reason.strip()
    return allowed


class Args(argparse.Namespace):
    """The command line, typed."""

    css: Path = CSS
    allow: Path = ALLOW


def main(argv: list[str]) -> int:
    """Check a stylesheet (app.css by default) against the allowlist; non-zero on any problem."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("css", nargs="?", type=Path, default=CSS)
    parser.add_argument("--allow", type=Path, default=ALLOW)
    args = parser.parse_args(argv, namespace=Args())
    path = args.css
    problems = check(path.read_text(encoding="utf-8"), read_allow(args.allow))
    for problem in problems:
        print(problem)
    if problems:
        print(f"css tokens: {len(problems)} problem(s) in {path}")
        return 1
    print(f"css tokens: no literal colour or px outside :root in {path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
