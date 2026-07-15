"""Browser-suite options shared by visual regression tests."""

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--update-snapshots",
        action="store_true",
        default=False,
        help="replace browser visual-regression baseline PNGs",
    )


@pytest.fixture
def update_snapshots(pytestconfig: pytest.Config) -> bool:
    return bool(pytestconfig.getoption("--update-snapshots"))
