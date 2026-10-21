"""Which keys a screen offers, computed from the read made inside the request."""

from typing import Final

from herdr_pwa.engine import INTERRUPT_KEY, BlockedPrompt, MenuShape, digits_masked, screen_digest
from herdr_pwa.panetext import printed_whole

# The generic pad, with no digits: digits are earned per screen.
PAD_KEYS: Final = ("esc", "up", "down", "enter")

# The pad of a screen answered sideways: up and down do nothing there.
BUTTON_PAD_KEYS: Final = ("esc", "left", "right", "enter")

# A synthesised digit past 9 is two keystrokes, and the second one is a guess.
MAX_TAPPABLE_OPTION: Final = 9


def normalise_key(key: str) -> str:
    """A posted key as the allowed sets spell it; every check and echo goes through this."""
    return key.strip().lower()


def pad_keys(prompt: BlockedPrompt | None) -> tuple[str, ...]:
    """The generic pad this screen earns: the arrows only where the screen uses them."""
    if prompt is not None and prompt.shape is MenuShape.BUTTONS:
        # opencode's permission dialog moves its focus with left/right only
        # (its TUI binds no up/down there); offering them would send dead keys.
        return BUTTON_PAD_KEYS
    return PAD_KEYS


def answer_digits(prompt: BlockedPrompt | None) -> tuple[str, ...]:
    """The digit each option row sends, or ``()`` when no row has earned one."""
    # Single-choice only: a checklist digit toggles, and a picker digit once changed a global default.
    if prompt is None or not prompt.options or prompt.shape is not MenuShape.SINGLE_CHOICE:
        return ()
    # Measured live: beside a preview a digit only moves the cursor, so it is no answer.
    if not prompt.corroborated or prompt.digits_move:
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
    # A key the screen says gives the pane to another program is refused, a pad key included.
    handed = {hint.key for hint in prompt.handoffs} if prompt is not None else set[str]()
    offered = {*pad_keys(prompt), *hinted, *answer_digits(prompt)} - handed - {""}
    return frozenset({*offered, INTERRUPT_KEY})


def key_ends_question(prompt: BlockedPrompt | None, key: str) -> bool:
    """Does this keystroke end the question, or only move around inside it?"""
    if key == INTERRUPT_KEY:
        return True
    if prompt is None or not key:
        return False
    if prompt.shape is MenuShape.BUTTONS:
        # Enter confirms the focused choice; Esc is the reject the screen binds.
        return key in ("enter", "esc")
    # A stepper keeps the dialog up for its next step, so a row does not end it.
    if prompt.stepper is not None or prompt.shape is not MenuShape.SINGLE_CHOICE:
        return False
    return key == "enter" or key in answer_digits(prompt)


def control_token(prompt: BlockedPrompt) -> str:
    """What a control carries to prove which question it was drawn for."""
    # The question as posed, not the raw screen: a status line's clock flipped the raw digest
    # between drawing a control and tapping it, refusing taps on a question that never moved.
    # Unparsed, the screen stands in, its numbers masked: hermes ticks a countdown every second.
    return prompt.identity or screen_digest(digits_masked(prompt.raw))
