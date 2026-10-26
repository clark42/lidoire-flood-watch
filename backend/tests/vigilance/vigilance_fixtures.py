"""Recorded vigilance payloads (no network in tests)."""

import json
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str):
    path = FIXTURES / name
    if path.suffix in (".json", ".geojson"):
        return json.loads(path.read_text(encoding="utf-8"))
    return path.read_text(encoding="utf-8")
