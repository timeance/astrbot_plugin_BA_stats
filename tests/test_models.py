"""All payloads below are SYNTHETIC contract fixtures, not captured BATrace data."""

from datetime import datetime, timezone

import pytest

from models import (
    FetchResult, Match, Participant, Player, canonical_id, parse_match,
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


def test_time_is_aware_utc_and_recent_can_derive_start_from_end():
    assert utc_datetime("2023-11-15T06:13:20+08:00") == utc_datetime(1700000000)
    match = raw_match()
    match.pop("StartTime")
    normalized = parse_match({"matchInfo": match}, "a")
    assert normalized.started_at == datetime.fromtimestamp(1700000000, timezone.utc)


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


def test_recent_deduplicates_ids_sorts_actual_start_and_retains_unknown_time():
    rows = [
        {"matchId": "old", "data": raw_match(start=1700000000)},
        {"matchId": "new", "data": raw_match(start=1700000500)},
        {"matchId": "new", "data": raw_match(start=1700000500)},
        {"matchId": "unknown", "data": {"Data": {}}},
    ]
    assert [m.id for m in parse_recent({"matches": rows})] == ["new", "old", "unknown"]
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
