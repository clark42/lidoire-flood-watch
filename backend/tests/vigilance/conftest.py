"""Fixtures for the vigilance tests (no network)."""

import pytest


@pytest.fixture(autouse=True)
def _fresh_vigilance_state():
    from app.services.collectors import vigilance

    vigilance.reset_state()
    yield
    vigilance.reset_state()
