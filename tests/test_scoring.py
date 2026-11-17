"""Synthetic fixtures only. Anchor values were obtained from pinned upstream TS.

Offline oracle: ba-dragon 072dc9bc2d73ffc87000c1d3db4c0f9b4dcab549,
computeDragonScore({matches:[fixture], ...analysis}).rows[0].score, Node 24
type stripping. Another 160 deterministic synthetic cases were compared during
development (including intermediate roles, AFK/Elo features and trade logs).
"""

from copy import deepcopy

import pytest

from models import Match, Participant, parse_match
from scoring import MODEL, match_prediction, recent_score, score_band, team_composition


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
def test_incomplete_career_spend_is_unknown_not_percentage_substitution(analysis):
    match = normalized()
    assert recent_score(match, match.players[0], analysis) is None


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


def test_bundled_model_has_pinned_source_and_complete_mit_notice_without_titles():
    source = MODEL["source"]
    assert source["commit"] == "072dc9bc2d73ffc87000c1d3db4c0f9b4dcab549"
    assert source["license"] == "MIT"
    assert "Copyright (c) 2026 AlexanderHYu" in source["licenseText"]
    assert "Copyright (c) 2025-2026 Zola (Zawinzala)" in source["licenseText"]
    assert 'THE SOFTWARE IS PROVIDED "AS IS"' in source["licenseText"]
    assert not set(MODEL).intersection({"titles", "titleDist", "titleFreq", "tiers", "playerPct", "kalman", "effElo"})
