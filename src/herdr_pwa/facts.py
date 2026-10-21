"""Facts measured outside this code, kept as data: models' context windows, harnesses' words."""

from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal


@dataclass(frozen=True, slots=True)
class ContextWindow:
    """A model's published context window: its id, whether a release date follows it, its size."""

    model: str
    tokens: int
    dated: Literal["optional", "required"] = "optional"


# Source: Anthropic's models overview. Left out: models that may run 1M, which no log records.
CONTEXT_WINDOWS: Final = (
    ContextWindow("claude-haiku-4-5", 200_000),
    ContextWindow("claude-opus-4-5", 200_000),
    ContextWindow("claude-opus-4-1", 200_000),
    ContextWindow("claude-opus-4", 200_000, dated="required"),
    ContextWindow("claude-3-7-sonnet", 200_000),
    ContextWindow("claude-3-5-sonnet", 200_000),
    ContextWindow("claude-3-5-haiku", 200_000),
)

# A footer line names a key (whole words, or a glyph or "arrow" anywhere) and then a verb.
FOOTER_KEY_WORDS: Final = ("enter", "esc", "tab", "ctrl")
FOOTER_KEY_MARKS: Final = ("↑", "↓", "←", "→", "arrow")
FOOTER_VERBS: Final = (
    *("select", "cancel", "navigate", "confirm", "choose"),
    *("toggle", "continue", "submit", "expand", "manage"),
)

# The verbs that say Enter takes the row in play.
SELECT_VERBS: Final = ("select", "choose", "pick", "confirm", "proceed", "approve", "lock")
# The verbs that say a key flips a row's state instead of answering.
TOGGLE_VERBS: Final = ("toggle", "check", "uncheck", "deselect", "select all")
# opencode prints these verb-first, ``enter submit``: its TUI source and the live captures agree.
VERB_FIRST_VERBS: Final = frozenset({"select", "submit", "confirm", "toggle", "dismiss", "tab"})
# A pane-local fullscreen toggle redraws the whole screen from the phone for nothing.
OFFER_REFUSED: Final = frozenset({"fullscreen"})
# The tab opencode ends a question strip with, which is an affordance and not a step.
CONFIRM_TABS: Final = frozenset({"confirm", "submit", "done"})

# Whole in a hint's verb, these hand the pane to a program no key from here can work or quit.
HANDOFF_VERBS: Final = ("edit in", "editor", "shell", "suspend")

# Modifiers a footer joins to a key with ``+`` or ``-``.
KEY_MODIFIERS: Final = ("ctrl", "shift", "alt", "opt", "option", "cmd", "meta", "super")

# The key names this app sends, spelled the way herdr's ``send_keys`` spells them.
KEY_ALIASES: Final[MappingProxyType[str, tuple[str, ...]]] = MappingProxyType(
    {
        "enter": ("enter",),
        "return": ("enter",),
        "↵": ("enter",),
        "⏎": ("enter",),
        "esc": ("esc",),
        "escape": ("esc",),
        "tab": ("tab",),
        "space": ("space",),
        "spacebar": ("space",),
        "up": ("up",),
        "down": ("down",),
        "left": ("left",),
        "right": ("right",),
        "↑": ("up",),
        "↓": ("down",),
        "←": ("left",),
        "→": ("right",),
        "↑↓": ("up", "down"),
        "↓↑": ("up", "down"),
        # "Arrow keys" means the vertical pair; left/right only count where the screen draws them.
        "arrow": ("up", "down"),
        "arrows": ("up", "down"),
        "arrow keys": ("up", "down"),
        "arrow key": ("up", "down"),
    }
)
# Names a footer gives a key that this app never sends; ``⇆`` depends on the verb beside it.
UNSENT_KEYS: Final = ("backspace", "del", "delete", "pgup", "pgdn", "home", "end", "⇆")

# How a terminal prompt opens when it asks without a question mark.
PROMPT_OPENERS: Final = (
    *("do you", "would you", "should i", "shall i", "allow", "approve", "proceed"),
    *("continue", "confirm", "are you sure", "press", "enter", "type", "select", "choose"),
)
