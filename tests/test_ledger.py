"""The golden ledger: every recorded decision of the previous implementation, recomputed here."""

import functools
import hashlib
import re
from collections.abc import Iterable, Mapping
from pathlib import Path

import pytest
from pydantic import JsonValue, TypeAdapter

from herdr_pwa.decisions import (
    CURSOR_KEYS,
    DISCONNECTED_AFTER,
    IDLE_CADENCE,
    MAX_TAPPABLE_OPTION,
    SECTIONS,
    TAPPABLE_SHAPES,
    WORKING_CADENCE,
    AgentRow,
    Freshness,
    HerdDecision,
    ScreenDecision,
    decide_herd,
    decide_screen,
)
from herdr_pwa.engine import BlockedPrompt, Snapshot, rank
from tests import _foreign

REPO = _foreign.REPO
LEDGER_PATH = _foreign.LEDGER_PATH
# Pinned so the golden file can only change by a reviewed edit to this line.
LEDGER_SHA256 = "3889d0b4fbb8ce31e0ac73bf714d1a21ee59651c929299e7a4cede78296b2211"
FOREIGN_CORPUS = REPO / "tests" / "foreign_corpus.json"
FOREIGN_PREFIX = "foreign/"

# Every deliberate divergence from the ledger, by entry id, with the reason it is better.
DIVERGENCES: dict[str, str] = {
    "foreign/relay/qodercli-single-question.ansi": (
        "qoder prints its footer verb-first ('Enter select'), a convention opencode "
        "shares and the verb-first grammar now reads; the Enter select pair is a real "
        "offer this screen always advertised, and it is now named as a hint. Enter was "
        "always sendable from the pad, so what changed is the label, not the keys."
    ),
    "foreign/relay/qodercli-single-question-with-text.ansi": (
        "Same verb-first footer ('Enter select'); the offer lifts and the screen with "
        "hints but no numbered run now reads unknown, which is honest: something is "
        "asked here that we deliberately do not drive."
    ),
    "foreign/relay/qodercli-first-question-multi-question.ansi": (
        "Same verb-first footer; the Enter select pair lifts onto a multi-question "
        "wizard whose rows stay unclaimed, so nothing tappable appears prematurely."
    ),
    "foreign/relay/qodercli-multi-questions-and-notes.ansi": (
        "Same verb-first footer; the Enter select pair lifts, and unknown is the "
        "honest shape for a notes screen we do not model."
    ),
    "foreign/relay/qodercli-second-question-single.ansi": (
        "Same verb-first footer; the Enter select pair lifts. The screen counts two "
        "questions, and no row claims to end one."
    ),
    "foreign/relay/qodercli-submit-answers.ansi": (
        "Same verb-first footer ('Enter select'); the offer lifts onto a submit "
        "screen with no rows, so unknown names it rather than none."
    ),
    "foreign/collie/omp--select-multi.txt": (
        "omp draws its checklist inside a box; the edge-strip unboxes the rows, so "
        "the checkbox evidence is finally seen and the shape reads multi-select, with "
        "no tappable rows until a cursor the text can see exists."
    ),
    "foreign/collie/omp--select-multi-checked.txt": (
        "Same boxed checklist; unboxed, its tick evidence reads as multi-select."
    ),
    "foreign/collie/omp--select-multi-review.txt": (
        "omp's review step after its checklist: a summary row and a Submit cursor, no "
        "checkboxes and no toggle verb. The footer is verb-first ('Enter submit'), so "
        "the Enter submit pair lifts and a screen with hints but no live run reads "
        "unknown where the ledger said none, naming Enter as a hint."
    ),
}

# CSI, OSC and two-byte escapes, exactly as the ledger's decoder stripped them.
_ANSI = re.compile(r"\x1b(?:\[[0-9;?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")


def _obj(value: JsonValue) -> dict[str, JsonValue]:
    assert isinstance(value, dict)
    return value


def _objects(value: JsonValue) -> list[dict[str, JsonValue]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


# The golden file and the captures it names, read whole: this test owns their shape.
_DOCUMENT = TypeAdapter(dict[str, JsonValue])

LEDGER = _DOCUMENT.validate_json(LEDGER_PATH.read_bytes())
META = _obj(LEDGER["meta"])
SCREENS = _obj(LEDGER["screens"])
HERDS = _obj(LEDGER["herd"])


def _now() -> float:
    now = META["now"]
    assert isinstance(now, float)
    return now


NOW = _now()


def _cases(entries: dict[str, JsonValue]) -> list[object]:
    return [
        pytest.param(
            key,
            id=key,
            marks=[pytest.mark.xfail(strict=True, reason=DIVERGENCES[key])]
            if key in DIVERGENCES
            else [],
        )
        for key in sorted(entries)
    ]


def _plain_text(path: Path) -> str:
    raw = path.read_text(encoding="utf-8", errors="replace")
    return _ANSI.sub("", raw).replace("\r\n", "\n").replace("\r", "")


def _str(value: object) -> str:
    assert isinstance(value, str)
    return value


@pytest.fixture(scope="session")
def foreign_root(request: pytest.FixtureRequest) -> Path:
    option: object = request.config.getoption("--foreign-root")
    # The option already defaults to $HP_FOREIGN_CAPTURES or the per-user cache; see conftest.
    return Path(option) if isinstance(option, str) and option else _foreign.DEFAULT_ROOT


@functools.cache
def _corpus_problems(root: Path) -> tuple[str, ...]:
    return tuple(_foreign.problems(root))


def _foreign_path(root: Path, capture_id: str) -> Path:
    # Never a skip: a missing corpus once silently dropped 114 of 163 screens in every worktree.
    if found := _corpus_problems(root):
        pytest.fail(_foreign.explain(root, list(found)), pytrace=False)
    clone, _, name = capture_id.removeprefix(FOREIGN_PREFIX).partition("/")
    source = _obj(_obj(META["foreign_sources"])[clone])
    return root / clone / _str(source["captures"]) / name


def _decoded(capture_id: str, foreign_root: Path) -> str:
    entry = _obj(SCREENS[capture_id])
    found = _obj(entry["input"])
    path = (
        _foreign_path(foreign_root, capture_id)
        if capture_id.startswith(FOREIGN_PREFIX)
        else REPO / capture_id
    )
    text = _plain_text(path) if found["decode"] == "plain_text" else path.read_text("utf-8")
    # Only a byte-identical input makes a differing verdict a decision rather than a fixture.
    assert hashlib.sha256(text.encode("utf-8")).hexdigest() == found["sha256"]
    return text


def _array(items: Iterable[JsonValue]) -> list[JsonValue]:
    return list(items)


def _screen_json(decision: ScreenDecision) -> dict[str, JsonValue]:
    steps = decision.steps
    return {
        "verdict": {
            "shape": str(decision.shape),
            "options": len(decision.options),
            "digits": _array(decision.digits),
            "rows_answer": decision.rows_answer,
            "hints": [h.key for h in decision.hints],
            "ticked": _array(decision.ticked),
            "checkable": decision.checkable,
            "steps": None if steps is None else [steps.answered, steps.total],
        },
        "render": {
            "answer_hints": [h.key for h in decision.answer_hints],
            "rows_toggle": decision.rows_toggle,
            "corroborated": decision.corroborated,
            "drew_rows": decision.drew_rows,
            "asked": decision.asked,
            "is_choice": decision.is_choice,
            "numbers": [o.number for o in decision.options],
        },
    }


def _freshness_json(fresh: Freshness) -> dict[str, JsonValue]:
    return {
        "age": fresh.age,
        "age_label": fresh.age_label,
        "condition": str(fresh.condition),
        "stale": fresh.stale,
        "disconnected": fresh.disconnected,
        "degraded": fresh.degraded,
        "failure": fresh.failure,
    }


def _row_json(row: AgentRow, prompt: JsonValue) -> dict[str, JsonValue]:
    found: dict[str, JsonValue] = {
        "target": row.target,
        "status": str(row.status),
        "effective_status": str(row.effective_status),
        "feed_key": _array(row.feed_key),
        "prompt": prompt,
    }
    if row.answer is not None:
        found["answer_digits"] = _array(row.answer.digits)
        found["rows_answer"] = row.answer.rows_answer
        found["answer_hints"] = [h.key for h in row.answer.answer_hints]
    return found


def _herd_json(
    herd: HerdDecision, prompt_ids: dict[str, JsonValue], freshness: list[JsonValue]
) -> dict[str, JsonValue]:
    return {
        "order": [row.session for row in herd.rows],
        "sections": [
            {
                "key": str(s.key),
                "label": s.label,
                "count": s.count,
                "sessions": [row.session for row in s.rows],
            }
            for s in herd.sections
        ],
        "counts": {str(status): count for status, count in herd.counts.items()},
        "cadence": herd.cadence,
        "freshness": freshness,
        "agents": {row.session: _row_json(row, prompt_ids.get(row.session)) for row in herd.rows},
    }


def _snapshot(entry: dict[str, JsonValue]) -> Snapshot:
    raw = _obj(_DOCUMENT.validate_json((REPO / _str(entry["snapshot"])).read_bytes())["result"])
    snapshot = _obj(raw["snapshot"])
    overrides = _obj(entry["status_overrides"])
    for agent in _objects(snapshot.get("agents")):
        agent["agent_status"] = overrides.get(_str(agent["pane_id"]), agent["agent_status"])
    return Snapshot.from_raw(snapshot)


def _unread(value: JsonValue) -> Mapping[str, bool] | None:
    if not isinstance(value, dict):
        return None
    return {key: flag for key, flag in value.items() if isinstance(flag, bool)}


def _prompts(
    snapshot: Snapshot, prompt_ids: dict[str, JsonValue], foreign_root: Path
) -> dict[str, BlockedPrompt]:
    return {
        agent.session: BlockedPrompt.parse(
            _decoded(_str(prompt_ids[agent.session]), foreign_root),
            target=agent.target,
            session=agent.session,
        )
        for agent in snapshot.agents
        if agent.session in prompt_ids
    }


def test_the_golden_file_is_the_one_frozen() -> None:
    assert hashlib.sha256(LEDGER_PATH.read_bytes()).hexdigest() == LEDGER_SHA256


def test_divergences_name_real_entries() -> None:
    assert set(DIVERGENCES) <= set(SCREENS) | set(HERDS)


def test_constants_match_the_ledger() -> None:
    constants = _obj(META["constants"])
    assert constants["IDLE_CADENCE"] == IDLE_CADENCE
    assert constants["WORKING_CADENCE"] == WORKING_CADENCE
    assert constants["DISCONNECTED_AFTER"] == DISCONNECTED_AFTER
    assert constants["MAX_TAPPABLE_OPTION"] == MAX_TAPPABLE_OPTION
    assert constants["CURSOR_KEYS"] == sorted(CURSOR_KEYS)
    assert constants["TAPPABLE_SHAPES"] == sorted(str(s) for s in TAPPABLE_SHAPES)
    assert constants["SECTIONS"] == [[str(key), label] for key, label in SECTIONS]
    assert constants["URGENCY"] == {str(key): rank(key) for key, _ in SECTIONS}


def test_foreign_corpus_index_agrees_with_the_ledger() -> None:
    corpus = _obj(_DOCUMENT.validate_json(FOREIGN_CORPUS.read_bytes())["captures"])
    listed = META["verdict_fields"]
    fields = [_str(f) for f in (listed if isinstance(listed, list) else [])]
    foreign = {
        key.removeprefix(FOREIGN_PREFIX): _obj(_obj(entry)["verdict"])
        for key, entry in SCREENS.items()
        if key.startswith(FOREIGN_PREFIX)
    }
    assert set(foreign) == set(corpus)
    for key, verdict in foreign.items():
        row = _obj(corpus[key])
        assert {f: row[f] for f in fields} == verdict, key


@pytest.mark.parametrize("capture_id", _cases(SCREENS))
def test_screen_matches_the_ledger(capture_id: str, foreign_root: Path) -> None:
    decision = decide_screen(BlockedPrompt.parse(_decoded(capture_id, foreign_root)))
    expected = {k: v for k, v in _obj(SCREENS[capture_id]).items() if k != "input"}
    assert _screen_json(decision) == expected


@pytest.mark.parametrize("herd_id", _cases(HERDS))
def test_herd_matches_the_ledger(herd_id: str, foreign_root: Path) -> None:
    entry = _obj(HERDS[herd_id])
    given = _obj(entry["input"])
    snapshot = _snapshot(given)
    prompt_ids = _obj(given["prompts"])
    prompts = _prompts(snapshot, prompt_ids, foreign_root)
    unread = _unread(given["unread"])
    freshness: list[JsonValue] = []
    for case in _objects(entry["freshness"]):
        taken_at, error = case["taken_at"], case["error"]
        assert taken_at is None or isinstance(taken_at, float)
        assert error is None or isinstance(error, str)
        herd = decide_herd(
            snapshot.agents,
            prompts=prompts,
            unread=unread,
            now=NOW,
            taken_at=taken_at,
            error=error,
        )
        freshness.append(
            {
                "taken_at": taken_at,
                "age": herd.freshness.age,
                "error": error,
                "result": _freshness_json(herd.freshness),
            }
        )
    herd = decide_herd(
        snapshot.agents, prompts=prompts, unread=unread, now=NOW, taken_at=None, error=None
    )
    expected = {k: v for k, v in entry.items() if k != "input"}
    assert _herd_json(herd, prompt_ids, freshness) == expected
