"""The golden ledger: every recorded decision, recomputed here."""

import copy
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
    Fetched,
    Freshness,
    HerdDecision,
    Overlay,
    ScreenDecision,
    answer_digits,
    decide_herd,
    decide_screen,
    rows_answer,
)
from herdr_pwa.engine import BlockedPrompt, MenuShape, Snapshot, rank

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures"
LEDGER_PATH = FIXTURES / "ledger.json"
# Pinned so the golden file can only change by a reviewed edit to this line. Verdicts never
# change; input hashes are re-pinned when a capture is scrubbed or replaced (see meta.rehashed).
LEDGER_SHA256 = "fcb402504bd6f25f913ca50f5ec5d42c7ac0d9d14702dad1873dd731efb09b16"
FOREIGN_PREFIX = "foreign/"
# collie's claude screens, vendored under tests/fixtures/foreign with collie's MIT notice.
VENDORED_PREFIX = "foreign/collie/claude--"

# Screens whose footer names a key that opens an editor in the pane: still read, never offered.
HANDED_OFF = (
    "foreign/collie/claude--plan-approval--feedback-focused.txt",
    "foreign/collie/claude--plan-approval--feedback-typed.txt",
    "foreign/collie/claude--plan-approval--feedback-wrapped.txt",
    "foreign/collie/claude--plan-approval--three-row-focused.txt",
    "foreign/collie/claude--plan-approval--three-row-typed-focused.txt",
    "foreign/collie/claude--plan-approval--three-row.txt",
    "foreign/collie/claude--plan-approval.txt",
    "foreign/collie/claude--select-preview-note-input.txt",
    "tests/fixtures/capture-blocked-plan-approval/detection.txt",
    "tests/fixtures/capture-blocked-plan-approval/recent-unwrapped.txt",
    "tests/fixtures/capture-blocked-plan-approval/recent.txt",
    "tests/fixtures/capture-blocked-plan-approval/visible.txt",
)

# A checklist with the cursor parked on its unnumbered Next row, and the same screen with it on a row.
PARKED: dict[str, str] = {
    "foreign/collie/claude--wizard-multiselect-pointer-next.txt": (
        "foreign/collie/claude--wizard-multiselect-checked.txt"
    ),
}

# claude questions drawn with a preview box beside their rows, and what each now reads. No
# row earns a digit, which there moves the cursor and answers nothing, so every key stays.
_ROWS_READ: dict[str, JsonValue] = {
    "corroborated": True,
    "drew_rows": True,
    "asked": True,
    "is_choice": True,
}
_ONE_QUESTION: dict[str, dict[str, JsonValue]] = {
    "verdict": {"shape": "single-choice", "options": 3},
    "render": {**_ROWS_READ, "numbers": [1, 2, 3]},
}
_WIZARD_STEP: dict[str, dict[str, JsonValue]] = {
    "verdict": {"shape": "single-choice", "options": 2, "steps": [0, 2]},
    "render": {**_ROWS_READ, "numbers": [1, 2]},
}
PREVIEWED: dict[str, dict[str, dict[str, JsonValue]]] = {
    "foreign/collie/claude--select-preview.txt": _ONE_QUESTION,
    "foreign/collie/claude--select-preview-note-attached.txt": _ONE_QUESTION,
    "foreign/collie/claude--wizard-preview-q1.txt": _WIZARD_STEP,
    "foreign/collie/claude--wizard-preview-note-attached.txt": _WIZARD_STEP,
    "foreign/collie/claude--wizard-preview-wrapped-label.txt": _WIZARD_STEP,
}

# Every deliberate divergence from the ledger, by entry id, with the reason it is better.
DIVERGENCES: dict[str, str] = {
    **dict.fromkeys(
        HANDED_OFF,
        "claude's footer offers ctrl+g to edit the plan or note in $EDITOR, and the ledger "
        "counts it as a hint, so it became a button: one tap put Vim in the pane, which no key the phone sends can "
        "work or quit. The hint is still read, so the screen still asks, but its key is no "
        "longer a hint or a control. test_a_hand_off_key_is_all_that_leaves_the_ledger holds "
        "the rest of each entry.",
    ),
    **dict.fromkeys(
        PARKED,
        "The ledger reads this checklist as unknown with no rows because the cursor sat on "
        "its unnumbered Next row, which broke the run; with the cursor on row 1 the same "
        "screen read as a six-row checklist. A cursor move is not a new question, and the "
        "flip refused the next tap as a changed screen. "
        "test_a_parked_cursor_reads_as_the_cursor_on_a_row holds the rest of each entry.",
    ),
    **dict.fromkeys(
        PREVIEWED,
        "claude draws an option's preview as a box beside the rows, and the ledger reads the "
        "box's sides as text after the last row, so no run was read and the question offered "
        "only its printed keys. The rows are now read with the box cut away, still with no "
        "digit, and on a wizard step the strip above them says the question is one step of two. "
        "test_a_preview_beside_the_rows_is_all_that_leaves_the_ledger holds the rest.",
    ),
    **dict.fromkeys(
        (
            "snapshot.example",
            "snapshot.example+blocked",
            "snapshot.example+blocked+read",
            "snapshot.example+blocked+unread",
        ),
        "The feed's age is now worded by the one span formatter the sheet and doctor use, "
        "which counts whole units: 59.5s reads '59s ago' where the old feed rounded it up to "
        "'60s ago', a minute it had not yet reached. "
        "test_herd_matches_the_ledger_but_for_its_age_wording holds the rest of each entry.",
    ),
}

# The divergence above, label by label: what the ledger said for an age, and what the app says.
REWORDED: dict[str, str] = {"60s ago": "59s ago"}

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


def _decoded(capture_id: str) -> str:
    entry = _obj(SCREENS[capture_id])
    found = _obj(entry["input"])
    path = _input_path(capture_id)
    text = _plain_text(path) if found["decode"] == "plain_text" else path.read_text("utf-8")
    # Only a byte-identical input makes a differing verdict a decision rather than a fixture.
    assert hashlib.sha256(text.encode("utf-8")).hexdigest() == found["sha256"]
    return text


def _array(items: Iterable[JsonValue]) -> list[JsonValue]:
    return list(items)


def _rows_toggle(prompt: BlockedPrompt) -> bool:
    # The old app's read, kept here because only its pinned verdict still uses it.
    return bool(answer_digits(prompt)) and prompt.shape is MenuShape.MULTI_SELECT


def _screen_json(decision: ScreenDecision, prompt: BlockedPrompt) -> dict[str, JsonValue]:
    """The ledger's row: the decision, and the per-screen reads the ledger also records."""
    steps = decision.steps
    return {
        "verdict": {
            "shape": str(decision.shape),
            "options": len(decision.options),
            "digits": _array(answer_digits(prompt)),
            "rows_answer": rows_answer(prompt),
            "hints": [h.key for h in decision.hints],
            "ticked": _array(decision.ticked),
            "checkable": sum(1 for o in decision.options if o.checked is not None),
            "steps": None if steps is None else [steps.answered, steps.total],
        },
        "render": {
            "answer_hints": [h.key for h in decision.answer_hints],
            "rows_toggle": _rows_toggle(prompt),
            "corroborated": decision.corroborated,
            "drew_rows": decision.drew_rows,
            "asked": decision.asked,
            "is_choice": decision.is_choice,
            "numbers": [o.number for o in decision.options],
        },
    }


def _freshness_json(fresh: Freshness, error: str | None) -> dict[str, JsonValue]:
    """The ledger's row: the freshness, and the three flags the ledger records beside it."""
    return {
        "age": fresh.age,
        "age_label": fresh.age_label,
        "condition": str(fresh.condition),
        "stale": fresh.stale,
        "disconnected": fresh.age is not None and fresh.age > DISCONNECTED_AFTER,
        "degraded": error is not None,
        "failure": error or "",
    }


def _row_json(
    row: AgentRow, prompt_id: JsonValue, prompt: BlockedPrompt | None
) -> dict[str, JsonValue]:
    found: dict[str, JsonValue] = {
        "target": row.target,
        "status": str(row.status),
        "effective_status": str(row.effective_status),
        "feed_key": _array(row.feed_key),
        "prompt": prompt_id,
    }
    if row.answer is not None:
        assert prompt is not None
        found["answer_digits"] = _array(answer_digits(prompt))
        found["rows_answer"] = rows_answer(prompt)
        found["answer_hints"] = [h.key for h in row.answer.answer_hints]
    return found


def _herd_json(
    herd: HerdDecision,
    prompt_ids: dict[str, JsonValue],
    prompts: dict[str, BlockedPrompt],
    freshness: list[JsonValue],
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
        "agents": {
            row.session: _row_json(row, prompt_ids.get(row.session), prompts.get(row.session))
            for row in herd.rows
        },
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


def _prompts(snapshot: Snapshot, prompt_ids: dict[str, JsonValue]) -> dict[str, BlockedPrompt]:
    return {
        agent.session: BlockedPrompt.parse(
            _decoded(_str(prompt_ids[agent.session])),
            target=agent.target,
            session=agent.session,
        )
        for agent in snapshot.agents
        if agent.session in prompt_ids
    }


def test_the_golden_file_is_the_one_frozen() -> None:
    assert hashlib.sha256(LEDGER_PATH.read_bytes()).hexdigest() == LEDGER_SHA256


def _input_path(capture_id: str) -> Path:
    return FIXTURES / capture_id if capture_id.startswith(FOREIGN_PREFIX) else REPO / capture_id


def test_every_row_reads_a_file_in_the_repo() -> None:
    # A row whose input is not shipped can never be recomputed, so it is pruned, not kept untested.
    assert [key for key in SCREENS if not _input_path(key).is_file()] == []


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


def test_the_vendored_screens_are_exactly_the_ledger_rows_that_read_them() -> None:
    folder = FIXTURES / VENDORED_PREFIX.rpartition("/")[0]
    shipped = {
        str(path.relative_to(FIXTURES)) for path in folder.iterdir() if path.name != "LICENSE"
    }
    assert shipped == {key for key in SCREENS if key.startswith(VENDORED_PREFIX)}
    # MIT keeps the copyright notice with the copies.
    assert "MIT License" in (folder / "LICENSE").read_text(encoding="utf-8")


@pytest.mark.parametrize("capture_id", _cases(SCREENS))
def test_screen_matches_the_ledger(capture_id: str) -> None:
    prompt = BlockedPrompt.parse(_decoded(capture_id))
    expected = {k: v for k, v in _obj(SCREENS[capture_id]).items() if k != "input"}
    assert _screen_json(decide_screen(prompt), prompt) == expected


@pytest.mark.parametrize("capture_id", HANDED_OFF)
def test_a_hand_off_key_is_all_that_leaves_the_ledger(capture_id: str) -> None:
    prompt = BlockedPrompt.parse(_decoded(capture_id))
    decision = decide_screen(prompt)
    handed = {hint.key for hint in decision.handoffs}
    assert handed
    # A copy: the ledger is shared by every test in the module.
    expected = copy.deepcopy({k: v for k, v in _obj(SCREENS[capture_id]).items() if k != "input"})
    for part, field in (("verdict", "hints"), ("render", "answer_hints")):
        keys = _obj(expected[part])[field]
        assert isinstance(keys, list)
        assert handed <= set(keys)
        _obj(expected[part])[field] = [key for key in keys if key not in handed]
    assert _screen_json(decision, prompt) == expected


@pytest.mark.parametrize(("capture_id", "twin"), PARKED.items())
def test_a_parked_cursor_reads_as_the_cursor_on_a_row(capture_id: str, twin: str) -> None:
    parked = BlockedPrompt.parse(_decoded(capture_id))
    on_a_row = BlockedPrompt.parse(_decoded(twin))
    expected = {k: v for k, v in _obj(SCREENS[twin]).items() if k != "input"}
    assert _screen_json(decide_screen(parked), parked) == expected
    # Identity is what a control carries; equal identity means a tap drawn before the move lands.
    assert parked.identity == on_a_row.identity


@pytest.mark.parametrize("capture_id", PREVIEWED)
def test_a_preview_beside_the_rows_is_all_that_leaves_the_ledger(capture_id: str) -> None:
    prompt = BlockedPrompt.parse(_decoded(capture_id))
    expected = copy.deepcopy({k: v for k, v in _obj(SCREENS[capture_id]).items() if k != "input"})
    assert _obj(expected["verdict"])["options"] == 0
    for part, fields in PREVIEWED[capture_id].items():
        _obj(expected[part]).update(fields)
    assert _screen_json(decide_screen(prompt), prompt) == expected


def _herd_found(herd_id: str) -> tuple[dict[str, JsonValue], dict[str, JsonValue]]:
    entry = _obj(HERDS[herd_id])
    given = _obj(entry["input"])
    snapshot = _snapshot(given)
    prompt_ids = _obj(given["prompts"])
    prompts = _prompts(snapshot, prompt_ids)
    unread = _unread(given["unread"])
    freshness: list[JsonValue] = []
    for case in _objects(entry["freshness"]):
        taken_at, error = case["taken_at"], case["error"]
        assert taken_at is None or isinstance(taken_at, float)
        assert error is None or isinstance(error, str)
        herd = decide_herd(
            snapshot.agents,
            prompts=prompts,
            overlay=Overlay(unread),
            fetched=Fetched(NOW, taken_at, error),
        )
        freshness.append(
            {
                "taken_at": taken_at,
                "age": herd.freshness.age,
                "error": error,
                "result": _freshness_json(herd.freshness, error),
            }
        )
    herd = decide_herd(
        snapshot.agents, prompts=prompts, overlay=Overlay(unread), fetched=Fetched(NOW)
    )
    expected = {k: v for k, v in entry.items() if k != "input"}
    return _herd_json(herd, prompt_ids, prompts, freshness), expected


@pytest.mark.parametrize("herd_id", _cases(HERDS))
def test_herd_matches_the_ledger(herd_id: str) -> None:
    found, expected = _herd_found(herd_id)
    assert found == expected


def _reworded(value: JsonValue) -> JsonValue:
    if isinstance(value, dict):
        return {key: _reworded(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_reworded(item) for item in value]
    return REWORDED.get(value, value) if isinstance(value, str) else value


@pytest.mark.parametrize("herd_id", sorted(set(HERDS) & set(DIVERGENCES)))
def test_herd_matches_the_ledger_but_for_its_age_wording(herd_id: str) -> None:
    found, expected = _herd_found(herd_id)
    assert found != expected
    assert found == _reworded(expected)
