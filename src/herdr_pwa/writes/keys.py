"""Which keys a screen offers, computed from the read made inside the request."""

from typing import Final

from herdr_pwa.engine.client import INTERRUPT_KEY
from herdr_pwa.engine.dialog import BlockedPrompt, MenuShape
from herdr_pwa.panetext import printed_whole

# The generic pad, with no digits: digits are earned per screen.
PAD_KEYS: Final = ("esc", "up", "down", "enter")

# A synthesised digit past 9 is two keystrokes, and the second one is a guess.
MAX_TAPPABLE_OPTION: Final = 9


def answer_digits(prompt: BlockedPrompt | None) -> tuple[str, ...]:
    """The digit each option row sends, or ``()`` when no row has earned one."""
    # Single-choice only: a checklist digit toggles, and a picker digit once changed a global default.
    if prompt is None or not prompt.options or prompt.shape is not MenuShape.SINGLE_CHOICE:
        return ()
    if not prompt.corroborated:
        return ()
    # A wrapped label was not printed whole, so the row it names is not certain.
    if not all(printed_whole(prompt.raw, option.label) for option in prompt.options):
        return ()
    numbers = [option.number for option in prompt.options]
    if not all(1 <= number <= MAX_TAPPABLE_OPTION for number in numbers):
        return ()
    return tuple(str(number) for number in numbers)


def allowed_keys(prompt: BlockedPrompt | None) -> frozenset[str]:
    """Every key this app will send at the screen just read, and no others."""
    hinted = {hint.key for hint in prompt.hints} if prompt is not None else set[str]()
    return frozenset({*PAD_KEYS, INTERRUPT_KEY, *hinted, *answer_digits(prompt)}) - {""}


def key_ends_question(prompt: BlockedPrompt | None, key: str) -> bool:
    """Does this keystroke end the question, or only move around inside it?"""
    if key == INTERRUPT_KEY:
        return True
    if prompt is None or not key:
        return False
    # A stepper keeps the dialog up for its next step, so a row does not end it.
    if prompt.stepper is not None or prompt.shape is not MenuShape.SINGLE_CHOICE:
        return False
    return key == "enter" or key in answer_digits(prompt)
