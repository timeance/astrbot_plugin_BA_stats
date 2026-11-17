"""All payloads below are SYNTHETIC contract fixtures, not captured BATrace data."""

from datetime import datetime, timezone

import pytest

from models import (
    FetchResult, Match, Participant, Player, analysis_outcomes, canonical_id, parse_match,
    parse_player, parse_recent, parse_search, utc_datetime,
)


def raw_match(*, start=1700000000, end=1700000600):
    return {
        "StartTime": start, "EndTime": end, "TotalPlayTimeInSec": 600,
        "MapId": 20, "Type": 99,
        "Data": {
            "a": {"Id": "001", "Name": "Synthetic <A>", "OldRating": 1000,
                  "NewRating": 1012, "Destruction": 2, "Losses": 1,
                  "DestructionScore": 500, "LossesScore": 100, "UnitData": {}},
            "b": {"Id": 2, "Name": "Synthetic B", "TeamId": 1,
                  "OldRating": 1000, "NewRating": 988, "UnitData": {}},
        },
    }


def test_dataclasses_do_not_share_mutable_lists():
    a, b = Match("a"), Match("b")
    a.players.append(Participant("p", "Synthetic"))
    assert b.players == []
    a.players[0].units.append({"id": "u"})
    assert Participant("q", "Another").units == []
    assert Player("001", "Synthetic").steam_id is None
    assert FetchResult([], datetime.now(timezone.utc)).stale is False


def test_scoring_preserves_old_rating_and_unit_lifecycle_without_guessing_unit_availability():
    raw = raw_match()
    raw["Data"]["a"].pop("NewRating")
    raw["Data"]["a"]["UnitData"] = {
        "u": {"Id": 13, "SpawnTime": 1700000010, "DeathTime": 1700000100,
              "KilledCount": 3, "WasRefunded": True},
        "v": {"Id": 57},
    }
    raw["Data"]["b"].pop("UnitData")
    a, b = parse_match({"matchInfo": raw}, "42").players
    assert a.elo_before == 1000 and a.elo is None and a.elo_delta is None
    assert a.units_available is True and b.units_available is False
    assert a.units[0] == {"id": "13", "name": None, "count": 1,
                          "spawn_time": 1700000010, "death_time": 1700000100,
                          "was_refunded": True, "killed_count": 3}
    assert a.units[1]["spawn_time"] == a.units[1]["death_time"] == a.units[1]["killed_count"] == 0
    assert a.units[1]["was_refunded"] is False
    assert b.units == []


def test_scoring_career_spend_and_unit_category_are_retained_when_supplied():
    analysis = analysis_player({"matchCount": 1,
        "categoryPreferences": [{"totalCost": 99999}, {"categoryKey": "vehicles", "totalCost": 2000}],
        "highlightUnits": [{"categoryType": 2, "totalCost": 99999},
                           {"unitId": 13, "categoryType": 2, "totalCost": 1500}],
    }).analysis
    assert len(analysis["categoryPreferences"]) == len(analysis["highlightUnits"]) == 1
    assert analysis["categoryPreferences"][0]["totalCost"] == 2000
    assert analysis["highlightUnits"][0]["categoryType"] == 2
    assert analysis["highlightUnits"][0]["totalCost"] == 1500


@pytest.mark.parametrize("key,value", [("SpawnTime", -1), ("DeathTime", True),
                                      ("KilledCount", 1.5), ("WasRefunded", 1)])
def test_invalid_scoring_unit_fields_are_rejected(key, value):
    raw = raw_match()
    raw["Data"]["a"]["UnitData"] = {"u": {"Id": 13, key: value}}
    with pytest.raises(ValueError):
        parse_match({"matchInfo": raw}, "42")


@pytest.mark.parametrize("raw", [1.0, True, None, "", " 1", "1 "])
def test_identity_does_not_guess_or_round(raw):
    with pytest.raises(ValueError):
        canonical_id(raw)


def test_large_ids_are_strings_and_names_are_never_identity_keys():
    payload = {"players": [
        {"id": "000765611980123456789012345", "name": "Same"},
        {"id": 98765432109876543210987654321, "name": "Same"},
        {"id": "000765611980123456789012345", "name": "Same"},
    ]}
    players = parse_search(payload)
    assert len(players) == 2
    assert players[0].id == "000765611980123456789012345"
    assert players[1].id == "98765432109876543210987654321"
    assert all(p.elo is None and p.steam_id is None for p in players)


def test_newer_search_duplicate_updates_name_without_moving_candidate():
    players = parse_search({"players": [
        {"id": "a", "name": "Old", "updated_at": "2023-01-01T00:00:00Z"},
        {"id": "b", "name": "Other"},
        {"id": "a", "name": "New", "updated_at": "2023-02-01T00:00:00Z"},
    ]})
    assert [(p.id, p.name) for p in players] == [("a", "New"), ("b", "Other")]


def test_links_use_reference_confirmed_routes_and_escape_external_id():
    player = parse_search({"players": [{"id": "x/#?", "name": "Synthetic"}]})[0]
    assert player.source_url == "https://dash.batrace.top/player/x%2F%23%3F"
    assert parse_match({"matchInfo": raw_match()}, "x/#?").source_url.endswith("/match/x%2F%23%3F")


def test_time_is_aware_utc_and_missing_start_stays_unknown_with_observed_end():
    assert utc_datetime("2023-11-15T06:13:20+08:00") == utc_datetime(1700000000)
    match = raw_match()
    match.pop("StartTime")
    normalized = parse_match({"matchInfo": match}, "a")
    assert normalized.started_at is None
    assert normalized.ended_at == datetime.fromtimestamp(1700000600, timezone.utc)


def test_match_end_is_not_derived_and_new_field_preserves_positional_arguments():
    at = utc_datetime(1700000000)
    match = Match("a", "Map", "ranked", at, 600, "0", [], True, "source", at, "20")
    assert match.started_at == at and match.map_id == "20" and match.ended_at is None
    raw = raw_match()
    raw.pop("EndTime")
    parsed = parse_match({"matchInfo": raw}, "a")
    assert parsed.started_at == at and parsed.ended_at is None


@pytest.mark.parametrize("raw", ["2023-01-01T00:00:00", True, float("nan"), float("inf")])
def test_time_does_not_guess_timezone_or_accept_nonfinite_values(raw):
    with pytest.raises(ValueError):
        utc_datetime(raw)


def test_player_uses_newest_dated_observation_not_array_order():
    player = parse_player({
        "search": {"players": [{"id": "001", "name": "Synthetic", "rating": 900}]},
        "analysis": {"matchCount": 2, "trend": {"points": [
            {"ratingAfter": 1200, "endTime": 1700000600},
            {"ratingAfter": 1100, "endTime": 1700000000},
        ]}},
    }, "001")
    assert player.elo == 1200
    assert player.elo_observed_at == utc_datetime(1700000600)
    assert player.updated_at is None  # a match end is not a source update stamp


def test_search_snapshot_newer_than_trend_keeps_its_rating():
    player = parse_player({
        "search": {"players": [{"id": "001", "name": "Synthetic", "rating": 1300,
                                  "updated_at": "2023-11-15T00:00:00Z"}]},
        "analysis": {"trend": {"points": [{"ratingAfter": 1200, "endTime": 1700000000}]}},
    }, "001")
    assert player.elo == 1300
    assert player.elo_observed_at is None


def test_player_does_not_replace_requested_identity_or_invent_steam_mapping():
    with pytest.raises(ValueError, match="ID"):
        parse_player({"search": {"players": [{"id": "other", "name": "Same"}]},
                      "analysis": {"matchCount": 0}}, "001")


def test_protobuf_defaults_are_scoped_and_count_is_distinct_from_value():
    match = parse_match({"matchInfo": raw_match()}, "42")
    a, b = match.players
    assert (a.kills, a.losses, a.destroyed_value, a.lost_value) == (2, 1, 500, 100)
    assert a.team == "0"
    assert (b.kills, b.losses, b.destroyed_value, b.lost_value) == (0, 0, 0, 0)
    assert parse_match({"matchInfo": {"Data": {"a": {"Id": "001"}}}}, "empty").players[0].elo is None
    assert Participant("001", "Synthetic").kills is None  # no universal zero filling


def test_rating_change_never_becomes_verified_outcome_or_mode():
    match = parse_match({"matchInfo": raw_match()}, "42")
    assert match.winner_team is None
    assert match.mode == "unknown"
    assert [p.outcome for p in match.players] == ["unknown", "unknown"]
    assert [p.elo_delta for p in match.players] == [12, -12]


def test_match_detail_and_recent_have_different_completeness():
    match = raw_match()
    assert parse_match({"matchInfo": match}, "42").complete
    assert not parse_match({"matchInfo": match}, "42", detail=False).complete
    match["Data"]["a"].pop("UnitData")
    assert not parse_match({"matchInfo": match}, "42").complete


def test_recent_deduplicates_ids_sorts_observed_end_then_start_and_retains_unknown_time():
    rows = [
        {"matchId": "old", "data": raw_match(start=1700000000, end=1700000600)},
        {"matchId": "new", "data": raw_match(start=1700000500, end=1700001100)},
        {"matchId": "new", "data": raw_match(start=1700000500, end=1700001100)},
        {"matchId": "end-only", "data": {"EndTime": 1700001200, "TotalPlayTimeInSec": 1200, "Data": {}}},
        {"matchId": "start-only", "data": {"StartTime": 1700001000, "Data": {}}},
        {"matchId": "unknown", "data": {"Data": {}}},
    ]
    parsed = parse_recent({"matches": rows})
    assert [m.id for m in parsed] == ["end-only", "new", "start-only", "old", "unknown"]
    assert parsed[0].started_at is None and parsed[0].ended_at == utc_datetime(1700001200)
    assert parse_recent({"matches": []}) == []


@pytest.mark.parametrize("payload", [{}, {"players": None}, {"players": {}}, {"error": "synthetic"}])
def test_missing_response_array_is_not_a_successful_empty_search(payload):
    with pytest.raises(ValueError):
        parse_search(payload)


@pytest.mark.parametrize("mutate", [
    lambda m: m["Data"]["a"].update({"Id": 1.5}),
    lambda m: m["Data"]["a"].update({"Destruction": -1}),
    lambda m: m["Data"]["a"].update({"Losses": 1.5}),
    lambda m: m["Data"]["a"].update({"NewRating": float("nan")}),
    lambda m: m["Data"]["a"].update({"TeamId": None}),
    lambda m: m["Data"]["a"].update({"UnitData": []}),
    lambda m: m.update({"EndTime": 1600000000}),
    lambda m: m.update({"TotalPlayTimeInSec": -1}),
])
def test_structural_changes_and_invalid_numbers_are_rejected(mutate):
    payload = raw_match()
    mutate(payload)
    with pytest.raises(ValueError):
        parse_match({"matchInfo": payload}, "42")


def test_explicit_mismatching_match_id_is_rejected():
    with pytest.raises(ValueError, match="ID mismatch"):
        parse_match({"matchId": "other", "matchInfo": raw_match()}, "42")


def analysis_player(analysis):
    return parse_player({"search": {"players": [{"id": "001", "name": "Synthetic"}]},
                         "analysis": analysis}, "001")


def test_analysis_retains_only_normalized_core_fields_and_explicit_zeroes():
    player = analysis_player({
        "matchCount": 50,
        "trend": {"points": [{
            "matchId": 42, "won": False, "ratingBefore": 1000, "ratingAfter": 990,
            "kdRatio": 0, "dmr": 1.5, "destructionScore": 0, "lossesScore": 100,
            "objectivesCaptured": 0, "endTime": 1700000000,
        }, {"matchId": "0043"}]},
        "highlightUnits": [{"unitId": 7, "unitName": "Tank", "spawnCount": 0,
                            "totalDamage": 0, "avgRoi": 0}, {"unitId": "007"}],
        "categoryPreferences": [{"categoryKey": "tank", "percentage": 0}],
        "mapPerformance": [{"mapId": 20, "mapName": "Synthetic map", "matchCount": 5,
                            "winRate": 60}],
        "playStyle": {"ignored": True}, "dragon": 99,
    })
    analysis = player.analysis
    assert set(analysis) == {"matchCount", "trend", "highlightUnits", "categoryPreferences", "mapPerformance"}
    assert analysis["matchCount"] == 50
    point, missing = analysis["trend"]["points"]
    assert point == {"matchId": "42", "won": False, "ratingBefore": 1000,
                     "ratingAfter": 990, "kdRatio": 0, "dmr": 1.5, "destructionScore": 0,
                     "lossesScore": 100, "objectivesCaptured": 0, "endTime": utc_datetime(1700000000)}
    assert missing["matchId"] == "0043"
    assert all(value is None for key, value in missing.items() if key != "matchId")
    assert analysis["highlightUnits"] == [
        {"unitId": "7", "unitName": "Tank", "spawnCount": 0, "totalDamage": 0, "avgRoi": 0},
        {"unitId": "007", "unitName": None, "spawnCount": None, "totalDamage": None, "avgRoi": None},
    ]
    assert analysis["categoryPreferences"] == [{"categoryKey": "tank", "percentage": 0}]
    assert analysis["mapPerformance"] == [{"mapId": "20", "mapName": "Synthetic map",
                                          "matchCount": 5, "winRate": 60}]


@pytest.mark.parametrize("values,expected", [
    ([None], None), ([False], False), ([True], True),
    ([None, False, None], False), ([True, None, True], True),
    ([False, True], None), ([True, False, True], None),
])
def test_analysis_duplicate_match_ids_do_not_inflate_samples_or_hide_conflicts(values, expected):
    rows = [{"matchId": 42 if i % 2 else "42", "endTime": 1700000000 + i,
             **({"won": value} if value is not None else {})}
            for i, value in enumerate(values)]
    rows.extend([{"matchId": "042", "won": False}, {"won": True}])
    analysis = analysis_player({"trend": {"points": rows}}).analysis
    points = analysis["trend"]["points"]
    assert len(points) == 3
    assert points[0]["matchId"] == "42"
    assert points[0]["won"] is expected
    assert points[0]["endTime"] == utc_datetime(1700000000 + len(values) - 1)
    assert points[1]["matchId"] == "042"
    conflict = True in values and False in values
    assert points[0].get("wonConflict", False) is conflict
    expected_outcomes = {"042": "loss"}
    if conflict or expected is not None:
        expected_outcomes["42"] = "conflict" if conflict else ("win" if expected else "loss")
    assert analysis_outcomes(analysis) == expected_outcomes


@pytest.mark.parametrize("analysis", [
    {"matchCount": -1}, {"matchCount": True}, {"matchCount": 1.5},
    {"trend": []}, {"trend": {"points": None}}, {"trend": {"points": [None]}},
    {"trend": {"points": [{"matchId": True}]}},
    {"trend": {"points": [{"won": 0}]}}, {"trend": {"points": [{"won": "false"}]}},
    {"trend": {"points": [{"kdRatio": -1}]}}, {"trend": {"points": [{"dmr": float("inf")}]}},
    {"trend": {"points": [{"ratingBefore": "1000"}]}},
    {"trend": {"points": [{"objectivesCaptured": 0.5}]}},
    {"trend": {"points": [{"endTime": "2026-11-17"}]}},
    {"matchCount": 0, "highlightUnits": {}},
    {"matchCount": 0, "highlightUnits": [{"unitId": 1.5}]},
    {"matchCount": 0, "highlightUnits": [{"unitId": 1, "spawnCount": -1}]},
    {"matchCount": 0, "highlightUnits": [{"unitId": 1, "totalDamage": True}]},
    {"matchCount": 0, "highlightUnits": [{"unitId": 1, "avgRoi": float("nan")}]},
    {"matchCount": 0, "categoryPreferences": [{"categoryKey": "tank", "percentage": 101}]},
    {"matchCount": 0, "categoryPreferences": [{"categoryKey": "tank", "percentage": -1}]},
    {"matchCount": 0, "mapPerformance": [{"mapId": 1, "winRate": "50"}]},
    {"matchCount": 0, "mapPerformance": [{"mapId": 1, "matchCount": -1}]},
])
def test_analysis_rejects_invalid_shapes_coercions_and_nonfinite_statistics(analysis):
    with pytest.raises(ValueError):
        analysis_player(analysis)


def test_player_analysis_defaults_remain_compatible_and_independent():
    a, b = Player("001", "A"), Player("002", "B")
    a.analysis["trend"] = {"points": []}
    assert b.analysis == {}
    assert Match("a", "Map").map_id is None


def test_match_map_identity_and_extra_counters_preserve_absent_and_zero_values():
    raw = raw_match()
    raw["MapId"] = "020"
    raw["Data"]["a"].update(DamageDealt=1234.5, DamageReceived=0, ObjectivesCaptured=2,
                               TotalSpawnedUnitScore=2000, SupplyPointsConsumed=0)
    match = parse_match({"matchInfo": raw}, "42")
    assert match.map_id == "020" and match.map_name == "地图#020"
    a, b = match.players
    assert (a.damage_dealt, a.damage_received, a.objectives, a.spawned_value, a.supply) == (1234.5, 0, 2, 2000, 0)
    assert (b.damage_dealt, b.damage_received, b.objectives, b.spawned_value, b.supply) == (None,) * 5


@pytest.mark.parametrize("field,value", [
    ("DamageDealt", -1), ("DamageReceived", "0"), ("ObjectivesCaptured", 1.5),
    ("TotalSpawnedUnitScore", True), ("SupplyPointsConsumed", float("nan")),
])
def test_match_extra_counters_are_strict(field, value):
    raw = raw_match()
    raw["Data"]["a"][field] = value
    with pytest.raises(ValueError):
        parse_match({"matchInfo": raw}, "42")
