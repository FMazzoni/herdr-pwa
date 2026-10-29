"""No fixture carries a real address, home path, request id, session id or commit of this repo."""

import re
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures"

EMAIL = re.compile(
    r"""
    [A-Za-z0-9._%+-]+ @  # the mailbox
    (?P<domain>[A-Za-z0-9-]+ (?:\.[A-Za-z0-9-]+)+)  # a dotted domain
    """,
    re.VERBOSE,
)
# RFC 2606 reserves these for documentation, so no real mailbox sits behind them.
PLACEHOLDER_DOMAIN = re.compile(
    r"""
    (?:^|\.)  # the whole domain, or its last labels
    (?:example\.(?:com|net|org)|example|test|invalid|localhost)$  # RFC 2606's reserved names
    """,
    re.VERBOSE,
)
# collie's vendored screens type this stand-in into a form; they are kept byte for byte.
PLACEHOLDER_EMAILS = frozenset({"a@b.com"})
# Home directories, also as Claude Code spells them in a project folder name (-home-<name>-).
HOME = re.compile(
    r"""
    (?:/|-) (?:var[/-])? (?:home|Users) [/-]  # a home folder, or one spelt into a folder name
    (?P<name>[A-Za-z0-9._]+)  # whose home it is
    """,
    re.VERBOSE,
)
PLACEHOLDER_HOMES = frozenset({"user"})
# Anthropic's API request ids; a fixture spells them req_SYNTHETIC… instead.
REQUEST_ID = re.compile(r"req_011C[A-Za-z0-9]+")
# A short or full commit id, standing alone rather than inside a longer hash or name.
HEX = re.compile(r"(?<![0-9A-Za-z])[0-9a-f]{7,40}(?![0-9A-Za-z])")
# Any 8-4-4-4-12 hex id, the shape of a session, record or tool-call id.
UUID = re.compile(
    r"""
    (?<![0-9A-Za-z])  # not the tail of a longer token
    [0-9a-fA-F]{8} (?:-[0-9a-fA-F]{4}){3} -[0-9a-fA-F]{12}  # 8-4-4-4-12 hex
    (?![0-9A-Za-z])  # nor its head
    """,
    re.VERBOSE,
)
# The README's schemes: session ids 5e55NNNN-0000-…, other record ids 0000NNNN-0000-….
PLACEHOLDER_UUID = re.compile(
    r"""
    (?:5e55|0000) [0-9a-f]{4}  # the scheme's prefix, then the record's number
    -0000 (?:-[0-9a-f]{4}){2} -[0-9a-f]{12}  # a zero second group
    """,
    re.VERBOSE,
)
# The README's hand-built notice logs number their records under these two prefixes instead.
HAND_BUILT = "transcript/notices/"
HAND_BUILT_UUID = re.compile(
    r"""
    (?:5a5c|6d1e) [0-9a-f]{4}  # the notice logs' prefix, then the record's number
    -0000 (?:-[0-9a-f]{4}){2} -[0-9a-f]{12}  # a zero second group
    """,
    re.VERBOSE,
)


def _texts() -> Iterator[tuple[str, str]]:
    for path in sorted(FIXTURES.rglob("*")):
        if path.is_file():
            yield str(path.relative_to(FIXTURES)), path.read_text("utf-8", errors="replace")


def _real_email(found: re.Match[str]) -> bool:
    if found[0] in PLACEHOLDER_EMAILS:
        return False
    return not PLACEHOLDER_DOMAIN.search(found["domain"].lower())


def test_every_email_is_a_placeholder() -> None:
    found = [
        (name, hit.group(0))
        for name, text in _texts()
        for hit in EMAIL.finditer(text)
        if _real_email(hit)
    ]
    assert found == []


def test_every_home_directory_is_a_placeholder() -> None:
    found = [
        (name, hit[0])
        for name, text in _texts()
        for hit in HOME.finditer(text)
        if hit["name"] not in PLACEHOLDER_HOMES
    ]
    assert found == []


def test_no_request_id_is_real() -> None:
    found = [
        (name, hit.group(0)[:12]) for name, text in _texts() for hit in REQUEST_ID.finditer(text)
    ]
    assert found == []


def _placeholder_uuid(name: str, uuid: str) -> bool:
    if PLACEHOLDER_UUID.fullmatch(uuid):
        return True
    return name.startswith(HAND_BUILT) and HAND_BUILT_UUID.fullmatch(uuid) is not None


def test_every_uuid_is_a_placeholder() -> None:
    # File names count too: omp and Claude Code name a transcript after its session id.
    found = sorted(
        {
            (name, hit[0])
            for name, text in _texts()
            for hit in UUID.finditer(f"{name}\n{text}")
            if not _placeholder_uuid(name, hit[0])
        }
    )
    assert found == []


def _commits(candidates: set[str]) -> set[str]:
    git = shutil.which("git")
    assert git is not None, "the git marker guards every caller"
    lines = "".join(f"{candidate}\n" for candidate in sorted(candidates))
    # git answers one line per name, in order, and a name it cannot find reads "<name> missing".
    command = [git, "-C", str(REPO), "cat-file", "--batch-check=%(objecttype)"]
    done = subprocess.run(command, input=lines, capture_output=True, text=True, check=True)  # noqa: S603 - a fixed git or python command line, never shell input
    answers = done.stdout.splitlines()
    return {
        name
        for name, answer in zip(sorted(candidates), answers, strict=True)
        if answer.startswith("commit")
    }


@pytest.mark.git
def test_no_fixture_cites_a_commit_of_this_repo() -> None:
    # A cited commit means the text came from a real run here, not a synthetic one.
    cited: dict[str, str] = {}
    for name, text in _texts():
        cited.update(dict.fromkeys((hit.group(0) for hit in HEX.finditer(text)), name))
    assert sorted((cited[sha], sha) for sha in _commits(set(cited))) == []
