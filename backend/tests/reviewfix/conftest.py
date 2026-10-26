"""Fixtures for the wave-2 review fixes (no network, no database).

Reuses the frozen risk scenario of tests/risk (risk_scenario.py).
"""

import pathlib
import sys

import pytest

_RISK_TESTS = pathlib.Path(__file__).resolve().parents[1] / "risk"
if str(_RISK_TESTS) not in sys.path:
    sys.path.insert(0, str(_RISK_TESTS))


@pytest.fixture(autouse=True)
def _no_real_influx(monkeypatch):
    """Fail loudly if a test reaches the real InfluxDB client."""
    from app.services import influx_client

    def _boom():
        raise AssertionError("real InfluxDB API used in a test")

    monkeypatch.setattr(influx_client, "get_write_api", _boom)
    monkeypatch.setattr(influx_client, "get_query_api", _boom)


@pytest.fixture
def tide_rows_from_events():
    """query_tides()-shaped rows (one row per field) from (ts, type, height, coef) events."""

    def _rows(events):
        rows = []
        for ts, tide_type, height, coef in events:
            rows.append({"timestamp": ts, "height_m": height, "coefficient": None,
                         "tide_type": tide_type, "port": "LIDOIRE"})
            if coef is not None:
                rows.append({"timestamp": ts, "height_m": None, "coefficient": coef,
                             "tide_type": tide_type, "port": "LIDOIRE"})
        return rows

    return _rows
