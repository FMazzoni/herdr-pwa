#!/usr/bin/env python3
"""Refuse a push that carries a foreign root, an unlisted ref or a denied identifier."""

# Install via .pre-commit-config.yaml, or `ln -s ../../scripts/hooks/pre_push.py .git/hooks/pre-push`
# to check every ref: pre-commit passes on only the first ref with new commits, and none without.

import os
import re
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

ZERO = "0" * 40
DEFAULT_PATTERNS = Path("~/.config/git/push-deny-patterns")
# Branches and v-tags only: refs/stash and ad hoc tags hold history that was never meant to leave.
ALLOWED_REF = re.compile(r"refs/heads/.+|refs/tags/v.*")
# Each commit's header (identities and message) sits between these separators, its diff after.
COMMIT_START, HEADER_END = "\x1e", "\x1f"
LOG_FORMAT = "%x1e%H%nauthor %an <%ae>%ncommitter %cn <%ce>%n%B%x1f"


@dataclass(frozen=True)
class Update:
    """One ref the push moves: what is sent, where it lands and what the remote had there."""

    local_ref: str
    local_sha: str
    remote_ref: str
    remote_sha: str


def _git(*args: str) -> str:
    done = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {done.stderr.strip()}")
    return done.stdout


def _from_stdin(lines: list[str]) -> list[Update]:
    updates: list[Update] = []
    for line in lines:
        if line.strip():
            local_ref, local_sha, remote_ref, remote_sha = line.rsplit(maxsplit=3)
            updates.append(Update(local_ref, local_sha, remote_ref, remote_sha))
    return updates


def _from_pre_commit(env: dict[str, str]) -> list[Update]:
    # pre-commit reads git's stdin itself and passes on only the first ref that sends commits.
    local_ref = env.get("PRE_COMMIT_LOCAL_BRANCH", "HEAD")
    local_sha = env.get("PRE_COMMIT_TO_REF") or _git("rev-parse", local_ref).strip()
    remote_sha = env.get("PRE_COMMIT_FROM_REF") or ZERO
    return [Update(local_ref, local_sha, env["PRE_COMMIT_REMOTE_BRANCH"], remote_sha)]


def _ref_problem(update: Update) -> str | None:
    if update.local_ref == "refs/stash" or not ALLOWED_REF.fullmatch(update.remote_ref):
        return f"{update.local_ref} -> {update.remote_ref}: only refs/heads/* and refs/tags/v* may be pushed"
    return None


def _root_problem(update: Update, main_roots: set[str]) -> str | None:
    roots = set(_git("rev-list", "--max-parents=0", f"{update.local_sha}^{{commit}}").split())
    if roots - main_roots:
        return f"{update.remote_ref}: its history does not start at main's root commit"
    return None


def _exclusions(remote: str, update: Update) -> list[str]:
    excluded = [f"--remotes={remote}"]
    # The remote's old tip is only known here when this clone has fetched it.
    known = (
        subprocess.run(["git", "cat-file", "-e", update.remote_sha], check=False).returncode == 0
    )
    if update.remote_sha != ZERO and known:
        excluded.append(update.remote_sha)
    return excluded


def _texts(remote: str, update: Update) -> Iterator[tuple[str, str]]:
    """Yield (where, text) for every identity, message, file name and added line not yet on the remote."""
    if _git("cat-file", "-t", update.local_sha).strip() == "tag":
        yield f"tag {update.remote_ref}", _git("cat-file", "tag", update.local_sha)
    log = _git(
        "log", f"--format={LOG_FORMAT}", "--patch", "--unified=0", "--no-renames",
        "--diff-merges=first-parent", "--no-ext-diff", "--no-textconv", "--no-color",
        update.local_sha, "--not", *_exclusions(remote, update),
    )  # fmt: skip
    for chunk in log.split(COMMIT_START)[1:]:
        header, _, diff = chunk.partition(HEADER_END)
        sha = header.split("\n", 1)[0][:12]
        yield f"commit {sha} message or identity", header
        for line in diff.splitlines():
            # Added lines, and the new file names in `+++ b/…` and `diff --git` headers.
            if line.startswith(("+", "diff --git ")):
                yield f"commit {sha} diff", line


def _load_patterns(env: dict[str, str]) -> list[tuple[int, re.Pattern[str]]] | str:
    setting = env.get("HP_PUSH_DENY_PATTERNS", "")
    if setting == "none":
        return []
    path = Path(setting or DEFAULT_PATTERNS).expanduser()
    if not path.is_file():
        return f"no deny patterns at {path}; create it, or set HP_PUSH_DENY_PATTERNS=none to skip the scan"
    patterns: list[tuple[int, re.Pattern[str]]] = []
    for number, raw in enumerate(path.read_text("utf-8").splitlines(), start=1):
        line = raw.strip()
        if line and not line.startswith("#"):
            try:
                patterns.append((number, re.compile(line, re.IGNORECASE)))
            except re.error as error:
                return f"{path} line {number} is not a valid regex: {error}"
    return patterns


def _scan(remote: str, update: Update, patterns: list[tuple[int, re.Pattern[str]]]) -> list[str]:
    found: list[str] = []
    for where, text in _texts(remote, update):
        # The pattern's line number, never the matched text, so the refusal leaks nothing.
        hits = {number for number, pattern in patterns if pattern.search(text)}
        found.extend(f"{where}: matches deny pattern on line {n}" for n in sorted(hits))
    return list(dict.fromkeys(found))


def check(remote: str, updates: list[Update], env: dict[str, str]) -> list[str]:
    """Return every reason to refuse these ref updates; empty means the push may go."""
    sent = [update for update in updates if update.local_sha != ZERO]
    if not sent:
        return []
    patterns = _load_patterns(env)
    if isinstance(patterns, str):
        return [patterns]
    try:
        main_roots = set(_git("rev-list", "--max-parents=0", "refs/heads/main").split())
    except RuntimeError:
        return ["no local main branch to compare root commits with"]
    problems: list[str] = []
    for update in sent:
        ref = _ref_problem(update)
        problems.extend([ref] if ref else [])
        root = _root_problem(update, main_roots)
        problems.extend([root] if root else [])
        problems.extend(_scan(remote, update, patterns))
    return problems


def main(argv: list[str]) -> int:
    """Read the push the way git or pre-commit hands it over, and refuse it on any problem."""
    env = dict(os.environ)
    if "PRE_COMMIT_REMOTE_BRANCH" in env:
        remote, updates = env.get("PRE_COMMIT_REMOTE_NAME", ""), _from_pre_commit(env)
    else:
        remote, updates = (argv[0] if argv else ""), _from_stdin(sys.stdin.read().splitlines())
    try:
        problems = check(remote, updates, env)
    except RuntimeError as error:
        problems = [str(error)]
    for problem in problems:
        print(f"pre-push refused: {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
