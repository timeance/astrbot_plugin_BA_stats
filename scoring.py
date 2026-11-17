"""Per-match scoring adapted from ba-dragon, under the MIT license.

Copyright (c) 2026 AlexanderHYu
Copyright (c) 2025-2026 Zola (Zawinzala)
Full original copyright, license and pinned source paths are bundled in
assets/scoring-model.json. Only computeDragonScore's individual career rows
are reproduced: no player aggregate, titles, or timeline. Missing career
composition uses the upstream avgRoles fallback; missing match inputs do not.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

try:
    from .models import Match, Participant
except ImportError:  # Standalone tests use the repository as their import root.
    from models import Match, Participant


MODEL = json.loads((Path(__file__).parent / "assets" / "scoring-model.json").read_text(encoding="utf-8"))
ROLE_KEYS = ("armor", "inf", "recon", "arty", "aa", "heli", "jet")
CAT_KEYS = ("recon", "infantry", "vehicles", "support", "logistic", "helicopters", "aircrafts")
ROLE_NAMES = {"armor": "装甲", "inf": "步兵", "recon": "侦察", "arty": "炮兵",
              "aa": "防空", "heli": "直升机", "jet": "固定翼", "_": "后勤/运输"}
# score.ts TRADE and TRADE_NORM, unchanged from the pinned source.
_TRADE_KILL = dict(armor=1.2, inf=1.2, recon=1.1, aa=1.0, heli=0.9, jet=0.8, arty=0.75)
_TRADE_LOSS = dict(armor=0.75, inf=0.7, recon=0.9, aa=1.0, heli=1.0, jet=1.1, arty=1.4)
_TRADE_NORM = {"kill": 0.9719, "loss": 1.1218}


def _finite(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _nonnegative(value) -> bool:
    return _finite(value) and value >= 0


def _old_rating(person: Participant) -> float | None:
    old = person.elo_before
    if old is None and _finite(person.elo) and _finite(person.elo_delta):
        old = person.elo - person.elo_delta
    return float(old) if _finite(old) else None


def _expect(own: float, opponent: float) -> float:
    exponent = (opponent - own) / MODEL["expect"]["scale"]
    # Equivalent limits of the upstream Elo formula, without numeric overflow.
    if exponent > 308:
        return 0.0
    return 1 / (1 + 10 ** exponent)


def match_prediction(match: Match) -> dict[str, float] | None:
    """A=team '0', B=team '1'; percentages in 0..100, using pre-match ELO.

    Every participant on both teams must have a pre-match observation. No AFK
    penalty applies to a pre-match prediction. Spectators are excluded.
    """
    teams = [[p for p in match.players if p.team == team] for team in ("0", "1")]
    if any(not team for team in teams):
        return None
    averages = []
    for team in teams:
        ratings = [_old_rating(p) for p in team]
        if any(r is None for r in ratings):
            return None
        averages.append(sum(ratings) / len(ratings))
    a = 100 * _expect(*averages)
    return {"a": a, "b": 100 - a}


def _career_roles(analysis: Mapping | None) -> dict[str, float] | None:
    """rolesFromCareer, including the upstream prior of 30% category spend."""
    analysis = analysis or {}
    categories = analysis.get("categoryPreferences", [])
    highlights = analysis.get("highlightUnits", [])
    if not isinstance(categories, list) or not isinstance(highlights, list):
        return None
    buckets = {}
    for unit in highlights:
        if not isinstance(unit, Mapping):
            return None
        category, cost = unit.get("categoryType"), unit.get("totalCost")
        if not _nonnegative(category) or int(category) != category or not _nonnegative(cost):
            return None
        if category >= len(CAT_KEYS):
            continue
        bucket = buckets.setdefault(CAT_KEYS[int(category)], {"total": 0.0})
        bucket["total"] += cost
        entry = MODEL["units"].get(str(unit.get("unitId")))
        role = entry[0] if entry and entry[0] else "_none"
        bucket[role] = bucket.get(role, 0.0) + cost
    roles = dict.fromkeys(ROLE_KEYS, 0.0)
    for category in categories:
        if not isinstance(category, Mapping) or not _nonnegative(category.get("totalCost")):
            return None
        key, spend = str(category.get("categoryKey")), category["totalCost"]
        default = MODEL["catSplit"].get(key, {})
        bucket = buckets.get(key, {})
        prior = 0.3 * spend
        denominator = bucket.get("total", 0) + prior
        for role in ROLE_KEYS:
            share = ((bucket.get(role, 0) + prior * default.get(role, 0)) / denominator
                     if denominator > 0 else default.get(role, 0))
            roles[role] += spend * share
    total = sum(roles.values())
    if total <= 0:
        return dict(MODEL["avgRoles"])
    return {role: value / total for role, value in roles.items()}


def _career_trade(roles: Mapping) -> tuple[float, float]:
    """tradeMult({Id: stbid}, roles): no per-unit trade data in career rows."""
    total = sum(roles.get(role, 0) for role in ROLE_KEYS)
    if total <= 0:
        return 1.0, 1.0
    kill = sum(roles.get(role, 0) * _TRADE_KILL[role] for role in ROLE_KEYS) / total
    loss = sum(roles.get(role, 0) * _TRADE_LOSS[role] for role in ROLE_KEYS) / total
    return kill * _TRADE_NORM["kill"], loss * _TRADE_NORM["loss"]


def _inactive(match: Match, players: list[Participant]) -> set[str]:
    """score.ts detectInactive; protobuf-zero counters are normalized upstream."""
    result = set()
    start = match.started_at.timestamp() if match.started_at else 0
    duration = match.duration_seconds or 0
    for team_id in ("0", "1"):
        team = [p for p in players if p.team == team_id]
        average = sum(p.destroyed_value for p in team) / (len(team) or 1)
        for p in team:
            if p.destroyed_value + p.lost_value == 0:
                result.add(p.player_id)
                continue
            spawns = [u.get("spawn_time") for u in p.units if _nonnegative(u.get("spawn_time"))
                      and u["spawn_time"] != 0]
            if spawns and start and duration:
                if (max(spawns) - start) / duration < 0.4 and p.destroyed_value < 0.15 * average:
                    result.add(p.player_id)
    return result


def _match_features(match: Match, me: Participant, trade: tuple[float, float]) -> dict | None:
    """The matchFeatures inputs used by computeDragonScore, with strict gaps."""
    players = [p for p in match.players if p.team in ("0", "1")]
    if me.team not in ("0", "1") or not players:
        return None
    # Financial counters and rating observations cannot be reconstructed from
    # analysis trend points or post-match team averages.
    if any(not _nonnegative(p.destroyed_value) or not _nonnegative(p.lost_value)
           or _old_rating(p) is None or not _finite(p.elo) for p in players):
        return None
    old = _old_rating(me)
    if old is None or not _finite(me.elo) or abs(me.elo - old) < 0.01:
        return None
    team = [p for p in players if p.team == me.team]
    opponent = [p for p in players if p.team != me.team]
    if not team or not opponent:
        return None
    # Remove OTHER inactive players only: do not give oneself AFK relief.
    inactive = _inactive(match, players) - {me.player_id}
    active_team = [p for p in team if p.player_id not in inactive]
    active_opponent = [p for p in opponent if p.player_id not in inactive]
    average = lambda members: sum(_old_rating(p) for p in members) / len(members)
    penalty = MODEL["expect"]["afkPenalty"]
    # The upstream model explicitly uses 0.5 when an entire side is inactive.
    expected = 0.5
    if active_team and active_opponent:
        expected = _expect(average(active_team) - penalty * (len(team) - len(active_team)),
                           average(active_opponent) - penalty * (len(opponent) - len(active_opponent)))
    kill, loss = trade
    dw, lw = me.destroyed_value * kill, me.lost_value * loss
    avg_dw = sum(p.destroyed_value * (kill if p.player_id == me.player_id else 1)
                 for p in active_team) / len(active_team)
    # The pinned model gives objectives zero weight; retain upstream zero
    # handling without treating a missing displayed objective as an observation.
    if MODEL["weights"]["obj"] and any(not _nonnegative(p.objectives) for p in players):
        return None
    objectives = me.objectives or 0
    avg_objectives = sum(p.objectives or 0 for p in active_team) / len(active_team)
    return {"eloN": (old - 2100) / 400, "gapN": (old - average(players)) / 400,
            "S": 1 if me.elo > old else 0, "E": expected,
            "x": {"kd": math.log2((dw + 200) / (lw + 200)),
                  "con": math.log2((dw + 200) / (avg_dw + 200)),
                  "obj": math.log2((objectives + 1) / (avg_objectives + 1))}}


def _percentile(value: float, quantiles: list[float]) -> float:
    """score.ts pctOf: linear interpolation in the fitted empirical CDF."""
    n = len(quantiles) - 1
    if value <= quantiles[0]:
        return 0.0
    if value >= quantiles[n]:
        return 1.0
    lo, hi = 0, n
    while hi - lo > 1:
        mid = (lo + hi) >> 1
        if quantiles[mid] <= value:
            lo = mid
        else:
            hi = mid
    span = quantiles[hi] - quantiles[lo]
    return (lo + ((value - quantiles[lo]) / span if span > 0 else 0.5)) / n


def _to10(percentile: float) -> float:
    # JS Math.round differs from Python round at exactly half-way values.
    return math.floor((1 + 9 * percentile) * 10 + 0.5) / 10


def recent_score(match: Match, participant: Participant, analysis: Mapping | None) -> float | None:
    """Return the exact individual computeDragonScore career-row score, 1..10.

    Career categories with missing spend or highlight category/cost return None.
    Empty career composition uses MODEL.avgRoles, exactly as upstream does.
    UnitData affects the upstream AFK detector but never replaces career roles
    or supplies per-unit trade multipliers here. Teammates use multiplier 1.
    """
    me = next((p for p in match.players if p.player_id == participant.player_id), None)
    if me is None or (analysis is not None and not isinstance(analysis, Mapping)):
        return None
    roles = _career_roles(analysis)
    if roles is None:
        return None
    features = _match_features(match, me, _career_trade(roles))
    if features is None:
        return None
    norms, weights = MODEL["norm"]["career"], MODEL["weights"]
    z = {}
    for metric in ("kd", "con", "obj"):
        norm = norms[metric]
        mu = norm["elo"] * features["eloN"] + norm["gap"] * features["gapN"]
        variance = 0.0
        for role in ROLE_KEYS:
            mu += roles[role] * norm["mu"][role]
            variance += roles[role] * norm["sigma"][role] ** 2
        z[metric] = (features["x"][metric] - mu) / math.sqrt(variance or 1)
    obj_factor = sum(roles[r] for r in ("armor", "inf", "recon"))
    obj_factor += 0.2 * sum(roles[r] for r in ("arty", "aa", "heli", "jet"))
    stat = (weights["kd"] * z["kd"] + weights["con"] * z["con"]
            + weights["obj"] * obj_factor * z["obj"])
    stat /= weights["kd"] + weights["con"] + weights["obj"] * obj_factor
    combined = stat + weights["out"] * (features["S"] - features["E"]) / MODEL["outSd"]
    return _to10(_percentile(combined, MODEL["matchPct"]["career"]))


def score_band(score: float | None) -> Literal["low", "medium", "high"] | None:
    """Color bands only, using the displayed-score thresholds in markOf."""
    if not _finite(score) or not 1 <= score <= 10:
        return None
    displayed = math.floor(score * 10 + 0.5) / 10
    if displayed >= _to10(MODEL["marks"]["dragon"]):
        return "high"
    if displayed <= _to10(MODEL["marks"]["qu"]):
        return "low"
    return "medium"


def team_composition(match: Match, team: str | None) -> dict:
    """Estimate spend shares from the pinned model's BASE prices per spawn.

    coverage is 0..1: identified, priced spawn count / all recorded spawn
    count. It is NOT cost coverage. Missing UnitData is separately counted.
    With an unpriced spawn or a missing participant's UnitData, the all-spawn
    denominator is unknowable: categories stays empty instead of renormalizing
    known spend to 100%. Transport/logistics with known IDs remain identified.
    An explicitly empty UnitData produces no categories, never seven zeroes.
    """
    members = [p for p in match.players if p.team == team]
    missing = sum(not p.units_available for p in members)
    total_count = known_count = unknown_count = 0
    costs = dict.fromkeys((*ROLE_KEYS, "_"), 0.0)
    for person in members:
        for unit in person.units:
            count = unit.get("count", 1)
            if not _nonnegative(count) or int(count) != count:
                missing += 1
                continue
            total_count += count
            entry = MODEL["units"].get(str(unit.get("id")))
            if not entry or not _nonnegative(entry[1]):
                unknown_count += count
                continue
            known_count += count
            costs[entry[0] or "_"] += entry[1] * count
    result = {"categories": [], "coverage": known_count / total_count if total_count else 0.0,
              "unknown_count": unknown_count, "missing_participants": missing, "estimated": True}
    total_cost = sum(costs.values())
    if not missing and not unknown_count and total_cost > 0:
        result["categories"] = [{"name": ROLE_NAMES[role], "percentage": cost / total_cost * 100}
                                for role, cost in costs.items() if cost > 0]
    return result
