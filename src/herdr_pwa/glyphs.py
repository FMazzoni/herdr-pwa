"""The glyphs terminals draw with, each set spelled once for every pattern that composes it."""

import re
from typing import Final

# Unicode's Box Drawing block, U+2500-257F.
BOX_DRAWING: Final = "".join(chr(c) for c in range(0x2500, 0x2580))
# Box drawing plus Block Elements, U+2580-259F: everything a TUI frames a screen with.
BOX_AND_BLOCK: Final = "".join(chr(c) for c in range(0x2500, 0x25A0))

# Horizontal rules: light and heavy, then double.
LINE_RULE: Final = "─━"
RULE: Final = LINE_RULE + "═"
DASHED_RULE: Final = "╌╍┄┅┈┉"
# Vertical sides: light, heavy and double, and the left eighth-blocks some TUIs draw a bar with.
SIDE: Final = "│┃║"
LEFT_BLOCKS: Final = "▏▍▎▊"

# A drawn box's corners and the tees an inner rule meets its sides with.
TOP_LEFT: Final = "╭┌╔"
BOTTOM_LEFT: Final = "╰└╚"
BOTTOM_RIGHT: Final = "╯┘╝"
TEE_LEFT: Final = "├┝┠╞╟"
TEE_RIGHT: Final = "┤┥┨╡╢"

# The preview box claude draws beside a question's rows: light or heavy lines, never double.
PREVIEW_TOP: Final = "┌╭"
PREVIEW_SIDE: Final = "│┃├┝"
PREVIEW_BOTTOM: Final = "└╰"

# The glyphs a TUI parks its cursor on a row with; ``>`` is drawn as one too, but is also a quote.
CURSOR: Final = "\u276f▸▶‣"
QUOTE_CURSOR: Final = ">"
# What opens the agent's own input line.
PROMPT: Final = "\u276f>"

ARROW_LEFT: Final = "←"
ARROW_RIGHT: Final = "→"

# Checkbox glyphs, ticked and blank, as a checklist row draws them.
TICKED_BOX: Final = "☑☒◼"
BLANK_BOX: Final = "☐◻"
# A ticked mark inside brackets, ``[✔]``.
CHECK: Final = "✔✓"
# A question stepper's step boxes: blank, answered, and the strip's own submit tab.
STEP_BLANK: Final = "☐"
STEP_ANSWERED: Final = "☒☑"
STEP_SUBMIT: Final = CHECK
STEP_BOXES: Final = STEP_BLANK + STEP_ANSWERED + STEP_SUBMIT


def one_of(*glyphs: str) -> str:
    """A character class matching any of *glyphs*, each escaped."""
    return f"[{re.escape(''.join(glyphs))}]"
