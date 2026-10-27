"""A question an agent's own record poses, and what its screen confirmed of it."""

from __future__ import annotations

from typing import NamedTuple

from pydantic import BaseModel, ConfigDict

from herdr_pwa.validation import Text


class QuestionOption(BaseModel):
    """One answer the record offered: its label, and the text drawn under it."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    label: str
    description: Text = None
    preview: Text = None


class RecordedQuestion(BaseModel):
    """The one single-choice question a record still waits on, under the call that posed it."""

    model_config = ConfigDict(frozen=True)

    id: str
    question: str
    options: tuple[QuestionOption, ...] = ()


class QuestionMatch(NamedTuple):
    """The question the screen confirms, the cursor's row, its footer, and the keys it may offer."""

    question: RecordedQuestion
    cursor: int
    footer: tuple[str, ...]
    keys: frozenset[str]
    # Whether a digit only moves the cursor, so the phone offers no digit as an answer.
    digits_move: bool
