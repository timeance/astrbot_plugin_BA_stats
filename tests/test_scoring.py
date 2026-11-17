"""Synthetic fixtures only. Anchor values were obtained from pinned upstream TS.

Offline oracle: ba-dragon 072dc9bc2d73ffc87000c1d3db4c0f9b4dcab549,
computeDragonScore({matches:[fixture], ...analysis}).rows[0].score, Node 24
type stripping. Another 160 deterministic synthetic cases were compared during
development (including intermediate roles, AFK/Elo features and trade logs).
"""

from copy import deepcopy
from pathlib import Path

import pytest

from models import Match, Participant, parse_match, parse_recent
from scoring import MODEL, match_prediction, profile_rating, recent_score, score_band, team_composition


def raw_match():
    return {"StartTime": 1700000000, "EndTime": 1700001200, "TotalPlayTimeInSec": 1200,
            "Data": {
                "a": {"Id": "a", "OldRating": 1900, "NewRating": 1915,
                      "DestructionScore": 4000, "LossesScore": 1500, "ObjectivesCaptured": 3},
                "b": {"Id": "b", "TeamId": 1, "OldRating": 2100, "NewRating": 2085,
                      "DestructionScore": 3000, "LossesScore": 2000, "ObjectivesCaptured": 5},
                "c": {"Id": "c", "OldRating": 2000, "NewRating": 2010,
                      "DestructionScore": 1000, "LossesScore": 4000},
                "d": {"Id": "d", "TeamId": 1, "OldRating": 2200, "NewRating": 2180,
                      "DestructionScore": 1500, "LossesScore": 1000},
            }}


def normalized(raw=None):
    return parse_match({"matchInfo": raw if raw is not None else raw_match()}, "synthetic")


FRONT_ANALYSIS = {
    "categoryPreferences": [{"categoryKey": "vehicles", "totalCost": 10000},
                            {"categoryKey": "infantry", "totalCost": 5000}],
    "highlightUnits": [{"unitId": 13, "categoryType": 2, "totalCost": 4000},
                       {"unitId": 57, "categoryType": 1, "totalCost": 2000},
                       {"unitId": 69, "categoryType": 1, "totalCost": 1000}],
}
SUPPORT_ANALYSIS = {
    "categoryPreferences": [{"categoryKey": "support", "totalCost": 7000},
                            {"categoryKey": "aircrafts", "totalCost": 3000}],
    "highlightUnits": [{"unitId": 4, "categoryType": 3, "totalCost": 2500},
                       {"unitId": 12, "categoryType": 3, "totalCost": 1500},
                       {"unitId": 44, "categoryType": 6, "totalCost": 1000}],
}


@pytest.mark.parametrize("case,analysis,expected", [
    ("average", {}, 9.9), ("front", FRONT_ANALYSIS, 10.0),
    ("support", SUPPORT_ANALYSIS, 8.6), ("inactive-ally", {}, 9.9),
    ("early-last-spawn", {}, 9.9), ("all-zero", {}, 8.0), ("lost", {}, 9.5),
])
def test_scores_match_upstream_typescript_career_rows(case, analysis, expected):
    raw = raw_match()
    if case == "inactive-ally":
        raw["Data"]["c"].update(DestructionScore=0, LossesScore=0)
    if case == "early-last-spawn":
        raw["Data"]["c"].update(DestructionScore=5, UnitData={
            "u": {"Id": 13, "SpawnTime": 1700000010}})
    if case == "all-zero":
        for row in raw["Data"].values():
            row.update(DestructionScore=0, LossesScore=0)
    if case == "lost":
        raw["Data"]["a"]["NewRating"] = 1885
    match = normalized(raw)
    before = deepcopy((match, analysis))
    assert recent_score(match, match.players[0], analysis) == expected
    assert (match, analysis) == before


@pytest.mark.parametrize("start,expected", [(1700000000, 5.8), (None, 8.0)])
def test_missing_start_does_not_trigger_early_spawn_afk_from_an_inferred_time(start, expected):
    # Oracle fixture 11: TS gives 5.8 with StartTime and 8.0 without it.
    raw = {"EndTime": 1700001200, "TotalPlayTimeInSec": 1200, "Data": {
        "a": {"Id": "a", "OldRating": 1372, "NewRating": 1389,
              "DestructionScore": 3775, "LossesScore": 7305},
        "c": {"Id": "c", "OldRating": 1109, "NewRating": 1126,
              "DestructionScore": 1, "LossesScore": 8680,
              "UnitData": {"u": {"Id": 13, "SpawnTime": 1700000020}}},
        "b": {"Id": "b", "TeamId": 1, "OldRating": 1882, "NewRating": 1869,
              "DestructionScore": 11936, "LossesScore": 2820},
        "d": {"Id": "d", "TeamId": 1, "OldRating": 906, "NewRating": 893,
              "DestructionScore": 1, "LossesScore": 5688,
              "UnitData": {"u": {"Id": 13, "SpawnTime": 1700000020}}},
    }}
    if start is not None:
        raw["StartTime"] = start
    analysis = {
        "categoryPreferences": [
            {"categoryKey": "recon", "totalCost": 6984},
            {"categoryKey": "infantry", "totalCost": 11429},
            {"categoryKey": "vehicles", "totalCost": 3748},
            {"categoryKey": "support", "totalCost": 4764},
            {"categoryKey": "logistic", "totalCost": 1892},
            {"categoryKey": "helicopters", "totalCost": 8792},
            {"categoryKey": "aircrafts", "totalCost": 14267},
        ],
        "highlightUnits": [
            {"unitId": 13, "categoryType": 2, "totalCost": 819},
            {"unitId": 57, "categoryType": 1, "totalCost": 222},
            {"unitId": 69, "categoryType": 1, "totalCost": 2189},
            {"unitId": 4, "categoryType": 3, "totalCost": 1252},
            {"unitId": 12, "categoryType": 3, "totalCost": 662},
            {"unitId": 44, "categoryType": 6, "totalCost": 578},
            {"unitId": 8, "categoryType": 5, "totalCost": 316},
            {"unitId": 9999999, "categoryType": 3, "totalCost": 97},
        ],
    }
    match = normalized(raw)
    assert (match.started_at is None) == (start is None)
    assert match.ended_at.timestamp() == 1700001200
    assert recent_score(match, match.players[0], analysis) == expected


@pytest.mark.parametrize("field", ["elo", "elo_delta", "destroyed_value", "lost_value"])
def test_missing_required_match_values_return_none(field):
    match = normalized()
    if field == "elo_delta":
        match.players[0].elo_before = None
    setattr(match.players[0], field, None)
    assert recent_score(match, match.players[0], FRONT_ANALYSIS) is None


def test_missing_teammate_financials_or_opponent_elo_do_not_make_up_a_score():
    match = normalized()
    match.players[2].destroyed_value = None
    assert recent_score(match, match.players[0], {}) is None
    match = normalized()
    match.players[1].elo_before = match.players[1].elo_delta = None
    assert recent_score(match, match.players[0], {}) is None


@pytest.mark.parametrize("analysis", [
    {"categoryPreferences": [{"categoryKey": "vehicles", "percentage": 100}]},
    {"highlightUnits": [{"unitId": 13, "totalCost": 2000}]},
    {"highlightUnits": [{"unitId": 13, "categoryType": 2}]},
])
def test_incomplete_career_spend_uses_the_prior_without_substituting_percentages(analysis):
    match = normalized()
    assert recent_score(match, match.players[0], analysis) == recent_score(match, match.players[0], {}) == 9.9


def test_partial_role_observations_keep_the_usable_spend():
    match = normalized()
    partial = deepcopy(SUPPORT_ANALYSIS)
    partial["highlightUnits"].extend([
        {"unitId": 13, "totalCost": 99999}, {"unitId": 57, "categoryType": 1},
    ])
    partial["categoryPreferences"].append({"categoryKey": "vehicles", "percentage": 100})
    assert recent_score(match, match.players[0], partial) == 8.6


def test_explicit_loss_overrides_positive_elo_and_conflict_blocks_the_fallback():
    match = normalized()
    me = match.players[0]
    loss = {"matchId": match.id, "won": False}
    analysis = {"trend": {"points": [loss, {"matchId": match.id}, loss]}}
    assert recent_score(match, me, analysis) == 9.5
    expected = profile_rating([match], me.player_id, analysis)
    assert expected["sample_size"] == 1
    assert expected["win_rate_sample"] == {"value": 0, "sample_size": 1, "wins": 0, "losses": 1}
    for other in match.players[1:]:
        other.elo = other.elo_delta = None
    assert recent_score(match, me, analysis) == 9.5
    assert profile_rating([match], me.player_id, analysis) == expected
    analysis["trend"]["points"].append({**loss, "won": True})
    assert recent_score(match, me, analysis) is None
    blocked = profile_rating([match], me.player_id, analysis)
    assert blocked["sample_size"] == blocked["win_rate_sample"]["sample_size"] == 0


@pytest.mark.parametrize("post_elo", [1900, 1900.005, None])
def test_explicit_win_does_not_admit_a_match_without_a_valid_elo_change(post_elo):
    match = normalized()
    me = match.players[0]
    me.elo = post_elo
    analysis = {"trend": {"points": [{"matchId": match.id, "won": True}]}}
    assert recent_score(match, me, analysis) is None
    profile = profile_rating([match], me.player_id, analysis)
    assert profile["score"] is None and profile["sample_size"] == 0
    assert profile["win_rate_sample"] == {"value": 100, "sample_size": 1, "wins": 1, "losses": 0}


def test_unrated_spectator_foreign_participant_and_missing_opposition_have_no_score():
    match = normalized()
    match.players[0].elo = match.players[0].elo_before
    assert recent_score(match, match.players[0], {}) is None
    match.players[0].team = "2"
    assert recent_score(match, match.players[0], {}) is None
    assert recent_score(match, Participant("absent", "Absent"), {}) is None
    match.players = [match.players[2]]
    assert recent_score(match, match.players[0], {}) is None


def test_career_rows_do_not_switch_to_per_unit_trade_or_match_role_scoring():
    match = normalized()
    baseline = recent_score(match, match.players[0], SUPPORT_ANALYSIS)
    match.players[0].units_available = True
    match.players[0].units = [{"id": "13", "count": 50, "killed_count": 400,
                               "death_time": 1700001000, "was_refunded": False}]
    assert recent_score(match, match.players[0], SUPPORT_ANALYSIS) == baseline == 8.6


def test_prediction_uses_all_pre_match_ratings_including_inactive_players():
    match = normalized()
    prediction = match_prediction(match)
    assert prediction["a"] == pytest.approx(24.02530733520421)
    assert prediction["a"] + prediction["b"] == pytest.approx(100)
    match.players[0].destroyed_value = match.players[0].lost_value = 0
    for person in match.players:
        person.elo += 1000  # The retained OldRating takes precedence.
    assert match_prediction(match) == prediction


def test_prediction_can_restore_old_rating_but_never_uses_new_rating_alone():
    match = normalized()
    expected = match_prediction(match)
    for person in match.players:
        person.elo_before = None
    assert match_prediction(match) == expected
    match.players[2].elo_delta = None
    assert match_prediction(match) is None


def test_prediction_accepts_old_rating_without_new_rating_and_ignores_spectators():
    match = normalized()
    expected = match_prediction(match)
    for person in match.players:
        person.elo = person.elo_delta = None
    match.players.insert(0, Participant("observer", "Observer", team="2"))
    match.players.reverse()
    assert match_prediction(match) == expected
    match.players = [p for p in match.players if p.player_id != "d"]
    assert match_prediction(match) is None  # Both sides exist, but one roster is short.
    match.players = [p for p in match.players if p.team != "1"]
    assert match_prediction(match) is None
    assert match_prediction(Match("empty")) is None


def unit_match(ids=(13, 57, 52)):
    raw = raw_match()
    for person in raw["Data"].values():
        person["UnitData"] = {}
    raw["Data"]["a"]["UnitData"] = {str(i): {"Id": uid} for i, uid in enumerate(ids)}
    return normalized(raw)


def test_composition_estimates_base_spend_and_keeps_known_logistics():
    match = unit_match()
    result = team_composition(match, "0")
    assert result["coverage"] == 1
    assert result["unknown_count"] == result["missing_participants"] == 0
    assert result["estimated"] is True
    shares = {c["name"]: c["percentage"] for c in result["categories"]}
    assert shares == pytest.approx({"装甲": 390 / 490 * 100,
                                    "步兵": 60 / 490 * 100, "后勤/运输": 40 / 490 * 100})
    assert sum(shares.values()) == pytest.approx(100)


def test_unknown_unit_price_prevents_renormalizing_known_spend_to_100_percent():
    match = unit_match((13, 13, 99999999))
    result = team_composition(match, "0")
    assert result["coverage"] == pytest.approx(2 / 3)
    assert result["unknown_count"] == 1
    assert result["categories"] == []


def test_missing_unit_data_and_explicit_empty_data_are_distinct_and_not_zero_roles():
    raw = raw_match()
    del raw["Data"]["c"]
    match = normalized(raw)
    result = team_composition(match, "0")
    assert result["missing_participants"] == 1
    assert result["categories"] == [] and result["coverage"] == 0
    raw["Data"]["a"]["UnitData"] = {}
    result = team_composition(normalized(raw), "0")
    assert result["missing_participants"] == 0
    assert result["categories"] == [] and result["coverage"] == 0


def test_known_record_coverage_is_not_team_cost_coverage_when_a_member_is_missing():
    match = unit_match((13,))
    match.players[2].units_available = False
    result = team_composition(match, "0")
    assert result["coverage"] == 1  # Only the recorded spawns are identified.
    assert result["missing_participants"] == 1
    assert result["categories"] == []


def test_composition_matches_raw_none_team_and_keeps_missing_member_detection():
    match = unit_match((13,))
    match.players[0].team = None
    result = team_composition(match, None)
    assert result["categories"] == [{"name": "装甲", "percentage": 100}]
    assert result["coverage"] == 1 and result["missing_participants"] == 0
    match.players.append(Participant("unknown-team-ally", "Ally", team=None))
    result = team_composition(match, None)
    assert result["categories"] == [] and result["missing_participants"] == 1


@pytest.mark.parametrize("score,band", [(1, "low"), (2.8, "low"), (2.9, "medium"),
                                        (8.1, "medium"), (8.2, "high"), (10, "high"),
                                        (None, None), (float("nan"), None), (True, None)])
def test_color_bands_use_upstream_displayed_score_marks(score, band):
    assert score_band(score) == band


def profile_sample():
    # Newest first: short strong win, full-length loss, and a win with an AFK ally.
    short, loss, afk = (raw_match() for _ in range(3))
    short["TotalPlayTimeInSec"] = 210
    short["Data"]["a"].update(DestructionScore=6100, LossesScore=800)
    short["Data"]["c"]["ObjectivesCaptured"] = 1
    loss["Data"]["a"].update(NewRating=1880, DestructionScore=350, LossesScore=6500, ObjectivesCaptured=0)
    loss["Data"]["c"]["ObjectivesCaptured"] = 5
    afk["Data"]["a"].update(DestructionScore=2300, LossesScore=2000, ObjectivesCaptured=1)
    afk["Data"]["c"].update(DestructionScore=0, LossesScore=0)
    return [short, loss, afk]


@pytest.mark.parametrize("indices,analysis,expected", [
    ([0, 1, 2], SUPPORT_ANALYSIS, (1.8, "纯蛆", "low", [30, 49, 56, 75])),
    ([0], FRONT_ANALYSIS, (7.8, "有龙样", "high", [100, 98, 72, 95])),
    ([1], {}, (2.0, "有点蛆", "low", [0, 10, 1, 30])),
])
def test_profile_matches_pinned_compute_dragon_score(indices, analysis, expected):
    # In the mixed TS fixture the individual scores are 9.9, 1.1, 5.1, but
    # the player score is 1.8. Averaging their displayed scores would give 5.4.
    raws = profile_sample()
    matches = [parse_match({"matchInfo": raws[i]}, str(i)) for i in indices]
    before = deepcopy(matches)
    profile = profile_rating(matches, "a", analysis)
    score, title, tone, parts = expected
    assert profile == {
        "score": score, "title": title, "tone": tone, "sample_size": len(matches),
        "win_rate_sample": {"value": None, "sample_size": 0, "wins": 0, "losses": 0},
        "dimensions": [dict(key=key, label=label, value=value) for key, label, value in zip(
            ("kd", "contrib", "obj", "outcome", "win_rate"),
            ("交换效率", "摧毁贡献", "夺点表现", "赛果超预期", "实际胜率"), [*parts, None])],
    }
    assert matches == before


def test_profile_caps_newest_valid_rows_after_normalizing_match_order():
    short, loss, _ = profile_sample()
    unrated = deepcopy(short)
    unrated["Data"]["a"]["NewRating"] = unrated["Data"]["a"]["OldRating"]
    raws = []
    for i, raw in enumerate([loss, *([short] * 20), unrated]):
        start = 1700000000 + i * 3600
        raws.append({"matchId": str(i), "data": {
            **raw, "StartTime": start, "EndTime": start + raw["TotalPlayTimeInSec"],
        }})
    matches = parse_recent({"matches": raws[::2] + raws[1::2]})
    assert [m.id for m in matches] == [str(i) for i in range(21, -1, -1)]
    profile = profile_rating(matches, "a", {})
    assert (profile["score"], profile["title"], profile["sample_size"]) == (10, "真龙", 20)
    assert [d["value"] for d in profile["dimensions"]] == [99, 99, 83, 95, None]


def test_win_rate_uses_newest_twenty_distinct_matches_independently_of_score():
    matches = [Match(str(i), players=[Participant("a", "A", team="0")]) for i in range(21)]
    analysis = {"trend": {"points": [
        {"matchId": 0, "won": False}, {"matchId": 19, "won": True}, {"matchId": 20, "won": True},
    ]}}
    profile = profile_rating([matches[0], *matches], "a", analysis)
    assert profile["score"] is None and profile["sample_size"] == 0
    assert profile["dimensions"][-1]["value"] == 50
    assert profile["win_rate_sample"] == {"value": 50, "sample_size": 2, "wins": 1, "losses": 1}


def test_missing_objectives_omit_only_unobserved_parts_and_never_supply_a_win():
    short, _, afk = profile_sample()
    short["Data"]["c"].pop("ObjectivesCaptured")
    latest = parse_match({"matchInfo": short}, "latest")
    earlier = parse_match({"matchInfo": afk}, "earlier")
    single = profile_rating([latest], "a", FRONT_ANALYSIS)
    assert single["score"] == 7.8
    assert [d["value"] for d in single["dimensions"]] == [100, 98, None, 95, None]
    # Missing objectives for an inactive ally do not affect the active-team comparison.
    earlier_obj = profile_rating([earlier], "a", FRONT_ANALYSIS)["dimensions"][2]["value"]
    assert earlier_obj is not None
    both = profile_rating([latest, earlier], "a", FRONT_ANALYSIS)
    assert both["sample_size"] == 2 and both["dimensions"][2]["value"] == earlier_obj
    empty = profile_rating([latest], "absent", {"trend": {"points": [{"matchId": "latest", "won": True}]}})
    assert (empty["sample_size"], empty["score"], empty["title"], empty["tone"]) == (0, None, None, None)
    assert [d["value"] for d in empty["dimensions"]] == [None] * 5


def test_profile_distinguishes_explicit_zero_from_unknown_values():
    raw = raw_match()
    for player in raw["Data"].values():
        player.update(DestructionScore=0, LossesScore=0, ObjectivesCaptured=0)
    zero = profile_rating([normalized(raw)], "a", {})
    # The TS oracle still scores the explicit zeros and returns an objective percentile.
    assert (zero["score"], zero["title"], zero["sample_size"]) == (7.2, "泯然众人", 1)
    assert [d["value"] for d in zero["dimensions"]] == [61, 58, 54, 86, None]
    raw["Data"]["a"].pop("DestructionScore")  # Protobuf omits zero counters.
    assert profile_rating([normalized(raw)], "a", {}) == zero
    raw["Data"]["a"].pop("ObjectivesCaptured")
    missing = profile_rating([normalized(raw)], "a", {})
    assert missing["score"] == zero["score"]
    assert [d["value"] for d in missing["dimensions"]] == [61, 58, None, 86, None]
    raw["Data"]["a"]["DestructionScore"] = None
    incomplete = profile_rating([normalized(raw)], "a", {})
    assert incomplete["score"] is None and incomplete["sample_size"] == 0
    assert [d["value"] for d in incomplete["dimensions"]] == [None] * 5


def test_bundled_model_has_pinned_source_and_complete_mit_notice_without_match_awards():
    source = MODEL["source"]
    assert source["commit"] == "072dc9bc2d73ffc87000c1d3db4c0f9b4dcab549"
    assert source["license"] == "MIT"
    notice = Path(__file__).resolve().parents[1] / "docs" / "BA_DRAGON_LICENSE.txt"
    assert source["licenseText"].encode("utf-8") == notice.read_bytes()
    assert "Copyright (c) 2026 AlexanderHYu" in source["licenseText"]
    assert "Copyright (c) 2025-2026 Zola (Zawinzala)" in source["licenseText"]
    assert 'THE SOFTWARE IS PROVIDED "AS IS"' in source["licenseText"]
    assert not set(MODEL).intersection({"titles", "titleDist", "titleFreq", "effElo"})
