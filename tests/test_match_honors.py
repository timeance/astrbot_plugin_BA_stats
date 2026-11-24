"""Offline BATrace-shaped fixtures exercise the public honors engine."""

import pytest

from match_honors import match_honors
from models import parse_match


@pytest.fixture
def roster():
    return {
        str(pid): dict(Id=pid, Name=f"玩家{pid}", TeamId=pid // 5, UnitData={},
                       ObjectivesCaptured=0, DestructionScore=0, LossesScore=0)
        for pid in range(10)
    }


def match_for(roster, *, detail=True, **info):
    return parse_match({"matchInfo": {"EndTime": 1700000200, "Data": roster, **info}}, "123", detail=detail)


def units(row, unit_id, kills, **fields):
    for count in kills:
        row["UnitData"][str(len(row["UnitData"]))] = {"Id": unit_id, "KilledCount": count, **fields}


def titles(honors, pid="0"):
    return [honor["name"] for honor in honors.get(pid, [])]


def test_retains_source_counters_without_replacing_missing_values(roster):
    roster["0"].update(TotalRefundedUnitScore=0, SupplyDestroyed=15000,
                       SupplyPointsConsumedByAllies=11896, SupplyPointsConsumedFromAllies=1,
                       SupplyCaptured=9939, SupplyAirdropped=8625, SupplyCapturedByEnemy=10053)
    units(roster["0"], 57, [0], WasAirdropped=True, BuildingDestroyedCount=28)
    units(roster["0"], 57, [0], WasAirdropped=False, BuildingDestroyedCount=0)
    units(roster["0"], 57, [0])
    a, b, *_ = match_for(roster).players
    fields = ("refunded_value", "supply_destroyed", "supply_consumed_by_allies",
              "supply_captured", "supply_airdropped", "supply_captured_by_enemy")
    assert tuple(getattr(a, field) for field in fields) == (0, 15000, 11896, 9939, 8625, 10053)
    assert all(getattr(b, field) is None for field in fields)
    assert [(u.get("was_airdropped"), u.get("building_destroyed_count")) for u in a.units] == [
        (True, 28), (False, 0), (None, None),
    ]


@pytest.mark.parametrize("row_fields,unit_fields", [
    ({"SupplyDestroyed": -1}, {}),
    ({"TotalRefundedUnitScore": True}, {}),
    ({}, {"WasAirdropped": 1}),
    ({}, {"BuildingDestroyedCount": 1.5}),
])
def test_rejects_invalid_honor_counters(roster, row_fields, unit_fields):
    roster["0"].update(row_fields)
    units(roster["0"], 57, [0], **unit_fields)
    with pytest.raises(ValueError):
        match_for(roster)


@pytest.mark.parametrize("gap", ["listing", "missing_unit_data", "unequal", "no_formal_opponent"])
def test_requires_complete_balanced_formal_rosters(roster, gap):
    roster["0"]["ObjectivesCaptured"] = 4
    if gap == "missing_unit_data":
        roster["0"].pop("UnitData")
    elif gap == "unequal":
        roster.pop("9")
    elif gap == "no_formal_opponent":
        for pid in range(5, 10):
            roster[str(pid)]["TeamId"] = 2
    assert match_honors(match_for(roster, detail=gap != "listing"), "0") == ({}, [])


def test_highlights_share_ties_and_ignore_spectators_and_missing_values(roster):
    roster["0"].update(Name="", DestructionScore=9205, LossesScore=6725, ObjectivesCaptured=6)
    roster["5"].update(Name="并列玩家", DestructionScore=9205, LossesScore=6725, ObjectivesCaptured=6)
    roster["1"].update(DestructionScore=None, LossesScore=None, ObjectivesCaptured=None)
    roster["2"].update(DestructionScore=1000, LossesScore=None, ObjectivesCaptured=1)
    roster["10"] = dict(Id=10, TeamId=2, Name="观战", UnitData={}, DestructionScore=99999,
                        LossesScore=0, ObjectivesCaptured=99, SupplyCaptured=99999)
    honors, highlights = match_honors(match_for(roster), None)
    assert "收割机" not in titles(honors)
    assert "抢点先锋" not in titles(honors)
    assert "大腿" not in titles(honors)
    assert "10" not in honors
    assert highlights == [
        {"label": "摧毁最多", "value": "9,205", "names": "#0、并列玩家"},
        {"label": "净交换最高", "value": "+2,480", "names": "#0、并列玩家"},
        {"label": "占点最多", "value": "6 次", "names": "#0、并列玩家"},
    ]


def test_empty_or_nonpositive_highlights_are_omitted(roster):
    assert match_honors(match_for(roster), "0") == ({}, [])
    roster["0"].update(DestructionScore=2.5, LossesScore=5)
    assert match_honors(match_for(roster), "0")[1] == [
        {"label": "摧毁最多", "value": "2.5", "names": "玩家0"},
    ]


def test_recon_spawn_and_kill_titles_have_independent_leaders(roster):
    units(roster["0"], 21, [0] * 18)
    units(roster["1"], 21, [3, 2, 2, 2])
    units(roster["2"], 23, [3, 3, 3])
    units(roster["5"], 21, [0] * 17)
    honors, _ = match_honors(match_for(roster), None)
    assert titles(honors) == ["侦察大队"]
    assert titles(honors, "1") == titles(honors, "2") == ["侦察尖刀"]
    assert titles(honors, "5") == []

    units(roster["3"], 21, [2] * 5)
    assert "侦察尖刀" not in titles(match_honors(match_for(roster), None)[0], "1")


def test_armor_and_single_unit_use_instance_records_and_share_ties(roster):
    units(roster["0"], 13, [7])
    units(roster["1"], 13, [4, 4, 4])
    units(roster["5"], 13, [7])
    units(roster["6"], 4, [8])
    units(roster["7"], 55, [8])  # Unclassified units still count for the single-unit title.
    honors, _ = match_honors(match_for(roster), None)
    assert titles(honors) == titles(honors, "5") == ["装甲王牌"]
    assert titles(honors, "1") == []
    assert titles(honors, "6") == titles(honors, "7") == ["一人成军"]


def test_infantry_and_drop_titles_use_own_caps_and_exclusive_scoring_roles(roster):
    roster["0"]["ObjectivesCaptured"] = roster["5"]["ObjectivesCaptured"] = 3
    units(roster["0"], 57, [5] * 4)
    units(roster["5"], 57, [0] * 4, WasAirdropped=True)
    units(roster["6"], 69, [7] * 3, WasAirdropped=True)
    units(roster["6"], 21, [0] * 5, WasAirdropped=True)
    honors, _ = match_honors(match_for(roster), None)
    assert titles(honors) == ["步兵当家", "抢点先锋"]
    assert titles(honors, "5") == ["伞兵先遣队", "抢点先锋"]
    assert titles(honors, "6") == ["禁飞区"]

    roster["0"]["ObjectivesCaptured"] = 2
    roster["5"].pop("ObjectivesCaptured")
    honors, _ = match_honors(match_for(roster), None)
    assert "步兵当家" not in titles(honors)
    assert "伞兵先遣队" not in titles(honors, "5")


def test_aa_uses_total_kills_and_planes_require_one_returned_sortie(roster):
    units(roster["0"], 69, [6, 6])
    units(roster["1"], 69, [6, 5])
    units(roster["5"], 44, [5], WasRefunded=True)
    units(roster["6"], 44, [7], DeathTime=1700000190)
    units(roster["7"], 44, [4, 4], WasRefunded=True)
    units(roster["8"], 8, [7], WasRefunded=True)
    honors, _ = match_honors(match_for(roster), None)
    assert titles(honors) == ["禁飞区"]
    assert titles(honors, "5") == ["王牌投弹手"]
    assert all(titles(honors, pid) == [] for pid in ("1", "6", "7", "8"))


@pytest.mark.parametrize("field,value,title,leader_only,positive", [
    ("SupplyDestroyed", 15000, "断人粮道", True, True),
    ("SupplyPointsConsumedByAllies", 11896, "奶妈", True, True),
    ("SupplyCaptured", 9939, "土匪", False, True),
    ("SupplyAirdropped", 8625, "快递员", False, True),
    ("SupplyCapturedByEnemy", 10053, "运输大队长", False, False),
])
def test_supply_thresholds_and_leadership(roster, field, value, title, leader_only, positive):
    roster["0"][field] = roster["5"][field] = value
    roster["1"][field] = value - 1
    honors, _ = match_honors(match_for(roster), None)
    assert titles(honors) == titles(honors, "5") == [title]
    assert honors["0"][0]["positive"] is positive
    assert honors["0"][0]["reason"]
    assert titles(honors, "1") == titles(honors, "2") == []
    roster["5"][field] = value + 1
    assert (title in titles(match_honors(match_for(roster), None)[0])) is not leader_only


def test_buildings_and_refund_ratio_use_their_own_counters(roster):
    units(roster["0"], 4, [0, 0], BuildingDestroyedCount=14)
    units(roster["1"], 4, [0], BuildingDestroyedCount=27)
    roster["0"].update(TotalSpawnedUnitScore=100, TotalRefundedUnitScore=52)
    roster["1"].update(TotalSpawnedUnitScore=100, TotalRefundedUnitScore=51.9)
    roster["5"].update(TotalSpawnedUnitScore=0, TotalRefundedUnitScore=100)
    roster["6"]["TotalRefundedUnitScore"] = 100
    honors, _ = match_honors(match_for(roster), None)
    assert titles(honors) == ["拆迁队", "七天无理由退货"]
    assert all(titles(honors, pid) == [] for pid in ("1", "5", "6"))


def test_winner_and_losing_overall_net_leaders_share_titles(roster):
    for pid in ("0", "1", "5", "6"):
        roster[pid].update(DestructionScore=120, LossesScore=100)
    roster["7"].update(DestructionScore=199, LossesScore=190)
    honors, _ = match_honors(match_for(roster), "0")
    assert all("大腿" in titles(honors, pid) for pid in ("0", "1"))
    assert all("虽败犹荣" in titles(honors, pid) for pid in ("5", "6"))
    roster["0"]["DestructionScore"] += 1
    honors, _ = match_honors(match_for(roster), "0")
    assert not any("虽败犹荣" in titles(honors, pid) for pid in ("5", "6"))


@pytest.mark.parametrize("info,expected", [({"WinnerTeam": 0}, "0"), ({"WinnerTeam": 1}, "1"),
                                          ({"WinnerTeam": 2}, None), ({}, None)])
def test_formal_winner_is_retained_when_supplied(roster, info, expected):
    assert match_for(roster, **info).winner_team == expected


def test_capture_and_destruction_share_thresholds(roster):
    for pid, value in enumerate([179] + [100] * 8 + [21]):
        roster[str(pid)]["DestructionScore"] = value
    for pid in ("0", "1", "2"):
        roster[pid]["ObjectivesCaptured"] = 3
    roster["5"]["ObjectivesCaptured"] = 2
    honors, _ = match_honors(match_for(roster), None)
    assert titles(honors) == ["抢点先锋", "收割机"]
    assert titles(honors, "1") == titles(honors, "2") == ["抢点先锋"]
    assert titles(honors, "5") == []

    roster["3"]["ObjectivesCaptured"] = 1
    roster["9"]["DestructionScore"] += 1
    honors, _ = match_honors(match_for(roster), None)
    assert titles(honors) == titles(honors, "1") == titles(honors, "2") == []


def test_survival_requires_team_average_and_lowest_loss_share(roster):
    for pid, loss in enumerate([109, 200, 200, 200, 291]):
        roster[str(pid)].update(DestructionScore=100, LossesScore=loss)
        roster[str(pid + 5)]["DestructionScore"] = 100
    honors, _ = match_honors(match_for(roster), None)
    assert titles(honors) == ["保活能手"]
    roster["0"]["DestructionScore"] = 99
    assert "保活能手" not in titles(match_honors(match_for(roster), None)[0])
    roster["0"].update(DestructionScore=100, LossesScore=110)
    assert "保活能手" not in titles(match_honors(match_for(roster), None)[0])
    roster["0"]["LossesScore"] = 109
    roster["1"]["LossesScore"] = None
    assert "保活能手" not in titles(match_honors(match_for(roster), None)[0])


def test_worst_net_requires_low_value_kd_and_no_capture_title(roster):
    for row in roster.values():
        row.update(DestructionScore=60, LossesScore=60)
    roster["0"].update(DestructionScore=79, LossesScore=100, Destruction=10, Losses=1)
    roster["5"].update(DestructionScore=40, LossesScore=60)
    honors, _ = match_honors(match_for(roster), None)
    assert titles(honors) == ["交换吃亏"]
    assert honors["0"][0]["positive"] is False
    assert "交换吃亏" not in titles(honors, "5")

    roster["0"]["ObjectivesCaptured"] = 3
    honors, _ = match_honors(match_for(roster), None)
    assert titles(honors) == ["抢点先锋"]

    roster["0"].update(DestructionScore=80, ObjectivesCaptured=0)
    roster["5"]["DestructionScore"] += 1
    assert "交换吃亏" not in titles(match_honors(match_for(roster), None)[0])


def test_shortest_death_median_requires_ten_valid_non_refunded_lifetimes(roster):
    units(roster["0"], 57, [0] * 5, SpawnTime=100, DeathTime=200)
    units(roster["0"], 57, [0] * 5, SpawnTime=100, DeathTime=208)
    units(roster["5"], 57, [0] * 10, SpawnTime=100, DeathTime=204)
    units(roster["6"], 57, [0] * 9, SpawnTime=100, DeathTime=101)
    units(roster["6"], 57, [0], SpawnTime=100, DeathTime=101, WasRefunded=True)
    units(roster["6"], 57, [0], SpawnTime=100, DeathTime=99)
    units(roster["7"], 57, [0] * 10, SpawnTime=100, DeathTime=205)
    honors, _ = match_honors(match_for(roster), None)
    assert titles(honors) == titles(honors, "5") == ["落地成盒"]
    assert "104 秒" in honors["0"][0]["reason"]
    assert honors["0"][0]["positive"] is False
    assert titles(honors, "6") == titles(honors, "7") == []


def test_two_title_limit_prefers_specialized_then_general_then_negative(roster):
    roster["0"].update(DestructionScore=220, LossesScore=200, ObjectivesCaptured=3,
                       SupplyCapturedByEnemy=10053)
    units(roster["0"], 21, [0] * 18)
    honors, _ = match_honors(match_for(roster), "0")
    assert titles(honors) == ["侦察大队", "大腿"]
    assert all(honor["positive"] for honor in honors["0"])
