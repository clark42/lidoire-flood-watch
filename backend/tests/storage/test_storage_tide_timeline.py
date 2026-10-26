"""Tide timeline reads: one point per timestamp even with legacy duplicates."""

from datetime import datetime, timedelta, timezone

from app.services import influx_client

UTC = timezone.utc


async def test_tide_timeline_read_is_idempotent(capture_queries, fake_tables):
    queries, respond = capture_queries
    t = datetime(2026, 10, 24, 21, tzinfo=UTC)
    # Legacy data: the PM point and the hourly height are two tables
    respond(fake_tables([
        [{"_time": t, "_value": 1.55}, {"_time": t + timedelta(hours=1), "_value": 1.18}],
        [{"_time": t, "_value": 1.55}],
    ]))
    rows = await influx_client.query_tides_timeline(t - timedelta(hours=1), t + timedelta(hours=2))
    assert rows == [{"timestamp": t, "value": 1.55}, {"timestamp": t + timedelta(hours=1), "value": 1.18}]
    assert 'group(columns: ["_measurement", "_field", "port"])' in queries[0]
    assert queries[0].index("group(") < queries[0].index("aggregateWindow(")
