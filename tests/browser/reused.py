"""A browser test green for this tree is reported as reused: neither run, nor passed, nor skipped.

Kept apart from the receipts so the controller of a parallel run, which tallies every report,
counts reuse without importing the browser rig.
"""

import pytest

# The user property a reused test's report carries, holding the tree it was green for.
REUSED = "reused"


def pytest_report_teststatus(report: pytest.TestReport) -> tuple[str, str, str] | None:
    """Tally a reused browser test under its own word in the summary."""
    if any(name == REUSED for name, _ in report.user_properties):
        return REUSED, "r", "REUSED"
    return None
