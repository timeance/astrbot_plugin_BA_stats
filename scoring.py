"""Match and player scoring adapted from ba-dragon, under the MIT license.

Copyright (c) 2026 AlexanderHYu
Copyright (c) 2025-2026 Zola (Zawinzala)
Full original copyright, license and pinned source paths are bundled in
assets/scoring-model.json. Both reports use computeDragonScore's career
observations; only the player overview aggregates them into a score and tier.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

try:
    from .models import PROFILE_AXES, AnalysisOutcome, Match, Participant, analysis_outcomes, win_rate_sample
except ImportError:  # Standalone tests use the repository as their import root.
    from models import PROFILE_AXES, AnalysisOutcome, Match, Participant, analysis_outcomes, win_rate_sample


MODEL = json.loads((Path(__file__).parent / "assets" / "scoring-model.json").read_text(encoding="utf-8"))
ROLE_KEYS = ("armor", "inf", "recon", "arty", "aa", "heli", "jet")
CAT_KEYS = ("recon", "infantry", "vehicles", "support", "logistic", "helicopters", "aircrafts")
_PROFILE_TIERS = {"dragon": ("真龙", "high"), "solid": ("有龙样", "high"),
                  "average": ("泯然众人", "medium"), "weak": ("有点蛆", "low"), "qu": ("纯蛆", "low")}
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

    Both teams must have equal, nonempty rosters and every player needs a
    pre-match observation. No AFK penalty applies; spectators are excluded.
    """
    teams = [[p for p in match.players if p.team == team] for team in ("0", "1")]
    if not teams[0] or len(teams[0]) != len(teams[1]):
        return None
    averages = []
    for team in teams:
        ratings = [_old_rating(p) for p in team]
        if any(r is None for r in ratings):
            return None
        averages.append(sum(ratings) / len(ratings))
    a = 100 * _expect(*averages)
    return {"a": a, "b": 100 - a}


def _career_roles(analysis: Mapping | None) -> dict[str, float]:
    """rolesFromCareer, including the upstream prior of 30% category spend."""
    analysis = analysis or {}
    categories = analysis.get("categoryPreferences")
    highlights = analysis.get("highlightUnits")
    buckets = {}
    for unit in highlights if isinstance(highlights, list) else ():
        if not isinstance(unit, Mapping) or unit.get("unitId") is None:
            continue
        category, cost = unit.get("categoryType"), unit.get("totalCost")
        if not _nonnegative(category) or int(category) != category or not _nonnegative(cost):
            continue
        if category >= len(CAT_KEYS):
            continue
        bucket = buckets.setdefault(CAT_KEYS[int(category)], {"total": 0.0})
        bucket["total"] += cost
        entry = MODEL["units"].get(str(unit.get("unitId")))
        role = entry[0] if entry and entry[0] else "_none"
        bucket[role] = bucket.get(role, 0.0) + cost
    roles = dict.fromkeys(ROLE_KEYS, 0.0)
    for category in categories if isinstance(categories, list) else ():
        if not isinstance(category, Mapping) or not _nonnegative(category.get("totalCost")):
            continue
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


def _match_features(match: Match, me: Participant, trade: tuple[float, float],
                    outcome: AnalysisOutcome | None) -> dict | None:
    """The matchFeatures inputs used by computeDragonScore, with strict gaps."""
    players = [p for p in match.players if p.team in ("0", "1")]
    if me.team not in ("0", "1") or outcome == "conflict":
        return None
    # Financial counters and rating observations cannot be reconstructed from
    # analysis trend points or post-match team averages.
    if any(not _nonnegative(p.destroyed_value) or not _nonnegative(p.lost_value)
           or _old_rating(p) is None for p in players):
        return None
    old = _old_rating(me)
    # Keep the rated-match gate independent of the source of S.
    if not _finite(me.elo) or abs(me.elo - old) < 0.01:
        return None
    won = (me.elo > old) if outcome is None else (outcome == "win")
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
    obj = None
    if all(_nonnegative(p.objectives) for p in active_team):
        avg_obj = sum(p.objectives for p in active_team) / len(active_team)
        obj = math.log2((me.objectives + 1) / (avg_obj + 1))
    elif MODEL["weights"]["obj"]:
        return None
    return {"eloN": (old - 2100) / 400, "gapN": (old - average(players)) / 400,
            "S": int(won), "E": expected,
            "x": {"kd": math.log2((dw + 200) / (lw + 200)),
                  "con": math.log2((dw + 200) / (avg_dw + 200)),
                  "obj": obj}}


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


def _phi(z: float) -> float:
    """score.ts uses this normal CDF approximation for the outcome percentile."""
    t = 1 / (1 + 0.2316419 * abs(z))
    d = 0.3989423 * math.exp(-z * z / 2)
    p = d * t * (0.3193815 + t * (-0.3565638 + t * (1.781478 + t * (-1.821256 + t * 1.330274))))
    return 1 - p if z > 0 else p


def _career_performance(features: dict, roles: Mapping) -> tuple[float, dict]:
    norms, weights = MODEL["norm"]["career"], MODEL["weights"]
    z = {}
    for metric, observation in features["x"].items():
        if observation is None:
            continue
        norm = norms[metric]
        mu = norm["elo"] * features["eloN"] + norm["gap"] * features["gapN"]
        variance = 0.0
        for role in ROLE_KEYS:
            mu += roles[role] * norm["mu"][role]
            variance += roles[role] * norm["sigma"][role] ** 2
        z[metric] = (observation - mu) / math.sqrt(variance or 1)
    obj_factor = sum(roles[r] for r in ("armor", "inf", "recon"))
    obj_factor += 0.2 * sum(roles[r] for r in ("arty", "aa", "heli", "jet"))
    stat = weights["kd"] * z["kd"] + weights["con"] * z["con"]
    if weights["obj"]:
        stat += weights["obj"] * obj_factor * z["obj"]
    stat /= weights["kd"] + weights["con"] + weights["obj"] * obj_factor
    out = (features["S"] - features["E"]) / MODEL["outSd"]
    parts = {key: _percentile(z[k], norms[k]["q"]) for k, key in
             (("kd", "kd"), ("con", "contrib"), ("obj", "obj")) if k in z}
    if weights["out"]:
        parts["outcome"] = _phi(out)
    return stat + weights["out"] * out, parts


def recent_score(match: Match, participant: Participant, analysis: Mapping | None) -> float | None:
    """Return the individual computeDragonScore career-row score, 1..10."""
    me = next((p for p in match.players if p.player_id == participant.player_id), None)
    if me is None or (analysis is not None and not isinstance(analysis, Mapping)):
        return None
    roles = _career_roles(analysis)
    outcome = analysis_outcomes(analysis).get(match.id)
    features = _match_features(match, me, _career_trade(roles), outcome)
    if features is None:
        return None
    combined, _ = _career_performance(features, roles)
    return _to10(_percentile(combined, MODEL["matchPct"]["career"]))


def match_score(match: Match, participant: Participant) -> float | None:
    """Use the same fitted scale for every player, with this battle's unit mix."""
    spend = dict.fromkeys(ROLE_KEYS, 0)
    for unit in participant.units:
        entry = MODEL["units"].get(str(unit["id"]))
        if entry is None:
            return None
        role, cost = entry
        if role in spend:
            spend[role] += cost * unit.get("count", 1)
    total = sum(spend.values())
    roles = {k: v / total for k, v in spend.items()} if total else MODEL["avgRoles"]
    outcome = None
    if match.winner_team in ("0", "1"):
        outcome = "win" if participant.team == match.winner_team else "loss"
    features = _match_features(match, participant, _career_trade(roles), outcome)
    if not features:
        return None
    combined, _ = _career_performance(features, roles)
    return _to10(_percentile(combined, MODEL["matchPct"]["career"]))


def match_score_band(score: float | None) -> Literal["low", "medium", "high"] | None:
    if score is None:
        return None
    return "high" if score >= 7.5 else "medium" if score >= 5 else "low"


def profile_rating(matches: list[Match], player_id: str, analysis: Mapping | None) -> dict:
    """Aggregate up to 20 valid recent rows. Input matches are newest first.

    The win-rate axis uses the newest 20 distinct matches with the player,
    independently of score eligibility, and only their explicit results.
    """
    roles = _career_roles(analysis) if analysis is None or isinstance(analysis, Mapping) else None
    outcomes = analysis_outcomes(analysis) if roles is not None else {}
    observations = []
    recent_results = []
    seen = set()
    if roles is not None:
        trade = _career_trade(roles)
        for match in matches:
            if match.id in seen:
                continue
            me = next((p for p in match.players if p.player_id == player_id), None)
            if me is None:
                continue
            seen.add(match.id)
            outcome = outcomes.get(match.id)
            if len(seen) <= 20 and outcome is not None:
                recent_results.append(outcome)
            features = _match_features(match, me, trade, outcome)
            if features is None:
                continue
            combined, parts = _career_performance(features, roles)
            observations.append((combined, (match.duration_seconds or 0) / 60, parts))
            if len(observations) == 20:
                break

    score = title = tone = None
    win_rate = win_rate_sample(recent_results)
    axes = {"win_rate": win_rate["value"]}
    if observations:
        k = MODEL["kalman"]
        mean, variance = 0.0, k["P0"]
        for combined, minutes, _ in reversed(observations):
            variance += k["q"]
            noise = k["r"] / min(1, max(0.3, minutes / k["shortMin"]))
            gain = variance / (variance + noise)
            mean += gain * (combined - mean)
            variance *= 1 - gain
        score = _to10(_percentile(mean, MODEL["playerPct"]))
        tier = next(t for threshold, t in MODEL["tiers"] if score >= _to10(threshold))
        title, tone = _PROFILE_TIERS[tier]
        for key, _ in PROFILE_AXES[:-1]:
            values = [parts[key] for _, _, parts in observations if key in parts]
            if values:
                axes[key] = math.floor(sum(values) / len(values) * 100 + 0.5)
    return {"score": score, "title": title, "tone": tone, "sample_size": len(observations),
            "win_rate_sample": win_rate,
            "dimensions": [{"key": key, "label": label, "value": axes.get(key)}
                           for key, label in PROFILE_AXES]}


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
