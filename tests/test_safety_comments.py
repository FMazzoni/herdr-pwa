"""Comments whose consequence is a safety guarantee keep naming it, so an edit cannot drop it unseen."""

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

import herdr_pwa

SRC = Path(herdr_pwa.__file__).parent
RULE = "comments keep the consequence (AGENTS.md:30)"


@dataclass(frozen=True, slots=True)
class Pin:
    """A phrase the comment beside an anchor must keep, and the guarantee it states."""

    file: str
    # A substring of exactly one line; empty means the module docstring.
    anchor: str
    phrase: str
    guarantee: str


PINS = (
    Pin("auth.py", "", "local uid can forge", "the identity header is forgeable locally"),
    Pin(
        "auth.py",
        "any_login: bool = False",
        "every header identity is refused",
        "no pin fails closed",
    ),
    Pin("auth.py", 'served_host: str = ""', "empty refuses all", "no served Host fails closed"),
    Pin("audit.py", "", "logged first", "a write is audited before it is made"),
    Pin(
        "audit.py",
        "class AuditUnavailableError",
        "must not happen",
        "an unrecordable write is not made",
    ),
    Pin(
        "writes/resolve.py",
        "def attempt(",
        "cannot record refuses the write",
        "an unrecordable write is refused",
    ),
    Pin(
        "writes/resolve.py",
        "said = doubt(resolution.agent, writer.witness)",
        "keys would reach another agent",
        "a write checks the session record itself",
    ),
    Pin(
        "writes/resolve.py",
        "with memory.in_flight.hold(key) as mine:",
        "Refused, not queued",
        "a second write to a changing target is refused",
    ),
    Pin(
        "writes/answer.py",
        'now = control_token(prompt) if prompt is not None else ""',
        "stale",
        "a key drawn for another screen is refused",
    ),
    Pin(
        "writes/answer.py",
        "entry.outcome(audit_result(Outcome.ACCEPTED, Evidence.DELIVERED))",
        "still leaves a record",
        "a sent key is audited before it is watched",
    ),
    Pin(
        "writes/prompt.py",
        "if agent.session:",
        "never on the recyclable address",
        "writes are keyed on the session, not the pane",
    ),
    Pin(
        "engine/model.py",
        "class Seat:",
        "None only for a pane just made",
        "a write names the session that must hold the pane",
    ),
    Pin(
        "engine/dialog.py",
        "def _from_record(",
        "the screen differs",
        "a recorded question counts only when the screen matches it",
    ),
    Pin(
        "engine/dialog.py",
        "def _footer_lines(",
        "offers no dialog key",
        "a bar below the dialog adds no keys",
    ),
    Pin(
        "writes/lifecycle.py",
        '"HISTFILE": ""',
        "HISTFILE keeps",
        "a launch prompt stays out of shell history",
    ),
    Pin("statedir.py", "", "each mode checked", "state is 0700 and 0600, verified"),
    Pin(
        "statedir.py",
        "fd, name = tempfile.mkstemp(",
        "never reused",
        "a planted scratch file is not written through",
    ),
    Pin(
        "statedir.py",
        "os.fsync(handle.fileno())",
        "a crash can leave the name empty",
        "a rename lands only after its data",
    ),
    Pin(
        "statedir.py",
        "with locked(lock_path(path)):",
        "drops what landed between",
        "a rewrite reads under its lock",
    ),
    Pin("notify_state.py", "", "0600", "the notifier's files are private"),
    Pin("files/reader.py", "def _confined(", "so refuse", "no /proc refuses the read"),
    Pin(
        "files/git.py",
        "_READ: Final",
        "fsmonitor, hook",
        "a read runs nothing a repository names",
    ),
)


@pytest.mark.parametrize("pin", PINS, ids=[f"{pin.file}:{pin.anchor or 'module'}" for pin in PINS])
def test_a_safety_comment_keeps_its_consequence(pin: Pin) -> None:
    comment = comment_at(SRC / pin.file, pin.anchor)
    where = f"the comment at {pin.anchor!r}" if pin.anchor else "the module docstring"
    assert pin.phrase in comment, (
        f"{pin.file}: {where} no longer says {pin.phrase!r}, the guarantee that "
        f"{pin.guarantee}; {RULE}. Got: {comment!r}"
    )


def comment_at(path: Path, anchor: str) -> str:
    """The comment beside the one line holding ``anchor``, whitespace flattened."""
    source = path.read_text(encoding="utf-8")
    if not anchor:
        return _flat(ast.get_docstring(ast.parse(source)) or "")
    lines = source.splitlines()
    found = [index for index, line in enumerate(lines) if anchor in line]
    assert len(found) == 1, f"{path.name}: anchor {anchor!r} is on {len(found)} lines, not one"
    return _flat(" ".join(_above(lines, found[0]) + _below(lines, found[0])))


def _flat(text: str) -> str:
    return " ".join(text.replace('"""', " ").replace("#", " ").split())


def _above(lines: list[str], index: int) -> list[str]:
    start = index
    while start and lines[start - 1].strip().startswith("#"):
        start -= 1
    return lines[start:index]


def _below(lines: list[str], index: int) -> list[str]:
    taken: list[str] = []
    in_docstring = False
    for line in lines[index + 1 :]:
        stripped = line.strip()
        if in_docstring or stripped.startswith(("#", '"""')):
            taken.append(stripped)
            # A docstring that opens without closing runs on to the line that closes it.
            in_docstring ^= stripped.count('"""') == 1
        else:
            break
    return taken
