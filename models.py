"""Plugin-owned normalized models; missing source values remain unknown.

IDs are strings. All adapter-produced datetimes are aware UTC. Ratings and
unit/value exchanges are distinct; these models claim no lifetime coverage.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
import math
from typing import Any, Generic, Literal, TypeVar
from urllib.parse import quote

Outcome = Literal["win", "loss", "unknown"]
AnalysisOutcome = Literal["win", "loss", "conflict"]
PROFILE_AXES = (("kd", "交换效率"), ("contrib", "摧毁贡献"), ("obj", "夺点表现"),
                ("outcome", "赛果超预期"), ("win_rate", "实际胜率"))
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
    # Source analysis keys are retained; IDs, numbers, booleans and times are normalized.
    analysis: dict[str, Any] = field(default_factory=dict)


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
    damage_dealt: float | None = None
    damage_received: float | None = None
    objectives: int | None = None
    spawned_value: float | None = None
    supply: float | None = None
    # Keep the pre-match observation even when NewRating is unavailable.
    elo_before: float | None = None
    # An empty supplied UnitData is different from a missing UnitData.
    units_available: bool = False
    refunded_value: float | None = None
    supply_destroyed: float | None = None
    supply_consumed_by_allies: float | None = None
    supply_captured: float | None = None
    supply_airdropped: float | None = None
    supply_captured_by_enemy: float | None = None


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
    map_id: str | None = None
    # Start/end are source observations, never inferred from duration.
    ended_at: datetime | None = None


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


def analysis_outcomes(analysis: Mapping | None) -> dict[str, AnalysisOutcome]:
    """Resolve explicit results by exact match ID; absence is distinct from conflict."""
    outcomes: dict[str, AnalysisOutcome] = {}
    for point in (analysis or {}).get("trend", {}).get("points", []):
        try:
            mid = canonical_id(point.get("matchId"))
        except ValueError:
            continue
        won = point.get("won")
        if point.get("wonConflict") is True:
            outcomes[mid] = "conflict"
        elif isinstance(won, bool):
            result = "win" if won else "loss"
            outcomes[mid] = result if outcomes.get(mid, result) == result else "conflict"
    return outcomes


def win_rate_sample(outcomes: Iterable[AnalysisOutcome]) -> dict:
    wins = losses = 0
    for outcome in outcomes:
        wins += outcome == "win"
        losses += outcome == "loss"
    size = wins + losses
    return {"value": 100 * wins / size if size else None,
            "sample_size": size, "wins": wins, "losses": losses}


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


def _nonnegative(value: Any) -> float | None:
    result = _number(value)
    if result is not None and result < 0:
        raise ValueError("expected a nonnegative number")
    return result


def _percentage(value: Any) -> float | None:
    result = _nonnegative(value)
    if result is not None and result > 100:
        raise ValueError("expected a percentage in 0..100")
    return result


def _array(obj: dict[str, Any], key: str) -> list:
    result = obj.get(key, [])
    if not isinstance(result, list):
        raise ValueError(f"invalid {key} array")
    return result


def _parse_analysis(payload: Any) -> dict[str, Any]:
    analysis = _object(payload)
    if "matchCount" not in analysis and "trend" not in analysis:
        raise ValueError("missing player analysis")
    trend = _object(analysis.get("trend", {}))
    points: list[dict[str, Any]] = []
    positions: dict[str, int] = {}
    results: dict[str, set[bool]] = {}
    for raw in _array(trend, "points"):
        row = _object(raw)
        won = row.get("won")
        if won is not None and not isinstance(won, bool):
            raise ValueError("expected a boolean won")
        point = {
            "matchId": canonical_id(row["matchId"]) if row.get("matchId") is not None else None,
            "won": won,
            "ratingBefore": _number(row.get("ratingBefore")),
            "ratingAfter": _number(row.get("ratingAfter")),
            "kdRatio": _nonnegative(row.get("kdRatio")),
            "dmr": _nonnegative(row.get("dmr")),
            "destructionScore": _nonnegative(row.get("destructionScore")),
            "lossesScore": _nonnegative(row.get("lossesScore")),
            "objectivesCaptured": _number(row.get("objectivesCaptured"), count=True),
            "endTime": utc_datetime(row.get("endTime")),
        }
        mid = point["matchId"]
        if mid is None:
            points.append(point)
            continue
        outcomes = results.setdefault(mid, set())
        if won is not None:
            outcomes.add(won)
        if mid not in positions:
            positions[mid] = len(points)
            points.append(point)
        else:
            previous = points[positions[mid]]
            # Keep the newest dated record, but never resolve contradictory results by order.
            if point["endTime"] is not None and (
                previous["endTime"] is None or point["endTime"] > previous["endTime"]
            ):
                points[positions[mid]] = point
    for mid, outcomes in results.items():
        point = points[positions[mid]]
        point["won"] = next(iter(outcomes)) if len(outcomes) == 1 else None
        if len(outcomes) > 1:
            point["wonConflict"] = True
    units = []
    for raw in _array(analysis, "highlightUnits"):
        row = _object(raw)
        if row.get("unitId") is None:
            continue
        units.append({
            "unitId": canonical_id(row.get("unitId")),
            "unitName": _name(row.get("unitName")) or None,
            "spawnCount": _number(row.get("spawnCount"), count=True),
            "totalDamage": _nonnegative(row.get("totalDamage")),
            "avgRoi": _number(row.get("avgRoi")),
            **({"categoryType": _number(row["categoryType"], count=True)}
               if "categoryType" in row else {}),
            **({"totalCost": _nonnegative(row["totalCost"])} if "totalCost" in row else {}),
        })
    categories = []
    for raw in _array(analysis, "categoryPreferences"):
        row = _object(raw)
        if row.get("categoryKey") is None:
            continue
        categories.append({
            "categoryKey": canonical_id(row.get("categoryKey")),
            "percentage": _percentage(row.get("percentage")),
            **({"totalCost": _nonnegative(row["totalCost"])} if "totalCost" in row else {}),
        })
    maps = []
    for raw in _array(analysis, "mapPerformance"):
        row = _object(raw)
        maps.append({
            "mapId": canonical_id(row.get("mapId")),
            "mapName": _name(row.get("mapName")) or None,
            "matchCount": _number(row.get("matchCount"), count=True),
            "winRate": _percentage(row.get("winRate")),
        })
    return {
        "matchCount": _number(analysis.get("matchCount"), count=True),
        "trend": {"points": points},
        "highlightUnits": units,
        "categoryPreferences": categories,
        "mapPerformance": maps,
    }


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
    player.analysis = _parse_analysis(envelope.get("analysis"))
    observations: list[tuple[datetime, float]] = []
    for point in player.analysis["trend"]["points"]:
        rating = point["ratingAfter"]
        at = point["endTime"]
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
            refunded = unit.get("WasRefunded", False)
            if not isinstance(refunded, bool):
                raise ValueError("expected a boolean WasRefunded")
            airdropped = unit.get("WasAirdropped")
            if airdropped is not None and not isinstance(airdropped, bool):
                raise ValueError("expected a boolean WasAirdropped")
            normalized_units.append({
                "id": canonical_id(unit.get("Id")),
                "name": _name(unit.get("Name")) or None,
                "count": 1,
                "spawn_time": _nonnegative(unit.get("SpawnTime", 0)),
                "death_time": _nonnegative(unit.get("DeathTime", 0)),
                "was_refunded": refunded,
                "killed_count": _number(unit.get("KilledCount", 0), count=True),
                **({"was_airdropped": airdropped} if "WasAirdropped" in unit else {}),
                **({"building_destroyed_count": _number(unit["BuildingDestroyedCount"], count=True)}
                   if "BuildingDestroyedCount" in unit else {}),
            })
        players.append(Participant(
            player_id=pid, name=_name(row.get("Name")), team=str(team),
            elo=new, elo_delta=(new - old if old is not None and new is not None else None),
            kills=_number(row.get("Destruction", 0), count=True),
            losses=_number(row.get("Losses", 0), count=True),
            destroyed_value=_number(row.get("DestructionScore", 0)),
            lost_value=_number(row.get("LossesScore", 0)), units=normalized_units,
            damage_dealt=_nonnegative(row.get("DamageDealt")),
            damage_received=_nonnegative(row.get("DamageReceived")),
            objectives=_number(row.get("ObjectivesCaptured"), count=True),
            spawned_value=_nonnegative(row.get("TotalSpawnedUnitScore")),
            supply=_nonnegative(row.get("SupplyPointsConsumed")),
            elo_before=old, units_available="UnitData" in row,
            refunded_value=_nonnegative(row.get("TotalRefundedUnitScore")),
            supply_destroyed=_nonnegative(row.get("SupplyDestroyed")),
            supply_consumed_by_allies=_nonnegative(row.get("SupplyPointsConsumedByAllies")),
            supply_captured=_nonnegative(row.get("SupplyCaptured")),
            supply_airdropped=_nonnegative(row.get("SupplyAirdropped")),
            supply_captured_by_enemy=_nonnegative(row.get("SupplyCapturedByEnemy")),
        ))
    duration = _number(info.get("TotalPlayTimeInSec"))
    if duration is not None and duration < 0:
        raise ValueError("negative duration")
    start = utc_datetime(info.get("StartTime"))
    end = utc_datetime(info.get("EndTime"))
    if end is not None and start is not None and end < start:
        raise ValueError("match ends before it starts")
    # On timed point victories BATrace sometimes omits the default Alpha enum.
    # Require the recorded final points in that case, never the ELO change.
    winner = info.get("WinnerTeam")
    winner_team = str(winner) if type(winner) is int and winner in (0, 1) else None
    points = info.get("TeamMatchPoints")
    if ("WinnerTeam" not in info and end is not None and info.get("EndMatchReason") == 2
            and isinstance(points, dict) and all(type(points.get(k)) is int for k in ("Alpha", "Bravo"))
            and points["Alpha"] > points["Bravo"] >= 0):
        winner_team = "0"
    # Type enum meanings are not documented by the reference; zero ELO change
    # alone cannot distinguish custom, incomplete and unrated matches.
    complete = detail and bool(players) and end is not None and all(
        isinstance(row, dict) and isinstance(row.get("UnitData"), dict) for row in rows.values()
    )
    map_id = canonical_id(info["MapId"]) if "MapId" in info else None
    return Match(
        id=requested_id, map_name=(f"地图#{map_id}" if map_id is not None else None),
        map_id=map_id, winner_team=winner_team,
        started_at=start, duration_seconds=duration, ended_at=end,
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
    return sorted(result.values(), key=lambda m: (m.ended_at or m.started_at or minimum, m.id), reverse=True)
