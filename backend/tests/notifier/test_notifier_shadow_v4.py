"""The shadow model 4.0a never triggers a notification (only the stored v3.4 score does)."""

import re
from datetime import timedelta, timezone

import httpx

from app.services import influx_client, notifier, risk_shadow_v4
from app.services.notifier import Notifier, NtfyChannel
from app.services.risk_v4 import V4Features


class _Record:
    def __init__(self, values):
        self.values = values

    def get_time(self):
        return self.values.get("_time")

    def get_value(self):
        return self.values.get("_value")


class _Table:
    def __init__(self, records):
        self.records = [_Record(r) for r in records]


def _pivot_query(points):
    """Fake _run_query: pivoted rows of the measurements named in the query (exact match)."""
    async def run(query):
        names = set(re.findall(r'r\._measurement == "([A-Za-z_]+)"', query))
        return [_Table([
            {"_time": p["timestamp"].replace(tzinfo=timezone.utc), **p["tags"], **p["fields"]}
            for p in points if p["measurement"] in names
        ])]
    return run


def _v34(ts, score):
    return influx_client.point_as_dict(influx_client.make_risk_point(
        score, "red" if score >= 55 else "green", ts, None, "3.4", "realtime",
    ))


def _v4_red(ts, measurement):
    p = risk_shadow_v4.shadow_v4_point(ts, V4Features(backwater_level_m=7.0, runoff_q_m3s=45.0), "realtime")
    d = influx_client.point_as_dict(p)
    assert d["fields"]["level"] == "red"
    d["measurement"] = measurement
    return d


async def _run(h, monkeypatch, points):
    h.clock.advance(1.0)
    monkeypatch.setattr(influx_client, "_run_query", _pivot_query(points))
    sources = h.world.sources()
    sources.latest_score = notifier._default_latest_score  # the production reader
    channel = NtfyChannel(h.settings.ntfy_url, h.settings.ntfy_topic, h.settings.ntfy_token,
                          transport=httpx.MockTransport(h.server))
    await Notifier(channel, h.store, h.settings, clock=h.clock, sources=sources).run_once()
    return h.server.take()


async def test_notifier_ignores_red_4_0a(harness, monkeypatch):
    h = harness()
    now = h.clock.now + timedelta(hours=1)
    points = [
        _v34(now - timedelta(minutes=10), 5.0),
        _v4_red(now - timedelta(minutes=5), influx_client.RISK_SHADOW_MEASUREMENT),
        # defense in depth: even a 4.0a point stored in risk_score is ignored
        _v4_red(now - timedelta(minutes=2), influx_client.RISK_MEASUREMENT),
    ]
    assert await _run(h, monkeypatch, points) == []
    assert h.state.get("level") in (None, "green")

    # control: a red v3.4 score is notified through the same reader
    later = h.clock.now + timedelta(hours=1)
    points.append(_v34(later - timedelta(minutes=10), 70.0))
    assert await _run(h, monkeypatch, points) == ["Lidoire : risque ROUGE (70/100)"]
