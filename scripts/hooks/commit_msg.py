#!/usr/bin/env python3
"""Refuse a commit message with a long subject, a long body or a WIP or fixup subject."""

# Runs as git's commit-msg hook or pre-commit's commit-msg stage, given the message file's path;
# see .pre-commit-config.yaml to install it. A path of `-` reads the message from stdin.

import re
import sys
from pathlib import Path

MAX_SUBJECT = 72
MAX_SENTENCES = 5
# git writes these subjects itself, so they are never held to the rules.
MERGE = re.compile(r"Merge (branch|remote-tracking branch|tag|commit|pull request) ")
UNFINISHED = re.compile(r"(wip\b|fixup!|squash!|amend!)", re.IGNORECASE)
# A trailer line as git interpret-trailers reads one: `Token: value`.
TRAILER = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*: ")
# A sentence ends at ., ! or ? (a run counts once, closing quotes or brackets allowed) before a space or the end.
SENTENCE_END = re.compile(r"[.!?]+[\"')\]]*(?=\s|$)")
SCISSORS = "# ------------------------ >8 ------------------------"


def _lines(message: str) -> list[str]:
    kept = message.split(SCISSORS, 1)[0].splitlines()
    return [line.rstrip() for line in kept if not line.startswith("#")]


def _body(lines: list[str]) -> list[str]:
    body = lines[1:]
    paragraphs = "\n".join(body).strip().split("\n\n")
    # The last paragraph is dropped when every line in it is a trailer.
    last = paragraphs[-1].splitlines()
    if last and all(TRAILER.match(line) for line in last):
        paragraphs = paragraphs[:-1]
    kept = "\n\n".join(paragraphs).splitlines()
    return [line for line in kept if not line.startswith("Rejected:")]


def problems(message: str) -> list[str]:
    """Return every reason to refuse this commit message; empty means it may be committed."""
    lines = _lines(message)
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines:
        return []
    subject = lines[0]
    if MERGE.match(subject):
        return []
    found: list[str] = []
    if len(subject) > MAX_SUBJECT:
        found.append(f"the subject is {len(subject)} characters, over {MAX_SUBJECT}")
    if UNFINISHED.match(subject):
        found.append("the subject marks unfinished work (WIP, fixup!, squash! or amend!)")
    sentences = len(SENTENCE_END.findall("\n".join(_body(lines))))
    if sentences > MAX_SENTENCES:
        found.append(f"the body has {sentences} sentences, over {MAX_SENTENCES}")
    return found


def main(argv: list[str]) -> int:
    """Check the message file git or pre-commit names, and refuse it on any problem."""
    if not argv:
        print("usage: commit_msg.py MESSAGE_FILE", file=sys.stderr)
        return 2
    path = argv[-1]
    message = sys.stdin.read() if path == "-" else Path(path).read_text("utf-8")
    found = problems(message)
    for problem in found:
        print(f"commit-msg refused: {problem}", file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
