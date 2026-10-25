"""No deprecated datetime.utcnow() and no print() in the collectors / storage / auth modules."""

import ast
import importlib
import pathlib
from datetime import datetime, timedelta, timezone

import pytest

APP = pathlib.Path(__file__).resolve().parents[2] / "app"

FILES = sorted(
    [*(APP / "services" / "collectors").glob("*.py")]
    + [APP / "services" / "influx_client.py", APP / "api" / "auth.py",
       APP / "api" / "status.py", APP / "api" / "timeline.py"]
)

MODULES_WITH_HELPER = [
    "app.services.collectors.hubeau",
    "app.services.collectors.infoclimat",
    "app.services.collectors.meteo_agricole",
    "app.services.collectors.netatmo",
    "app.services.collectors.tide",
    "app.services.collectors.vigicrues",
    "app.services.collectors.weather",
    "app.services.influx_client",
    "app.api.auth",
    "app.api.status",
    "app.api.timeline",
]


def _calls(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            yield node


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_followups_no_utcnow_no_print(path):
    tree = ast.parse(path.read_text())
    offenders = []
    for call in _calls(tree):
        func = call.func
        if isinstance(func, ast.Attribute) and func.attr == "utcnow":
            offenders.append(f"utcnow() line {call.lineno}")
        if isinstance(func, ast.Name) and func.id == "print":
            offenders.append(f"print() line {call.lineno}")
    assert offenders == []


@pytest.mark.parametrize("module", MODULES_WITH_HELPER)
def test_followups_utcnow_helper_is_naive_utc(module):
    now = importlib.import_module(module)._utcnow()
    assert isinstance(now, datetime) and now.tzinfo is None
    assert abs(now - datetime.now(timezone.utc).replace(tzinfo=None)) < timedelta(seconds=5)
