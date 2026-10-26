"""Suite-wide fixtures."""

import pytest


@pytest.fixture(autouse=True)
def _close_circuit_breakers():
    """Collector breakers are module singletons: a failure simulated by one
    test must not skip the upstream in the next one."""
    from app.services.cache import reset_breakers

    reset_breakers()
    yield
    reset_breakers()
