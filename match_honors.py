"""Personal honors and highlights from normalized BATrace match observations."""

from __future__ import annotations

from collections import defaultdict
from math import isfinite
from statistics import median

try:
    from .models import Match, Participant
    from .scoring import MODEL
except ImportError:  # Standalone tests use the repository as their import root.
    from models import Match, Participant
    from scoring import MODEL


def _observed(value) -> bool:
    return type(value) in (int, float) and isfinite(value) and value >= 0


def _leaders(values: dict, *, lowest: bool = False) -> list[str]:
    if not values:
        return []
    best = (min if lowest else max)(values.values())
    return [pid for pid, value in values.items() if value == best]


def _display(value: float, *, signed: bool = False) -> str:
    return format(value, "+," if signed else ",").removesuffix(".0")


def _unit_stats(player: Participant) -> dict:
    if not player.units_available and not player.units:
        return {}

    roles = defaultdict(list)
    returned_planes = []
    lifetimes = []
    for unit in player.units:
        role = MODEL["units"].get(str(unit.get("id")), (None,))[0]
        roles[role].append(unit)
        spawn, death = unit.get("spawn_time"), unit.get("death_time", 0)
        if unit.get("was_refunded") is True:
            if role == "jet" and death == 0:
                returned_planes.append(unit)
        elif _observed(spawn) and _observed(death) and 0 < spawn <= death:
            lifetimes.append(death - spawn)

    def aggregate(units, field, reduce=sum):
        # Omitted protobuf counters are zero. Explicit nulls are unknown.
        values = [unit.get(field, 0) for unit in units]
        return reduce(values) if values and all(map(_observed, values)) else None

    infantry = roles["inf"]
    drop_count = None
    if all(unit.get("was_airdropped", False) is not None for unit in infantry):
        drop_count = sum(unit.get("was_airdropped") is True for unit in infantry)
    return {
        "recon_spawns": len(roles["recon"]),
        "recon_kills": aggregate(roles["recon"], "killed_count"),
        "single_recon": aggregate(roles["recon"], "killed_count", max),
        "single_armor": aggregate(roles["armor"], "killed_count", max),
        "infantry_kills": aggregate(infantry, "killed_count"),
        "airdrop_spawns": drop_count,
        "aa_kills": aggregate(roles["aa"], "killed_count"),
        "single_returned_plane": aggregate(returned_planes, "killed_count", max),
        "single_unit": aggregate(player.units, "killed_count", max),
        "buildings": aggregate(player.units, "building_destroyed_count"),
        "lifetime": median(lifetimes) if len(lifetimes) >= 10 else None,
    }


def match_honors(
    match: Match, winner_team: str | None,
) -> tuple[dict[str, list[dict[str, str | bool]]], list[dict[str, str]]]:
    """Award honors to complete, balanced team 0/1 rosters, excluding spectators.

    The caller supplies a verified winner, or None. Shared maxima and minima
    qualify equally. Per-player display limits are applied after all rules.
    """
    if not match.complete:
        return {}, []
    players = {str(p.player_id): p for p in sorted(match.players, key=lambda p: str(p.player_id))
               if p.team in ("0", "1")}
    teams = {side: [pid for pid, p in players.items() if p.team == side] for side in ("0", "1")}
    if (not teams["0"] or len(teams["0"]) != len(teams["1"])
            or len(players) != sum(p.team in teams for p in match.players)):
        return {}, []

    honors: dict[str, list[dict[str, str | bool]]] = defaultdict(list)

    def award(pid, name, reason, *, positive=True):
        honors[pid].append({"name": name, "positive": positive, "reason": reason})

    def leader_award(values, name, minimum, reason, *, qualifies=lambda pid: True):
        for pid in _leaders(values):
            if values[pid] >= minimum and qualifies(pid):
                award(pid, name, reason(pid, values[pid]))

    def observed_values(getter):
        return {pid: value for pid in players if _observed(value := getter(pid))}

    def participant_values(field):
        return observed_values(lambda pid: getattr(players[pid], field))

    unit_stats = {pid: _unit_stats(p) for pid, p in players.items()}

    def unit_values(field):
        return observed_values(lambda pid: unit_stats[pid].get(field))

    destroyed = participant_values("destroyed_value")
    losses = participant_values("lost_value")
    caps = participant_values("objectives")
    net = {pid: value - losses[pid] for pid, value in destroyed.items() if pid in losses}
    single_recon = unit_values("single_recon")

    # Order within each priority group also determines display order.
    leader_award(unit_values("recon_spawns"), "侦察大队", 18,
                 lambda _, n: f"全场侦察出动最多，共 {n} 次")
    leader_award(unit_values("recon_kills"), "侦察尖刀", 9,
                 lambda pid, n: f"侦察击杀 {n} 个，单个侦察单位最高 {single_recon[pid]} 杀",
                 qualifies=lambda pid: single_recon.get(pid, 0) >= 3)
    leader_award(unit_values("single_armor"), "装甲王牌", 7,
                 lambda _, n: f"单个装甲单位击杀 {n} 个，全场最多")
    leader_award(unit_values("infantry_kills"), "步兵当家", 20,
                 lambda pid, n: f"步兵击杀 {n} 个，个人占点 {caps[pid]} 次",
                 qualifies=lambda pid: caps.get(pid, 0) >= 3)
    leader_award(unit_values("airdrop_spawns"), "伞兵先遣队", 4,
                 lambda pid, n: f"空降步兵 {n} 次，个人占点 {caps[pid]} 次",
                 qualifies=lambda pid: caps.get(pid, 0) >= 3)
    leader_award(unit_values("aa_kills"), "禁飞区", 12,
                 lambda _, n: f"防空单位击杀 {n} 个，全场最多")
    leader_award(unit_values("single_returned_plane"), "王牌投弹手", 5,
                 lambda _, n: f"单架次固定翼击杀 {n} 个并成功返航")
    leader_award(unit_values("single_unit"), "一人成军", 8,
                 lambda _, n: f"单个单位击杀 {n} 个，全场最多")
    leader_award(participant_values("supply_destroyed"), "断人粮道", 15000,
                 lambda _, n: f"摧毁敌方补给 {_display(n)} 点，全场最多")
    leader_award(participant_values("supply_consumed_by_allies"), "奶妈", 11896,
                 lambda _, n: f"队友消耗其补给 {_display(n)} 点，全场最多")

    for field, title, minimum, description in (
        ("supply_captured", "土匪", 9939, "缴获敌方补给"),
        ("supply_airdropped", "快递员", 8625, "空投补给"),
    ):
        for pid, value in participant_values(field).items():
            if value >= minimum:
                award(pid, title, f"{description} {_display(value)} 点")
    for pid, value in unit_values("buildings").items():
        if value >= 28:
            award(pid, "拆迁队", f"摧毁建筑 {value} 座")
    spawned = participant_values("spawned_value")
    for pid, value in participant_values("refunded_value").items():
        if 0 < spawned.get(pid, 0) and 100 * value >= 52 * spawned[pid]:
            award(pid, "七天无理由退货", f"退款占出动价值 {value / spawned[pid]:.1%}")

    cap_eligible: set[str] = set()
    cap_known: set[str] = {pid for pid, n in caps.items() if n < 3}
    for members in teams.values():
        if all(pid in caps for pid in members):
            cap_known.update(members)
            team_caps = {pid: caps[pid] for pid in members}
            total_caps = sum(team_caps.values())
            cap_eligible.update(pid for pid in _leaders(team_caps)
                                if caps[pid] >= 3 and 3 * caps[pid] >= total_caps)
        if all(pid in losses and pid in destroyed for pid in members):
            team_losses = {pid: losses[pid] for pid in members}
            total_losses = sum(team_losses.values())
            total_destroyed = sum(destroyed[pid] for pid in members)
            for pid in _leaders(team_losses, lowest=True):
                if (total_losses > 0 and 1000 * losses[pid] <= 109 * total_losses
                        and destroyed[pid] * len(members) >= total_destroyed):
                    award(pid, "保活能手",
                          f"摧毁分达到队均，损失占全队 {losses[pid] / total_losses:.1%}，队内最低")

    if winner_team in teams:
        winners = {pid: net[pid] for pid in teams[winner_team] if pid in net}
        for pid in _leaders(winners):
            if net[pid] > 0:
                award(pid, "大腿", f"净交换 {_display(net[pid], signed=True)}，胜方最高")
        for pid in _leaders(net):
            if players[pid].team != winner_team and net[pid] > 0:
                award(pid, "虽败犹荣", f"净交换 {_display(net[pid], signed=True)}，全场最高")
    for pid in players:
        if pid in cap_eligible:
            award(pid, "抢点先锋", f"占点 {caps[pid]} 次，队内最多且至少占全队三分之一")
    if len(destroyed) == len(players):
        total = sum(destroyed.values())
        for pid in _leaders(destroyed):
            if total > 0 and 1000 * destroyed[pid] >= 179 * total:
                award(pid, "收割机", f"摧毁分 {_display(destroyed[pid])}，占全场 {destroyed[pid] / total:.1%}")

    for pid in _leaders(net, lowest=True):
        if (net[pid] < 0 and 5 * destroyed[pid] < 4 * losses[pid]
                and pid in cap_known and pid not in cap_eligible):
            award(pid, "交换吃亏",
                  f"净交换 {_display(net[pid], signed=True)}，价值交换比 {destroyed[pid] / losses[pid]:.2f}",
                  positive=False)
    for pid, value in participant_values("supply_captured_by_enemy").items():
        if value >= 10053:
            award(pid, "运输大队长", f"被敌方缴获 {_display(value)} 点补给", positive=False)
    lifetimes = unit_values("lifetime")
    for pid in _leaders(lifetimes, lowest=True):
        if lifetimes[pid] <= 104:
            award(pid, "落地成盒", f"阵亡单位存活中位数 {_display(lifetimes[pid])} 秒，全场最短",
                  positive=False)

    highlights = []
    for values, label, signed, suffix in (
        (destroyed, "摧毁最多", False, ""),
        (net, "净交换最高", True, ""),
        (caps, "占点最多", False, " 次"),
    ):
        leaders = _leaders(values)
        if leaders and (best := values[leaders[0]]) > 0:
            highlights.append({
                "label": label,
                "value": _display(best, signed=signed) + suffix,
                "names": "、".join(players[pid].name or f"#{pid}" for pid in leaders),
            })
    return {pid: awards[:2] for pid, awards in honors.items()}, highlights
