import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--foreign-root",
        default=None,
        help="directory holding the pinned foreign capture clones (else $HP_FOREIGN_CAPTURES)",
    )
