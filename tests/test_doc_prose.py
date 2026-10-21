"""The human-facing docs stay readable: defined acronyms, short cells, whole sentences."""

import functools
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
# Docs a person reads; the prose rules follow the writing-for-people skill.
PEOPLE = (
    "README.md",
    "SECURITY.md",
    "tests/fixtures/README.md",
    "tests/fixtures/transcript/lineage/README.md",
)
# Docs for agents are compressed on purpose, so only their paths are checked.
AGENTS = ("AGENTS.md", ".agents/skills/phone-look/SKILL.md")
# Acronyms a reader of a developer's README already knows.
KNOWN = frozenset(
    {"AI", "API", "CSS", "HTML", "HTTP", "HTTPS", "JS", "JSON", "MIT", "QR", "UI", "URL"}
)
CELL_WORDS = 12
PARAGRAPH_SENTENCES = 5
FRAGMENT_WORDS = 10
NAMES_PER_100 = 8.0
FILLER = (
    "please note",
    "simply",
    "it's important to",
    "it is important to",
    "basically",
    "obviously",
    "needless to say",
    "in order to",
)
# Allowances, each with its reason.
LONG_CELLS = {
    # The hermes/ and transcript/ rows belong to a parallel branch that is re-capturing them.
    "tests/fixtures/README.md": 18,
}

FENCE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)
COMMENT = re.compile(r"<!--.*?-->|<[^>\n]+>", re.DOTALL)
CODE = re.compile(r"`[^`\n]+`")
LINK = re.compile(r"\]\([^)]*\)")
ACRONYM = re.compile(r"\b[A-Z]{2,}(?=s?\b)")
SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z`\"(*0-9])")
PATHLIKE = re.compile(
    r"""
    [\w.-]+ (?:/[\w.-]*)+  # a path with a folder in it
  | [\w.-]+ \.(?:py|json|jsonl|md|toml|yaml|yml|html|css|js|txt)  # or a file this repo keeps
    """,
    re.VERBOSE,
)
SEARCH = ("", "src/herdr_pwa")
# A word in capitals stands for a value the reader fills in, as in KEY.png.
PLACEHOLDER = re.compile(r"(^|[/.-])[A-Z]{3,}([/.-]|$)")
OUTSIDE = {
    # A folder inside Playwright's browser cache, not in the repo.
    ".agents/skills/phone-look/SKILL.md": frozenset({"sys/lib/"}),
}
SKIPPED_DIRS = frozenset({".git", ".venv", "node_modules", ".worktrees", "__pycache__"})
# Link text that is a file name, such as [SECURITY.md](SECURITY.md), is not an acronym.
FILE_LINK = re.compile(r"\[[\w.]+\]\([^)]*\)")


@dataclass(frozen=True, slots=True)
class Block:
    """One paragraph, list item or table row of a doc, with the heading it sits under."""

    section: str
    text: str
    is_row: bool


def _blocks(text: str) -> Iterator[Block]:
    section, lines = "", list[str]()
    for raw in [*COMMENT.sub("", FENCE.sub("", text)).splitlines(), ""]:
        line = raw.strip()
        starts = line.startswith(("#", "|", "- ", "1. ")) or re.match(r"^\d+\. ", line)
        if (not line or starts) and lines:
            yield Block(section, " ".join(lines), is_row=False)
            lines = []
        if line.startswith("#"):
            section = line
        elif line.startswith("|"):
            yield Block(section, line, is_row=True)
        elif line:
            lines.append(re.sub(r"^(- |\d+\. )", "", line))


def _prose(text: str) -> str:
    return LINK.sub("]", CODE.sub("name", FILE_LINK.sub("", text)))


def _sentences(text: str) -> list[str]:
    return [s for s in SENTENCE_END.split(text.strip()) if s]


def _doc(name: str) -> str:
    return (REPO / name).read_text("utf-8")


def acronyms_undefined(text: str) -> list[str]:
    """Acronyms whose first use comes before a definition: ``Name (ABC)``, ``ABC (``, ``ABC, a``."""
    plain = _prose(COMMENT.sub("", FENCE.sub("", text)))
    found: list[str] = []
    for word in dict.fromkeys(m.group(0) for m in ACRONYM.finditer(plain)):
        defined = re.search(rf"\({word}\)|\b{word}s? \(|\b{word}, (a|an|the) ", plain)
        first = re.search(rf"\b{word}", plain)
        if word not in KNOWN and (defined is None or (first and first.start() < defined.start())):
            found.append(word)
    return found


def long_cells(text: str, limit: int) -> list[str]:
    """Table cells over *limit* words."""
    rows = (b.text for b in _blocks(text) if b.is_row and not set(b.text) <= set("|-: "))
    cells = (c.strip() for row in rows for c in row.strip("|").split("|"))
    return [c for c in cells if len(c.split()) > limit]


def long_paragraphs(text: str) -> list[str]:
    """Paragraphs with more sentences than the skill allows."""
    paragraphs = (b.text for b in _blocks(text) if not b.is_row)
    return [p[:60] for p in paragraphs if len(_sentences(_prose(p))) > PARAGRAPH_SENTENCES]


def fragment_sections(text: str) -> list[str]:
    """Sections whose sentences average under FRAGMENT_WORDS words: telegraphic prose."""
    by_section: dict[str, list[str]] = {}
    for block in _blocks(text):
        if not block.is_row:
            by_section.setdefault(block.section, []).extend(_sentences(_prose(block.text)))
    return [
        f"{section or '(top)'}: {sum(len(s.split()) for s in found) / len(found):.1f}"
        for section, found in by_section.items()
        if len(found) >= 3 and sum(len(s.split()) for s in found) / len(found) < FRAGMENT_WORDS
    ]


def names_per_100(text: str) -> float:
    """Backticked names per 100 words of prose, outside code blocks."""
    body = COMMENT.sub("", FENCE.sub("", text))
    return 100 * len(CODE.findall(body)) / max(1, len(body.split()))


@functools.cache
def _file_names() -> frozenset[str]:
    found: set[str] = set()
    for _parent, dirs, files in REPO.walk():
        dirs[:] = [d for d in dirs if d not in SKIPPED_DIRS]
        found.update(files)
    return frozenset(found)


def _exists(path: str, bases: list[Path]) -> bool:
    if "/" not in path:
        return path in _file_names()
    return any((base / path).exists() for base in bases)


def missing_paths(name: str, text: str) -> list[str]:
    """Backticked paths found nowhere: beside the doc, at the root, in src, or as a file name."""
    names = (m[0].strip("`") for m in CODE.finditer(FENCE.sub("", text)))
    paths = [n for n in names if PATHLIKE.fullmatch(n) and not PLACEHOLDER.search(n)]
    paths = [p for p in paths if p not in OUTSIDE.get(name, frozenset())]
    # Notes and screenshots under .agents/ are gitignored, so a fresh clone never has them.
    paths = [p for p in paths if not p.startswith(".agents/") or p.startswith(".agents/skills/")]
    bases = [(REPO / name).parent, *(REPO / s for s in SEARCH)]
    return [p for p in paths if not _exists(p, bases)]


@pytest.mark.parametrize("name", PEOPLE)
def test_acronyms_are_defined_before_use(name: str) -> None:
    assert acronyms_undefined(_doc(name)) == []


@pytest.mark.parametrize("name", PEOPLE)
def test_table_cells_are_short(name: str) -> None:
    assert long_cells(_doc(name), LONG_CELLS.get(name, CELL_WORDS)) == []


@pytest.mark.parametrize("name", PEOPLE)
def test_paragraphs_are_short(name: str) -> None:
    assert long_paragraphs(_doc(name)) == []


@pytest.mark.parametrize("name", PEOPLE)
def test_sections_are_whole_sentences(name: str) -> None:
    assert fragment_sections(_doc(name)) == []


@pytest.mark.parametrize("name", PEOPLE)
def test_code_names_are_sparse(name: str) -> None:
    assert names_per_100(_doc(name)) <= NAMES_PER_100


@pytest.mark.parametrize("name", PEOPLE)
def test_no_filler(name: str) -> None:
    lowered = _doc(name).lower()
    assert [phrase for phrase in FILLER if phrase in lowered] == []


@pytest.mark.parametrize("name", PEOPLE + AGENTS)
def test_backticked_paths_exist(name: str) -> None:
    assert missing_paths(name, _doc(name)) == []
