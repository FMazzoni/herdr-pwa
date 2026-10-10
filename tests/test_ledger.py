"""The golden ledger: every recorded decision of the previous implementation, recomputed here."""

import hashlib
import re
from collections.abc import Iterable, Mapping
from pathlib import Path

import pytest

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
from herdr_pwa.engine.dialog import BlockedPrompt
from herdr_pwa.engine.model import Snapshot, rank
from herdr_pwa.jsontypes import (
    JsonObject,
    JsonValue,
    as_array,
    as_object,
    loads,
    objects,
)

REPO = Path(__file__).resolve().parents[1]
LEDGER_PATH = REPO / "tests" / "fixtures" / "ledger.json"
# Pinned so the golden file can only change by a reviewed edit to this line.
LEDGER_SHA256 = "3889d0b4fbb8ce31e0ac73bf714d1a21ee59651c929299e7a4cede78296b2211"
FOREIGN_CORPUS = REPO / "tests" / "foreign_corpus.json"
FOREIGN_PREFIX = "foreign/"

# Every deliberate divergence from the ledger, by entry id, with the reason it is better.
DIVERGENCES: dict[str, str] = {}

# CSI, OSC and two-byte escapes, exactly as the ledger's decoder stripped them.
_ANSI = re.compile(r"\x1b(?:\[[0-9;?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")


def _obj(value: object) -> JsonObject:
    found = as_object(value)
    assert found is not None
    return found


LEDGER = _obj(loads(LEDGER_PATH.read_bytes()))
META = _obj(LEDGER["meta"])
SCREENS = _obj(LEDGER["screens"])
HERDS = _obj(LEDGER["herd"])


def _now() -> float:
    now = META["now"]
    assert isinstance(now, float)
    return now


NOW = _now()


def _cases(entries: JsonObject) -> list[object]:
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
    # The option already defaults to $HP_FOREIGN_CAPTURES; see conftest.
    if isinstance(option, str) and option:
        return Path(option)
    return REPO / "evidence" / "foreign-captures"


def _foreign_path(root: Path, capture_id: str) -> Path:
    clone, _, name = capture_id.removeprefix(FOREIGN_PREFIX).partition("/")
    source = _obj(_obj(META["foreign_sources"])[clone])
    directory = root / clone / _str(source["captures"])
    if not directory.is_dir():
        # Foreign captures are never vendored; their absence is normal off this machine.
        pytest.skip(f"foreign corpus not present under {root}")
    head = (root / clone / ".git" / "HEAD").read_text(encoding="utf-8").strip()
    # A clone at another commit is a different screen set wearing the same file names.
    assert head == source["commit"], f"{clone} is at {head}, not the pinned {source['commit']}"
    return directory / name


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


def _screen_json(decision: ScreenDecision) -> JsonObject:
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


def _freshness_json(fresh: Freshness) -> JsonObject:
    return {
        "age": fresh.age,
        "age_label": fresh.age_label,
        "condition": str(fresh.condition),
        "stale": fresh.stale,
        "disconnected": fresh.disconnected,
        "degraded": fresh.degraded,
        "failure": fresh.failure,
    }


def _row_json(row: AgentRow, prompt: JsonValue) -> JsonObject:
    found: JsonObject = {
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
    herd: HerdDecision, prompt_ids: JsonObject, freshness: list[JsonValue]
) -> JsonObject:
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


def _snapshot(entry: JsonObject) -> Snapshot:
    raw = _obj(_obj(loads((REPO / _str(entry["snapshot"])).read_bytes()))["result"])
    snapshot = _obj(raw["snapshot"])
    overrides = _obj(entry["status_overrides"])
    for agent in objects(snapshot.get("agents")):
        agent["agent_status"] = overrides.get(_str(agent["pane_id"]), agent["agent_status"])
    return Snapshot.from_raw(snapshot)


def _unread(value: JsonValue) -> Mapping[str, bool] | None:
    found = as_object(value)
    if found is None:
        return None
    return {key: flag for key, flag in found.items() if isinstance(flag, bool)}


def _prompts(
    snapshot: Snapshot, prompt_ids: JsonObject, foreign_root: Path
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
    corpus = _obj(_obj(loads(FOREIGN_CORPUS.read_bytes()))["captures"])
    fields = [_str(f) for f in as_array(META["verdict_fields"])]
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
    for case in objects(entry["freshness"]):
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
