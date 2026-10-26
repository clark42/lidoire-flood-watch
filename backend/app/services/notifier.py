"""Notifications: risk level transitions, forecast heads-ups, stale data.

The ``notifier`` scheduler job runs every hour, a few minutes after
``risk_scorer``, and evaluates three triggers:

- observed level: the last stored realtime risk score (the one written by
  ``risk_scorer``). Rising to orange or red is sent at once; a lower level is
  sent only after it has held ``notify_downgrade_hold_hours`` (the score
  oscillates around the thresholds with the tide). Green <-> yellow is silent
  unless ``notify_yellow``.
- forecast: the max score over the next ``notify_forecast_hours`` reaches
  orange/red -> one heads-up per event (again on escalation to red), cleared
  once the forecast has stayed below orange for the same hold time.
- staleness: primary station (Pessac) last point older than
  ``notify_station_stale_hours``, or no realtime score stored for
  ``notify_score_stale_hours`` -> one alert, then one recovery message.

State (:class:`NotifierState`) is a JSON document in PostgreSQL (table
``notifier_state``): Redis has no persistence in production, and Postgres is
already a hard dependency of the backend, is created by ``init_db`` and is
covered by the backup CronJob. A trigger's state change is applied only once
its message was delivered, so a failed POST is retried on the next run
instead of being lost, and a restart never resends.

Channels implement :class:`Channel` (``name`` + ``async send() -> bool``);
ntfy is the only one today. Nothing runs when ``NTFY_URL`` / ``NTFY_TOPIC``
are empty (one info log at startup).
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional, Protocol
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from app.config import Settings, get_settings
from app.models.schemas import RiskLevel
from app.services.risk_core import score_to_level, to_utc_naive

logger = logging.getLogger(__name__)

JOB_ID = "notifier"
# Run after the hourly risk_scorer job, so its fresh score is the one read
JOB_OFFSET = timedelta(minutes=5)
STATE_KEY = "default"
SEND_TIMEOUT_S = 10.0
# Window searched for the last stored realtime score
SCORE_LOOKBACK = timedelta(hours=24)

_RANK = {RiskLevel.GREEN.value: 0, RiskLevel.YELLOW.value: 1, RiskLevel.ORANGE.value: 2, RiskLevel.RED.value: 3}
_ORANGE = _RANK[RiskLevel.ORANGE.value]
LEVEL_FR = {"green": "VERT", "yellow": "JAUNE", "orange": "ORANGE", "red": "ROUGE"}
TENDENCY_FR = {"hausse": "en hausse", "stable": "stable", "baisse": "en baisse"}
# ntfy priority (1-5) and tags (rendered as emojis by the ntfy apps)
_RISE_STYLE = {"yellow": (3, ("yellow_circle",)), "orange": (4, ("warning",)), "red": (5, ("rotating_light",))}

try:
    _LOCAL_TZ: Optional[ZoneInfo] = ZoneInfo("Europe/Paris")
except ZoneInfoNotFoundError:  # no tzdata: fall back to UTC labels
    _LOCAL_TZ = None


def _utcnow() -> datetime:
    """Naive UTC now (module-level so tests can freeze time)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _rank(level: Optional[str]) -> int:
    return _RANK.get(level or RiskLevel.GREEN.value, 0)


def _notifiable(level: str, settings: Settings) -> bool:
    return _rank(level) >= _ORANGE or (level == RiskLevel.YELLOW.value and settings.notify_yellow)


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Notification:
    title: str
    message: str
    priority: int = 3  # ntfy scale: 1 (min) .. 5 (max)
    tags: tuple[str, ...] = ()
    click: Optional[str] = None


class Channel(Protocol):
    name: str

    async def send(self, notification: Notification) -> bool:
        """Deliver the notification; False on failure (never raises)."""
        ...


def _header_value(text: str) -> str:
    """ASCII header value; non-ASCII text as an RFC 2047 word (decoded by ntfy)."""
    try:
        text.encode("ascii")
        return text
    except UnicodeEncodeError:
        return "=?UTF-8?B?" + base64.b64encode(text.encode("utf-8")).decode("ascii") + "?="


class NtfyChannel:
    """ntfy: ``POST <url>/<topic>``, message as body, metadata as headers."""

    name = "ntfy"

    def __init__(
        self,
        url: str,
        topic: str,
        token: str = "",
        *,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        timeout: float = SEND_TIMEOUT_S,
    ):
        self._endpoint = f"{url.strip().rstrip('/')}/{quote(topic.strip(), safe='')}"
        # Logged instead of the endpoint: on a public server the topic is a secret
        self.host = urlsplit(url.strip()).netloc or "ntfy"
        self._token = token.strip()
        self._transport = transport
        self._timeout = timeout

    async def send(self, notification: Notification) -> bool:
        headers = {
            "Title": _header_value(notification.title),
            "Priority": str(notification.priority),
            "Content-Type": "text/plain; charset=utf-8",
        }
        if notification.tags:
            headers["Tags"] = ",".join(notification.tags)
        if notification.click:
            headers["Click"] = notification.click
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        client_kwargs: dict[str, Any] = {"timeout": self._timeout}
        if self._transport is not None:
            client_kwargs["transport"] = self._transport
        try:
            async with httpx.AsyncClient(**client_kwargs) as client:
                response = await client.post(
                    self._endpoint, content=notification.message.encode("utf-8"), headers=headers,
                )
        except httpx.HTTPError as e:
            logger.error("ntfy (%s): send failed (%s)", self.host, type(e).__name__)
            return False
        if response.status_code >= 400:
            logger.error("ntfy (%s): HTTP %s", self.host, response.status_code)
            return False
        return True


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

_DATETIME_FIELDS = ("last_score_at", "pending_since", "forecast_clear_since")


@dataclass
class NotifierState:
    level: str = RiskLevel.GREEN.value     # observed level, after hysteresis
    last_score_at: Optional[datetime] = None  # last stored score processed
    pending_level: Optional[str] = None    # lower level waiting to hold
    pending_since: Optional[datetime] = None
    forecast_level: Optional[str] = None   # active forecast heads-up (None = none)
    forecast_clear_since: Optional[datetime] = None
    station_stale: bool = False
    score_stale: bool = False

    def to_json(self) -> str:
        data = asdict(self)
        for key in _DATETIME_FIELDS:
            if data[key] is not None:
                data[key] = data[key].isoformat()
        return json.dumps(data, sort_keys=True)

    @classmethod
    def from_json(cls, raw: Optional[str]) -> "NotifierState":
        if not raw:
            return cls()
        try:
            data = json.loads(raw)
        except ValueError:
            logger.warning("notifier: invalid stored state, starting from scratch")
            return cls()
        known = {f.name for f in fields(cls)}
        data = {k: v for k, v in data.items() if k in known}
        for key in _DATETIME_FIELDS:
            if data.get(key):
                data[key] = datetime.fromisoformat(data[key])
        return cls(**data)

    def apply(self, updates: dict) -> None:
        for key, value in updates.items():
            setattr(self, key, value)


class StateStore(Protocol):
    async def load(self) -> Optional[str]: ...

    async def save(self, value: str) -> None: ...


class PostgresStateStore:
    """One JSON row of ``notifier_state`` (created by init_db)."""

    def __init__(self, key: str = STATE_KEY):
        self.key = key

    async def load(self) -> Optional[str]:
        from app.db import get_session_factory
        from app.models.orm import NotifierStateRow

        async with get_session_factory()() as session:
            row = await session.get(NotifierStateRow, self.key)
            return row.value if row else None

    async def save(self, value: str) -> None:
        from app.db import get_session_factory
        from app.models.orm import NotifierStateRow

        async with get_session_factory()() as session:
            async with session.begin():
                row = await session.get(NotifierStateRow, self.key)
                if row is None:
                    session.add(NotifierStateRow(key=self.key, value=value))
                else:
                    row.value = value


# ---------------------------------------------------------------------------
# Message context and formatting (French)
# ---------------------------------------------------------------------------

@dataclass
class Context:
    station_name: str = "Pessac"
    station_level_m: Optional[float] = None
    station_at: Optional[datetime] = None
    station_rate_cm_h: Optional[float] = None
    station_tendency: Optional[str] = None
    rain_72h_mm: Optional[float] = None
    next_high_tide: Optional[datetime] = None
    tide_coefficient: Optional[int] = None
    tide_estimated: bool = False
    frontend_url: str = ""


def context_from_inputs(inputs: Any, settings: Settings) -> Context:
    """Message context from realtime inputs (``risk_v2.RealtimeInputs``, or None)."""
    ctx = Context(frontend_url=settings.frontend_url.strip())
    primary = settings.hubeau_primary_station
    for station in settings.hubeau_stations:
        if station["id"] == primary:
            ctx.station_name = station["name"]
    if inputs is None:
        return ctx
    stations = getattr(inputs, "stations", None)
    for s in (stations.stations if stations else []):
        if s.station_id == primary:
            ctx.station_level_m = s.current_level.height_m
            ctx.station_at = to_utc_naive(s.current_level.timestamp)
            ctx.station_rate_cm_h = s.rate_cm_per_hour
            ctx.station_tendency = getattr(s.tendency, "value", s.tendency)
    rainfall = getattr(inputs, "rainfall", None)
    if rainfall is not None:
        ctx.rain_72h_mm = rainfall.last_72h_mm
    tide = getattr(inputs, "tide", None)
    if tide is not None and tide.next_high_tide is not None:
        ctx.next_high_tide = to_utc_naive(tide.next_high_tide.timestamp)
        ctx.tide_coefficient = tide.next_high_tide.coefficient or tide.current_coefficient
        ctx.tide_estimated = not tide.available
    return ctx


def _num(value: float, decimals: int = 1) -> str:
    return f"{value:.{decimals}f}".replace(".", ",")


def _local(dt: datetime) -> str:
    """Naive UTC -> 'dd/mm HH:MM' in French local time (UTC without tzdata)."""
    aware = dt.replace(tzinfo=timezone.utc)
    if _LOCAL_TZ is None:
        return aware.strftime("%d/%m %H:%M UTC")
    return aware.astimezone(_LOCAL_TZ).strftime("%d/%m %H:%M")


def _hours(delta: timedelta) -> str:
    return _num(delta.total_seconds() / 3600, 0)


def _context_lines(ctx: Context) -> list[str]:
    lines = []
    if ctx.station_level_m is not None:
        tendency = TENDENCY_FR.get(ctx.station_tendency or "", ctx.station_tendency or "")
        rate = f" ({ctx.station_rate_cm_h:+.1f} cm/h)".replace(".", ",") if ctx.station_rate_cm_h is not None else ""
        lines.append(f"{ctx.station_name} : {_num(ctx.station_level_m, 2)} m, {tendency}{rate}")
    else:
        lines.append(f"{ctx.station_name} : pas de mesure récente")
    if ctx.rain_72h_mm is not None:
        lines.append(f"Pluie 72 h : {_num(ctx.rain_72h_mm, 0)} mm")
    if ctx.next_high_tide is not None:
        tide = f"Prochaine pleine mer : {_local(ctx.next_high_tide)}"
        if ctx.tide_coefficient:
            tide += f", coef. {ctx.tide_coefficient}"
        if ctx.tide_estimated:
            tide += " (estimée)"
        lines.append(tide)
    if ctx.frontend_url:
        lines.append(f"Tableau de bord : {ctx.frontend_url}")
    return lines


def _notification(title: str, first: str, ctx: Context, priority: int, tags: tuple[str, ...]) -> Notification:
    return Notification(
        title=title,
        message="\n".join([first, *_context_lines(ctx)]),
        priority=priority,
        tags=tags,
        click=ctx.frontend_url or None,
    )


# ---------------------------------------------------------------------------
# Triggers (pure: state + observations -> event)
# ---------------------------------------------------------------------------

@dataclass
class Event:
    """State updates, applied once ``notification`` (if any) is delivered."""

    updates: dict
    notification: Optional[Notification] = None


def level_event(
    state: NotifierState, score_at: datetime, score: float, level: str, ctx: Context, settings: Settings,
) -> Optional[Event]:
    """Observed level transition (with downgrade hysteresis) for a new stored score."""
    if state.last_score_at is not None and score_at <= state.last_score_at:
        return None  # already processed
    current = state.level
    updates: dict = {"last_score_at": score_at}
    cleared = {"pending_level": None, "pending_since": None}

    if _rank(level) > _rank(current):
        updates.update(level=level, **cleared)
        if not _notifiable(level, settings):
            return Event(updates)
        priority, tags = _RISE_STYLE[level]
        return Event(updates, _notification(
            f"Lidoire : risque {LEVEL_FR[level]} ({score:.0f}/100)",
            f"Le risque au moulin passe de {LEVEL_FR[current]} à {LEVEL_FR[level]} (score {score:.0f}/100).",
            ctx, priority, tags,
        ))

    if _rank(level) == _rank(current):
        updates.update(cleared)  # back up before the hold time: no message
        return Event(updates)

    # Lower level: wait until it has held; the target is the highest level
    # seen since the drop (e.g. red -> orange, yellow, orange => orange).
    since = state.pending_since or score_at
    target = level
    if state.pending_since is not None and _rank(state.pending_level) > _rank(level):
        target = state.pending_level
    hold = timedelta(hours=settings.notify_downgrade_hold_hours)
    if score_at - since < hold:
        updates.update(pending_level=target, pending_since=since)
        return Event(updates)

    updates.update(level=target, **cleared)
    if not _notifiable(current, settings):
        return Event(updates)
    tags = ("white_check_mark",) if target == RiskLevel.GREEN.value else ("chart_with_downwards_trend",)
    return Event(updates, _notification(
        f"Lidoire : retour en {LEVEL_FR[target]} ({score:.0f}/100)",
        f"Le risque au moulin redescend de {LEVEL_FR[current]} à {LEVEL_FR[target]} "
        f"depuis {_hours(score_at - since)} h (score {score:.0f}/100).",
        ctx, 3, tags,
    ))


def _entry_time(entry: dict) -> Optional[datetime]:
    try:
        return to_utc_naive(datetime.fromisoformat(str(entry["timestamp"]).replace("Z", "+00:00")))
    except (KeyError, ValueError):
        return None


def forecast_event(
    state: NotifierState, entries: list[dict], now: datetime, ctx: Context, settings: Settings,
) -> Optional[Event]:
    """Heads-up when the forecast max over the next hours reaches orange/red (once per event)."""
    horizon = settings.notify_forecast_hours
    window = [e for e in entries if 1 <= e.get("hours_ahead", 0) <= horizon]
    if not window:
        return None
    peak = max(window, key=lambda e: e["score"])
    level = score_to_level(peak["score"]).value
    active = state.forecast_level

    if _rank(level) >= _ORANGE:
        updates: dict = {"forecast_clear_since": None}
        if active is not None and _rank(level) <= _rank(active):
            return Event(updates) if state.forecast_clear_since is not None else None
        updates["forecast_level"] = level
        if _rank(state.level) >= _rank(level):
            return Event(updates)  # already observed at this level: nothing new
        first = next(
            e for e in sorted(window, key=lambda e: e["hours_ahead"])
            if _rank(score_to_level(e["score"]).value) >= _ORANGE
        )
        peak_at = _entry_time(peak)
        when = f" vers {_local(peak_at)}" if peak_at else ""
        red = level == RiskLevel.RED.value
        return Event(updates, _notification(
            f"Lidoire : risque {LEVEL_FR[level]} prévu",
            f"Prévision : score max {peak['score']:.0f}/100 ({LEVEL_FR[level]}) dans {peak['hours_ahead']} h{when}, "
            f"seuil orange atteint dans {first['hours_ahead']} h (horizon {horizon} h).",
            ctx, 4 if red else 3, ("rotating_light",) if red else ("warning",),
        ))

    if active is None:
        return None
    since = state.forecast_clear_since or now
    if now - since < timedelta(hours=settings.notify_downgrade_hold_hours):
        return Event({"forecast_clear_since": since})
    updates = {"forecast_level": None, "forecast_clear_since": None}
    if _rank(state.level) >= _ORANGE:
        return Event(updates)  # the observed level messages take over
    return Event(updates, _notification(
        "Lidoire : prévision revenue sous l'orange",
        f"Plus de risque orange ou rouge prévu sur {horizon} h (max {peak['score']:.0f}/100).",
        ctx, 2, ("white_check_mark",),
    ))


def staleness_events(
    state: NotifierState, score: Optional[dict], now: datetime, ctx: Context, settings: Settings,
) -> list[Event]:
    """One alert when data goes stale, one recovery message when it is back."""
    events = []
    name = ctx.station_name
    station_age = now - ctx.station_at if ctx.station_at else None
    station_stale = station_age is None or station_age > timedelta(hours=settings.notify_station_stale_hours)
    if station_stale and not state.station_stale:
        detail = (
            f"Dernière mesure de {name} : {_local(ctx.station_at)} (il y a {_hours(station_age)} h)."
            if ctx.station_at else f"Aucune mesure récente de {name} (Hub'Eau)."
        )
        events.append(Event({"station_stale": True}, Notification(
            title=f"Lidoire : données {name} périmées",
            message=f"{detail}\nStation primaire du modèle : au-delà de {_num(settings.notify_station_stale_hours, 0)} h "
                    "elle ne compte plus dans le score.",
            priority=3, tags=("hourglass",), click=ctx.frontend_url or None,
        )))
    elif not station_stale and state.station_stale:
        events.append(Event({"station_stale": False}, Notification(
            title=f"Lidoire : données {name} rétablies",
            message=f"Mesure de {name} à {_local(ctx.station_at)} : {_num(ctx.station_level_m or 0.0, 2)} m.",
            priority=2, tags=("white_check_mark",), click=ctx.frontend_url or None,
        )))

    score_at = to_utc_naive(score["timestamp"]) if score else None
    score_age = now - score_at if score_at else None
    score_stale = score_age is None or score_age > timedelta(hours=settings.notify_score_stale_hours)
    if score_stale and not state.score_stale:
        detail = (
            f"Dernier score enregistré : {_local(score_at)} (il y a {_hours(score_age)} h)."
            if score_at else "Aucun score de risque enregistré sur 24 h."
        )
        events.append(Event({"score_stale": True}, Notification(
            title="Lidoire : score de risque non calculé",
            message=f"{detail}\nVérifier le backend (job risk_scorer, InfluxDB).",
            priority=4, tags=("hourglass",), click=ctx.frontend_url or None,
        )))
    elif not score_stale and state.score_stale:
        level = str(score.get("level") or "")
        events.append(Event({"score_stale": False}, Notification(
            title="Lidoire : score de risque rétabli",
            message=f"Score enregistré à {_local(score_at)} : {float(score['score']):.0f}/100 "
                    f"({LEVEL_FR.get(level, level)}).",
            priority=2, tags=("white_check_mark",), click=ctx.frontend_url or None,
        )))
    return events


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def _default_realtime_inputs() -> Any:
    from app.services.risk_v2 import gather_realtime_inputs

    return await gather_realtime_inputs()


async def _default_latest_score(now: datetime) -> Optional[dict]:
    """Last stored realtime score (backfill points ignored), None if none."""
    from app.services import influx_client

    rows = await influx_client.query_risk_scores(now - SCORE_LOOKBACK, now + timedelta(minutes=1))
    live = [r for r in rows if r.get("source") != influx_client.RISK_SOURCE_BACKFILL]
    return live[-1] if live else None


async def _default_forecast() -> list[dict]:
    from app.services.forecast import get_risk_forecast_v2

    # Default horizon: shares the Redis cache with the dashboard
    return (await get_risk_forecast_v2()).get("forecasts", [])


@dataclass
class Sources:
    realtime_inputs: Callable[[], Awaitable[Any]] = _default_realtime_inputs
    latest_score: Callable[[datetime], Awaitable[Optional[dict]]] = _default_latest_score
    forecast: Callable[[], Awaitable[list[dict]]] = _default_forecast


async def _attempt(awaitable: Awaitable, name: str) -> tuple[bool, Any]:
    try:
        return True, await awaitable
    except Exception as e:  # one source down must not block the others
        logger.warning("notifier: %s unavailable: %s", name, e)
        return False, None


class Notifier:
    def __init__(
        self,
        channel: Channel,
        store: StateStore,
        settings: Optional[Settings] = None,
        *,
        clock: Callable[[], datetime] = _utcnow,
        sources: Optional[Sources] = None,
    ):
        self.channel = channel
        self.store = store
        self.settings = settings or get_settings()
        self.clock = clock
        self.sources = sources or Sources()

    async def _deliver(self, state: NotifierState, event: Optional[Event], sent: list[Notification]) -> None:
        if event is None:
            return
        if event.notification is not None:
            if not await self.channel.send(event.notification):
                return  # state unchanged: retried on the next run
            sent.append(event.notification)
        state.apply(event.updates)

    async def run_once(self) -> list[Notification]:
        """Evaluate every trigger once; returns the notifications delivered."""
        try:
            raw = await self.store.load()
        except Exception as e:
            # Without the state every alert would be resent: skip the run
            logger.error("notifier: state unavailable, run skipped: %s", e)
            return []
        state = NotifierState.from_json(raw)
        now = self.clock()
        sent: list[Notification] = []

        # A failure to read the inputs counts as "no recent Pessac point"
        _, inputs = await _attempt(self.sources.realtime_inputs(), "realtime inputs")
        ctx = context_from_inputs(inputs, self.settings)
        _, score = await _attempt(self.sources.latest_score(now), "latest risk score")

        for event in staleness_events(state, score, now, ctx, self.settings):
            await self._deliver(state, event, sent)

        if score:
            await self._deliver(state, level_event(
                state, to_utc_naive(score["timestamp"]), float(score["score"]),
                str(score.get("level") or score_to_level(float(score["score"])).value), ctx, self.settings,
            ), sent)

        ok, entries = await _attempt(self.sources.forecast(), "forecast")
        if ok and entries:
            await self._deliver(state, forecast_event(state, entries, now, ctx, self.settings), sent)

        new_raw = state.to_json()
        if new_raw != raw:
            try:
                await self.store.save(new_raw)
            except Exception as e:
                logger.error("notifier: state not saved (messages may be resent): %s", e)
        return sent


def build_channel(settings: Optional[Settings] = None) -> Optional[Channel]:
    """Configured channel, None when notifications are disabled."""
    settings = settings or get_settings()
    if not settings.notifications_enabled:
        return None
    return NtfyChannel(settings.ntfy_url, settings.ntfy_topic, settings.ntfy_token)


async def notifier_job() -> None:
    """Scheduler job (registered only when notifications are enabled)."""
    channel = build_channel()
    if channel is None:
        return
    try:
        sent = await Notifier(channel, PostgresStateStore()).run_once()
        if sent:
            logger.info("notifier: %d notification(s) sent via %s", len(sent), channel.name)
    except Exception as e:
        logger.error("notifier: run failed: %s", e)


def schedule_notifier(scheduler, first_run: datetime) -> bool:
    """Register the hourly notifier job (after ``first_run``, the first risk_scorer run)."""
    settings = get_settings()
    channel = build_channel(settings)
    if channel is None:
        logger.info("Notifications disabled (NTFY_URL / NTFY_TOPIC not set)")
        return False
    from apscheduler.triggers.interval import IntervalTrigger

    scheduler.add_job(
        notifier_job,
        trigger=IntervalTrigger(hours=1),
        id=JOB_ID,
        name="Send notifications (level, forecast, stale data)",
        next_run_time=first_run + JOB_OFFSET,
        replace_existing=True,
    )
    logger.info("Notifications enabled: %s on %s", channel.name, getattr(channel, "host", "?"))
    return True


async def send_test_notification(settings: Optional[Settings] = None, channel: Optional[Channel] = None) -> bool:
    """Send a test message (POST /api/notify/test). False if disabled or failed."""
    settings = settings or get_settings()
    channel = channel or build_channel(settings)
    if channel is None:
        return False
    url = settings.frontend_url.strip()
    lines = [
        "Notification de test de Lidoire Flood Watch.",
        "Alertes envoyées sur ce canal : passage en orange/rouge, prévision orange/rouge "
        f"sous {settings.notify_forecast_hours} h, données périmées.",
    ]
    if url:
        lines.append(f"Tableau de bord : {url}")
    return await channel.send(Notification(
        title="Lidoire : test de notification",
        message="\n".join(lines),
        priority=2,
        tags=("test_tube",),
        click=url or None,
    ))
