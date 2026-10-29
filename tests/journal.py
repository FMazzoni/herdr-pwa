"""Log text as the service's journal gets it, for tests whose caplog holds the raw arguments."""

import logging

import pytest

from herdr_pwa import logs


def journal_text(caplog: pytest.LogCaptureFixture) -> str:
    """Every captured record, scrubbed and formatted as the installed handler writes it."""
    scrub = logs.ScrubFilter()
    formatter = logs.ScrubFormatter("%(levelname)s %(name)s %(message)s", use_colors=False)
    return "\n".join(_line(record, scrub, formatter) for record in caplog.records)


def _line(record: logging.LogRecord, scrub: logs.ScrubFilter, formatter: logging.Formatter) -> str:
    _ = scrub.filter(record)
    return formatter.format(record)
