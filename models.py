"""Plugin-owned normalized models; missing source values remain unknown.

IDs are strings. All adapter-produced datetimes are aware UTC. Ratings and
unit/value exchanges are distinct; these models claim no lifetime coverage.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import math
from typing import Any, Generic, Literal, TypeVar
from urllib.parse import quote

Outcome = Literal["win", "loss", "unknown"]
Mode = Literal["ranked", "custom", "unknown"]
T = TypeVar("T")


@dataclass
class Player:
    id: str
    name: str
    steam_id: str | None = None
    elo: float | None = None
    source_url: str | None = None
    updated_at: datetime | None = None
    elo_observed_at: datetime | None = None


@dataclass
class Participant:
    player_id: str
    name: str
    team: str | None = None
    outcome: Outcome = "unknown"
    elo: float | None = None
    elo_delta: float | None = None
    kills: int | None = None
    losses: int | None = None
    destroyed_value: float | None = None
    lost_value: float | None = None
    units: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Match:
    id: str
    map_name: str | None = None
    mode: Mode = "unknown"
    started_at: datetime | None = None
    duration_seconds: float | None = None
    winner_team: str | None = None
    players: list[Participant] = field(default_factory=list)
    complete: bool = False
    source_url: str | None = None
    updated_at: datetime | None = None


@dataclass
class FetchResult(Generic[T]):
    data: T
    fetched_at: datetime
    stale: bool = False
    source_updated_at: datetime | None = None
    stale_reason: str | None = None


def utc_datetime(value: Any) -> datetime | None:
    """Parse Unix seconds or ISO with an explicit zone; never guess local time."""
    from datetime import timezone
    import math

    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("invalid timestamp")
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            raise ValueError("invalid timestamp")
        return datetime.fromtimestamp(value, timezone.utc)
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError("invalid timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp needs an explicit timezone")
    return parsed.astimezone(timezone.utc)


SOURCE_URL = "https://dash.batrace.top/"


def canonical_id(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("expected a string or integer ID")
    result = str(value)
    if not result or result != result.strip() or len(result) > 128:
        raise ValueError("invalid ID")
    return result


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("expected an object")
    return value


def _number(value: Any, *, count: bool = False) -> float | int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("expected a number")
    if not math.isfinite(value):
        raise ValueError("expected a finite number")
    if count:
        if value < 0 or int(value) != value:
            raise ValueError("expected a nonnegative count")
        return int(value)
    return value


def _name(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("expected a name")
    return value


def parse_search(payload: Any) -> list[Player]:
    """Search has an explicit players array; missing arrays are schema failures."""
    rows = _object(payload).get("players")
    if not isinstance(rows, list):
        raise ValueError("missing players array")
    result: dict[str, Player] = {}
    for row in rows:
        p = _object(row)
        pid = canonical_id(p.get("id"))
        candidate = Player(
            id=pid, name=_name(p.get("name")), elo=_number(p.get("rating")),
            updated_at=utc_datetime(p.get("updated_at")),
            source_url=SOURCE_URL + "player/" + quote(pid, safe=""),
        )
        # Preserve first-seen ordering and identity; a duplicate ID may be renamed.
        if pid not in result or (
            candidate.updated_at is not None
            and (result[pid].updated_at is None or candidate.updated_at > result[pid].updated_at)
        ):
            result[pid] = candidate
    return list(result.values())


def parse_player(payload: Any, requested_id: str) -> Player:
    """Verify a canonical search echo, then use dated analysis rating observations.

    The info endpoint's documented shape has no ID, so it cannot prove identity.
    This private cache envelope contains responses from search and analysis.
    """
    envelope = _object(payload)
    candidates = parse_search(envelope.get("search"))
    player = next((p for p in candidates if p.id == requested_id), None)
    if player is None:
        raise ValueError("canonical player ID was not echoed")
    analysis = _object(envelope.get("analysis"))
    if "matchCount" not in analysis and "trend" not in analysis:
        raise ValueError("missing player analysis")
    if "matchCount" in analysis:
        _number(analysis["matchCount"], count=True)
    trend = _object(analysis.get("trend", {}))
    points = trend.get("points", [])
    if not isinstance(points, list):
        raise ValueError("invalid trend points")
    observations: list[tuple[datetime, float]] = []
    for row in points:
        point = _object(row)
        rating = _number(point.get("ratingAfter"))
        at = utc_datetime(point.get("endTime"))
        if rating is not None and at is not None:
            observations.append((at, rating))
    if observations:
        at, rating = max(observations, key=lambda item: item[0])
        # Search rating is a profile snapshot; retain it if it is demonstrably newer.
        if player.elo is None or player.updated_at is None or at >= player.updated_at:
            player.elo = rating
            player.elo_observed_at = at
    return player


def parse_match(payload: Any, requested_id: str, *, detail: bool = True) -> Match:
    """Adapt the observed protobuf-shaped matchInfo, not arbitrary JSON objects.

    Only the reference-confirmed protobuf defaults are applied (team and four
    numeric counters). Ratings, times, names and mode never default to zero.
    """
    root = _object(payload)
    info = _object(root.get("matchInfo"))
    if "matchId" in root and canonical_id(root["matchId"]) != requested_id:
        raise ValueError("match ID mismatch")
    rows = info.get("Data")
    if not isinstance(rows, dict):
        raise ValueError("missing match participant data")
    players: list[Participant] = []
    seen: set[str] = set()
    for raw in rows.values():
        row = _object(raw)
        pid = canonical_id(row.get("Id"))
        if pid in seen:
            raise ValueError("duplicate participant ID")
        seen.add(pid)
        team = row.get("TeamId", 0)
        if isinstance(team, bool) or not isinstance(team, int):
            raise ValueError("invalid team ID")
        old, new = _number(row.get("OldRating")), _number(row.get("NewRating"))
        units = row.get("UnitData", {})
        if not isinstance(units, dict):
            raise ValueError("invalid unit data")
        normalized_units = []
        for unit in units.values():
            unit = _object(unit)
            normalized_units.append({
                "id": canonical_id(unit.get("Id")),
                "name": _name(unit.get("Name")) or None,
                "count": 1,
            })
        players.append(Participant(
            player_id=pid, name=_name(row.get("Name")), team=str(team),
            elo=new, elo_delta=(new - old if old is not None and new is not None else None),
            kills=_number(row.get("Destruction", 0), count=True),
            losses=_number(row.get("Losses", 0), count=True),
            destroyed_value=_number(row.get("DestructionScore", 0)),
            lost_value=_number(row.get("LossesScore", 0)), units=normalized_units,
        ))
    duration = _number(info.get("TotalPlayTimeInSec"))
    if duration is not None and duration < 0:
        raise ValueError("negative duration")
    start = utc_datetime(info.get("StartTime"))
    end = utc_datetime(info.get("EndTime"))
    if start is None and end is not None and duration is not None:
        from datetime import timedelta
        start = end - timedelta(seconds=duration)
    if end is not None and start is not None and end < start:
        raise ValueError("match ends before it starts")
    # The reference infers victory from rating changes. This is not an
    # authoritative outcome field: retain unknown even for consistent deltas.
    # Type enum meanings are not documented by the reference; zero ELO change
    # alone cannot distinguish custom, incomplete and unrated matches.
    complete = detail and bool(players) and end is not None and all(
        isinstance(row, dict) and isinstance(row.get("UnitData"), dict) for row in rows.values()
    )
    return Match(
        id=requested_id, map_name=(f"地图#{canonical_id(info['MapId'])}" if "MapId" in info else None),
        started_at=start, duration_seconds=duration,
        players=players, complete=complete,
        source_url=SOURCE_URL + "match/" + quote(requested_id, safe=""),
    )


def parse_recent(payload: Any) -> list[Match]:
    rows = _object(payload).get("matches")
    if not isinstance(rows, list):
        raise ValueError("missing matches array")
    result: dict[str, Match] = {}
    for raw in rows:
        row = _object(raw)
        mid = canonical_id(row.get("matchId"))
        match = parse_match({"matchInfo": row.get("data")}, mid, detail=False)
        if mid not in result or len(match.players) > len(result[mid].players):
            result[mid] = match
    from datetime import timezone
    minimum = datetime.min.replace(tzinfo=timezone.utc)
    return sorted(result.values(), key=lambda m: (m.started_at or minimum, m.id), reverse=True)
