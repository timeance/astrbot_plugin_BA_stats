"""Synthetic analysis -> model -> service contract tests; no upstream requests."""

from copy import deepcopy
from datetime import datetime, timezone
import importlib
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest


NOW = datetime(2026, 11, 17, tzinfo=timezone.utc)


@pytest.fixture
def report(monkeypatch):
    name = "_ba_stats_report_contract"
    package = ModuleType(name)
    package.__path__ = [str(Path(__file__).resolve().parents[1])]
    monkeypatch.setitem(sys.modules, name, package)
    modules = SimpleNamespace(**{
        part: importlib.import_module(f"{name}.{part}")
        for part in ("service", "models", "commands", "settings", "storage", "client")
    })
    m, s = modules.models, modules.service
    result = lambda data: m.FetchResult(data, NOW)
    client = SimpleNamespace(namespace="batrace:synthetic", player=AsyncMock(),
                             recent=AsyncMock(), match=AsyncMock())
    storage = SimpleNamespace(get_binding=AsyncMock(return_value=None))
    service = s.StatsService(client, storage, modules.settings.Settings(cooldown_seconds=0))
    caller = s.Caller("test", "group:one", "user:one")

    def player(analysis):
        return m.parse_player({"search": {"players": [{"id": "001", "name": "Synthetic", "rating": 1000}]},
                               "analysis": analysis}, "001")

    async def run(player, matches, command="stats stb:001"):
        client.player.return_value = result(player)
        client.recent.return_value = result(matches)
        return (await service.execute(modules.commands.parse_command(command), caller)).view

    try:
        yield SimpleNamespace(m=m, s=s, modules=modules, client=client, storage=storage,
                              service=service, caller=caller, result=result, player=player, run=run)
    finally:
        service.close()
        for key in tuple(sys.modules):
            if key.startswith(name + "."):
                sys.modules.pop(key, None)


def recent(report, mid, *, destroyed=100, lost=25, map_id=120, at=NOW):
    return report.m.Match(mid, map_name=f"地图#{map_id}", map_id=str(map_id), started_at=at,
                          players=[report.m.Participant("001", "Synthetic", team="0", elo_delta=10,
                                                        destroyed_value=destroyed, lost_value=lost),
                                   report.m.Participant("999", "Other", team="1", destroyed_value=9999,
                                                        lost_value=1)])


def metrics(view):
    return {metric["label"]: metric for metric in view["metrics"]}


async def test_overview_uses_distinct_results_and_pairs_damage_in_the_recent_window(report):
    player = report.player({"matchCount": 80, "trend": {"points": [
        {"matchId": "win", "won": True, "dmr": 2.5, "endTime": "2026-11-16T00:00:00Z"},
        {"matchId": "loss", "won": False, "dmr": 9, "endTime": "2026-11-15T00:00:00Z"},
        {"matchId": "unknown", "endTime": "2026-11-14T00:00:00Z"},
        {"matchId": "analysis-only", "won": True},
        {"won": False},
    ]}})
    matches = [recent(report, "win"), recent(report, "loss", destroyed=200, lost=75),
               recent(report, "unmatched", destroyed=None, lost=999)]
    for match, (dealt, received) in zip(matches, [(100, 20), (20, 40), (990, None)]):
        match.players[0].damage_dealt, match.players[0].damage_received = dealt, received
    original_player, original_matches = deepcopy(player), deepcopy(matches)
    view = await report.run(player, matches)
    data = metrics(view)
    assert data["ELO"]["value"] == 1000
    assert data["近期分值 K/D"]["value"] == 3  # (100 + 200) / (25 + 75)
    assert data["分析样本胜率"]["value"] == pytest.approx(200 / 3)
    dims = {axis["key"]: axis["value"] for axis in view["profile_rating"]["dimensions"]}
    assert dims["win_rate"] == 50  # Only the win and loss are in the recent sample.
    assert dims["outcome"] is None  # Team ELO is missing, but the actual outcomes are known.
    assert data["近期伤害交换比"]["value"] == 2  # (100 + 20) / (20 + 40), not the last match's dmr.
    assert data["近期伤害交换比"]["sample_size"] == 2
    assert data["分析样本胜率"]["sample_size"] == 3
    assert view["profile_rating"]["win_rate_sample"] == {"wins": 1, "losses": 1, "sample_size": 2, "value": 50}
    assert view["style_scope"] == "近80场风格"
    assert view["scope"] == "近期3场 · 11-17—11-17"
    assert [row["outcome"] for row in view["rows"]] == ["win", "loss", "unknown"]
    assert player == original_player and matches == original_matches
    assert not view.get("notes")


@pytest.mark.parametrize("won,expected,percentage", [(None, "unknown", None), (False, "loss", 0), (True, "win", 100)])
async def test_won_requires_boolean_and_an_exact_target_match_id(report, won, expected, percentage):
    point = {"matchId": "match"}
    if won is not None:
        point["won"] = won
    player = report.player({"trend": {"points": [point]}})
    matches = [recent(report, "match"), recent(report, "other"), recent(report, "0match")]
    view = await report.run(player, matches)
    assert [row["outcome"] for row in view["rows"]] == [expected, "unknown", "unknown"]
    assert view["rows"][0]["outcome_source"] == ("analysis" if won is not None else None)
    assert metrics(view)["分析样本胜率"]["value"] == percentage
    assert view["profile_rating"]["dimensions"][-1]["value"] == percentage


async def test_duplicate_results_conflict_stays_unknown_and_does_not_inflate_win_rate(report):
    player = report.player({"matchCount": 99, "trend": {"points": [
        {"matchId": 42, "won": True}, {"matchId": "42", "won": False},
        {"matchId": "42", "won": True}, {"matchId": "ok", "won": False},
        {"matchId": "ok"}, {"matchId": "missing"},
    ]}})
    view = await report.run(player, [recent(report, "42"), recent(report, "ok")])
    assert [row["outcome"] for row in view["rows"]] == ["unknown", "loss"]
    assert metrics(view)["分析样本胜率"]["value"] == 0
    assert view["profile_rating"]["dimensions"][-1]["value"] == 0


async def test_overview_units_maps_and_categories_use_analysis_and_preserve_same_name_ids(report):
    player = report.player({"matchCount": 7,
        "highlightUnits": [
            {"unitId": "a", "unitName": "Same", "spawnCount": 5, "totalDamage": 100, "avgRoi": 0},
            {"unitId": "b", "unitName": "Same", "spawnCount": 3},
            {"unitId": "c"}, {"unitId": "d", "unitName": "Fourth"},
        ],
        "categoryPreferences": [{"categoryKey": "support", "percentage": 0},
                                {"categoryKey": "infantry", "percentage": 75}],
        "mapPerformance": [{"mapId": 120 + i, "mapName": f"Map {i}", "matchCount": i,
                            "winRate": 0 if i == 0 else 50} for i in range(6)],
    })
    match = recent(report, "x")
    match.players[0].units = [{"id": "not-analysis", "name": "Not an analysis unit", "count": 999}]
    view = await report.run(player, [match])
    assert view["common_units"] == [
        {"id": "a", "name": "Same（ID：a）", "count": 5, "damage": 100, "roi": 0},
        {"id": "b", "name": "Same（ID：b）", "count": 3, "damage": None, "roi": None},
        {"id": "c", "name": "单位#c", "count": None, "damage": None, "roi": None},
    ]
    assert view["categories"] == [{"name": "支援", "percentage": 0}, {"name": "步兵", "percentage": 75}]
    assert len(view["map_stats"]) == 5
    assert view["map_stats"][0] == {"name": "Map 0", "count": 0, "win_rate": 0}
    assert view["rows"][0]["map_name"] == "Map 0"


async def test_analysis_empty_lists_remain_empty_without_inventing_statistics(report):
    player = report.player({"matchCount": 0})
    match = recent(report, "x", destroyed=None, lost=None)
    match.players[0].units = [{"id": "u", "name": "Recent only", "count": 5}]
    view = await report.run(player, [match])
    assert view["common_units"] == view["map_stats"] == view["categories"] == []
    assert metrics(view)["近期分值 K/D"]["value"] is None
    assert metrics(view)["分析样本胜率"]["value"] is None
    assert "单局伤害交换比" not in metrics(view)
    assert "近期伤害交换比" not in metrics(view)
    assert view["style_scope"] == "近0场风格"
    assert view["rows"][0]["map_name"] == "地图#120"


@pytest.mark.parametrize(("map_id", "upstream", "expected"), [
    (28, "#28", "煤山"), (25, "Coal Mountain", "卡达加军事基地"),
    (120, "Snow Castle", "纳尔瓦冰雪城堡"), (121, "#121", "地图#121"),
])
async def test_game_map_names_across_reports(report, map_id, upstream, expected):
    player = report.player({"matchCount": 1, "mapPerformance": [{"mapId": map_id, "mapName": upstream}]})
    match = recent(report, "match", map_id=map_id)
    view = await report.run(player, [match])
    assert view["map_stats"][0]["name"] == view["rows"][0]["map_name"] == expected
    assert report.s.match_view(report.result(match), player)["match"]["map_name"] == expected
    if map_id in (28, 25):
        assert report.s.match_view(report.result(match))["match"]["map_name"] == expected


def test_match_target_result_and_map_do_not_imply_team_or_opponent_results(report):
    player = report.player({"trend": {"points": [{"matchId": "42", "won": False}]},
                            "mapPerformance": [{"mapId": 120, "mapName": "Known map"}]})
    match = recent(report, "42")
    match.players.append(report.m.Participant("002", "Ally", team="0", elo_delta=10))
    before = deepcopy(match)
    view = report.s.match_view(report.result(match), player)
    assert view["match"]["map_name"] == "Known map"
    assert view["match"]["winner"] is None
    assert view["match"]["target_outcome"] == "loss"
    assert view["match"]["target_outcome_source"] == "analysis"
    assert [team["outcome"] for team in view["teams"]] == ["unknown", "unknown"]
    people = {p["id"]: p for team in view["teams"] for p in team["players"]}
    assert people["001"]["outcome"] == "loss"
    assert people["001"]["outcome_source"] == "analysis"
    assert people["999"]["outcome"] == people["002"]["outcome"] == "unknown"
    assert match == before
    direct = report.s.match_view(report.result(match))
    assert direct["match"]["target_outcome"] == "unknown"
    assert direct["match"]["target_outcome_source"] is None
    empty = report.s.match_view(report.result(report.m.Match("42")), player)
    assert empty["match"]["target_outcome"] == "unknown"


def test_match_team_metrics_sum_known_values_show_coverage_and_leave_missing_out(report):
    m = report.m
    match = m.Match("42", players=[
        m.Participant("001", "A", team="0", elo=1000, kills=2, losses=0, destroyed_value=100,
                      lost_value=0, damage_dealt=500, damage_received=0, objectives=0,
                      spawned_value=1000, supply=10),
        m.Participant("002", "B", team="0", elo=1200, kills=3, losses=1, destroyed_value=250,
                      lost_value=100),
        m.Participant("003", "C", team="1"),
    ])
    view = report.s.match_view(report.result(match))
    first = {item["label"]: item for item in view["teams"][0]["metrics"]}
    assert {key: item["value"] for key, item in first.items()} == {
        "摧毁分": 350, "损失分": 100, "赛后平均 ELO": 1100, "击杀": 5, "阵亡": 1, "伤害": 500,
        "出动价值": 1000, "占点": 0, "补给": 10,
    }
    assert first["伤害"]["note"] == "1/2 人有数据"
    assert first["出动价值"]["note"] == first["占点"]["note"] == first["补给"]["note"] == "1/2 人有数据"
    assert view["teams"][1]["metrics"] == []
    a, b = view["teams"][0]["players"]
    assert a["damage_exchange"] == "无承伤"
    assert (a["damage_dealt"], a["damage_received"], a["objectives"], a["spawned_value"], a["supply"]) == (500, 0, 0, 1000, 10)
    assert (b["damage_dealt"], b["damage_received"], b["objectives"], b["spawned_value"], b["supply"]) == (None,) * 5


async def test_analysis_cannot_bypass_binding_namespace_or_roster_identity_checks(report):
    binding = report.modules.storage.Binding("batrace:other", "001", "Synthetic", "")
    report.storage.get_binding.return_value = binding
    with pytest.raises(ValueError, match="其他数据源"):
        await report.service.execute(report.modules.commands.parse_command("me"), report.caller)
    report.client.player.assert_not_awaited()
    report.client.recent.assert_not_awaited()
    player = report.player({"trend": {"points": [{"matchId": "foreign", "won": True}]}})
    with pytest.raises(report.modules.client.IdentityMismatchError, match="未参与"):
        await report.run(player, [report.m.Match("foreign", players=[report.m.Participant("999", "Other")])])


async def test_last_applies_only_matching_analysis_and_retains_known_binding_namespace(report):
    report.storage.get_binding.return_value = report.modules.storage.Binding(report.client.namespace, "001", "Old name", "")
    player = report.player({"trend": {"points": [{"matchId": "42", "won": True}]}})
    report.client.match.return_value = report.result(recent(report, "42"))
    view = await report.run(player, [recent(report, "42")], "last")
    assert view["match"]["target_outcome"] == "win"
    report.client.player.assert_awaited_once_with("001")
    report.client.match.assert_awaited_once_with("42")


def scoring_report_match(report):
    # Deliberately put spectator and B before A in the upstream participant map.
    return report.m.parse_match({"matchInfo": {
        "StartTime": 1700000000, "EndTime": 1700001200, "TotalPlayTimeInSec": 1200,
        "Data": {
            "observer": {"Id": "observer", "Name": "Observer", "TeamId": 4},
            "b": {"Id": "002", "Name": "B player", "TeamId": 1,
                  "OldRating": 2100, "NewRating": 2090, "DestructionScore": 1000,
                  "LossesScore": 2000, "UnitData": {"u": {"Id": 57}}},
            "a": {"Id": "001", "Name": "A player", "OldRating": 2000,
                  "NewRating": 2010, "DestructionScore": 2000, "LossesScore": 1000,
                  "UnitData": {"u": {"Id": 13}}},
        }}}, "score-fixture")


async def test_recent_scores_flow_to_html_and_text_without_changing_result_authority(report):
    from render import Renderer

    player = report.player({"matchCount": 1})
    view = await report.run(player, [scoring_report_match(report)], "recent stb:001")
    row = view["rows"][0]
    assert row["score"] == 9.3 and row["score_band"] == "high"
    assert row["outcome"] == "unknown"  # ELO-derived outcome is internal to the model.
    renderer = Renderer()
    assert 'class="score high"' in renderer.template(view)
    assert "评分 9.3" in renderer.text(view)
    view["rows"] = report.s.recent_rows([recent(report, "missing")], player.id, player)
    assert view["rows"][0]["score"] is None
    assert "评分 暂无" in renderer.text(view)


async def test_overview_renders_aggregate_and_separate_observed_win_rate(report):
    from render import Renderer

    player = report.player({"trend": {"points": [{"matchId": "score-fixture", "won": True}]}})
    match = scoring_report_match(report)
    for participant in match.players:
        participant.objectives = 0
    view = await report.run(player, [match])
    rating = view["profile_rating"]
    # Pinned computeDragonScore gives 7.9 for the profile and 9.3 for the single match.
    assert (rating["score"], rating["title"]) == (7.9, "有龙样")
    assert [axis["value"] for axis in rating["dimensions"]] == [91, 53, 53, 92, 100]
    for output in (Renderer().template(view), Renderer().text(view)):
        assert all(term in output for term in ("7.9", "有龙样", "赛果超预期", "胜率"))
    report.client.match.assert_not_awaited()


def test_match_prediction_composition_and_a_b_order_reach_both_renderers(report):
    from render import Renderer

    match = scoring_report_match(report)
    before = deepcopy(match)
    view = report.s.match_view(report.result(match))
    assert [team["name"] for team in view["teams"]] == ["A 队", "B 队", "分队 4"]
    assert view["prediction"]["a"] == pytest.approx(35.9935000197115)
    assert view["prediction"]["b"] == pytest.approx(64.0064999802885)
    assert view["teams"][0]["composition"] == [{"name": "装甲", "percentage": 100}]
    assert view["teams"][1]["composition"] == [{"name": "步兵", "percentage": 100}]
    assert view["teams"][0]["composition_note"] == view["teams"][1]["composition_note"] == ""
    assert view["teams"][2]["composition"] == []
    assert "1 人缺单位数据" in view["teams"][2]["composition_note"]
    renderer = Renderer()
    for output in (renderer.template(view), renderer.text(view)):
        assert "36%" in output and "64%" in output
        assert "装甲" in output and "步兵" in output
        assert output.index("A 队") < output.index("B 队")
    assert match == before


@pytest.mark.parametrize("a_before", [1600, None])
def test_match_rating_times_reach_summary_from_real_payload(report, a_before):
    from render import Renderer

    match = report.m.parse_match({"matchInfo": {
        "StartTime": 1700000000, "EndTime": 1700001200, "TotalPlayTimeInSec": 1200,
        "Data": {
            "a": {"Id": "001", "Name": "A player", "TeamId": 0, "OldRating": a_before, "NewRating": 1620},
            "b": {"Id": "002", "Name": "B player", "TeamId": 1, "OldRating": 1600, "NewRating": 1580},
        }}}, "rating-times")
    view = report.s.match_view(report.result(match))
    assert view["teams"][0]["players"][0]["elo_before"] == a_before
    assert view["teams"][1]["players"][0]["elo_before"] == 1600
    assert [team["outcome"] for team in view["teams"]] == ["unknown", "unknown"]
    assert view["prediction"] == ({"a": 50, "b": 50} if a_before is not None else None)
    renderer = Renderer()
    html, text = renderer.template(view), renderer.text(view)
    summary = html.split('<div class="team-summaries">', 1)[1].split('<div class="match-columns">', 1)[0]
    assert summary.count('<dt>赛前平均 ELO</dt><dd>1600</dd>') == (2 if a_before is not None else 1)
    assert ('<dt>赛前平均 ELO</dt><dd>暂无</dd>' in summary) is (a_before is None)
    for rating in (1620, 1580):
        assert f'<dt>赛后平均 ELO</dt><dd>{rating}</dd>' in summary
    assert "赛后平均 ELO：1620" in text and "赛后平均 ELO：1580" in text
    assert "当前平均 ELO" not in html + text


def test_match_unknown_units_and_missing_ratings_stay_unknown_in_service(report):
    match = scoring_report_match(report)
    me = next(p for p in match.players if p.player_id == "001")
    me.units.append({"id": "999999999", "count": 1})
    me.elo_before = me.elo_delta = None
    view = report.s.match_view(report.result(match))
    assert view["prediction"] is None
    assert view["teams"][0]["composition"] == []
    assert view["teams"][0]["composition_coverage"] == 0.5
    assert "1 次出场单位无法定价" in view["teams"][0]["composition_note"]


def test_match_unknown_team_keeps_priced_units_and_display_label_separate(report):
    match = scoring_report_match(report)
    next(p for p in match.players if p.player_id == "001").team = None
    before = deepcopy(match)
    view = report.s.match_view(report.result(match))
    team = next(t for t in view["teams"] if t["name"] == "分队 未知")
    assert team["composition"] == [{"name": "装甲", "percentage": 100}]
    assert team["composition_coverage"] == 1
    assert team["composition_note"] == ""
    assert [p["id"] for p in team["players"]] == ["001"]
    assert match == before


@pytest.mark.parametrize("command", ["stats stb:001", "recent stb:001"])
async def test_report_start_and_scope_never_use_end_minus_duration(report, command):
    from render import Renderer

    player = report.player({"matchCount": 2})
    matches = report.m.parse_recent({"matches": [
        {"matchId": str(i), "data": {"EndTime": 1700001200 + i, "TotalPlayTimeInSec": 1200,
                                     "Data": {"a": {"Id": "001", "UnitData": {}}}}}
        for i in range(2)
    ]})
    view = await report.run(player, matches, command)
    assert [row["id"] for row in view["rows"]] == ["1", "0"]
    assert all(row["started_at"] is None and row["ended_at"] is not None for row in view["rows"])
    assert view["scope"] == "近期2场"
    detail = report.s.match_view(report.result(matches[0]), player)
    assert detail["match"]["started_at"] is None
    assert detail["match"]["ended_at"] == report.m.utc_datetime(1700001201)
    renderer = Renderer()
    for output in (renderer.template(view), renderer.text(view), renderer.template(detail), renderer.text(detail)):
        assert "2023-11-15" not in output  # End/duration must not fabricate a displayed start date.
    mixed = await report.run(player, [recent(report, "observed-start"), *matches], command)
    assert mixed["scope"] == "近期3场 · 11-17—11-17（1/3 场有开始时间）"
