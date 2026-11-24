"""平台无关的查询、身份选择和战报组装。生产路径从不读取演示数据。"""
from __future__ import annotations

import asyncio
from collections import Counter, OrderedDict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import itertools
import time

from .client import participant_for
from .commands import Request, Target
from .match_honors import match_honors
from .models import FetchResult, Match, Participant, Player, analysis_outcomes, win_rate_sample
from .pending import PendingManager
from .scoring import match_prediction, match_score, match_score_band, profile_rating, recent_score, score_band, team_composition
from .storage import Binding


CATEGORY_NAMES = {"recon": "侦察", "infantry": "步兵", "vehicles": "载具", "support": "支援",
                  "logistic": "后勤", "helicopters": "直升机", "aircrafts": "固定翼"}


@dataclass(frozen=True)
class Caller:
    platform: str
    session: str
    user: str

    @property
    def scope(self):
        return self.platform, self.session, self.user


@dataclass
class Response:
    text: str = ""
    view: dict | None = None
    # Keep commands separate from player-provided text; the adapter adds wake/At.
    instructions: tuple[tuple[str, str], ...] = ()


def ratio(numerator, denominator, zero_label="无损失"):
    if numerator is None or denominator is None:
        return None
    if denominator == 0:
        return zero_label if numerator > 0 else None
    return numerator / denominator


def freshness(*results):
    return {
        "fetched_at": min(r.fetched_at for r in results),
        "stale": any(r.stale for r in results),
        "source_updated_at": min((r.source_updated_at for r in results if r.source_updated_at), default=None),
    }


def participant_view(p: Participant):
    return dict(id=p.player_id, name=p.name, elo=p.elo, elo_before=p.elo_before, elo_delta=p.elo_delta,
                kills=p.kills, losses=p.losses, destroyed_value=p.destroyed_value,
                lost_value=p.lost_value, unit_exchange=ratio(p.kills, p.losses),
                value_exchange=ratio(p.destroyed_value, p.lost_value),
                damage_dealt=p.damage_dealt, damage_received=p.damage_received,
                damage_exchange=ratio(p.damage_dealt, p.damage_received, "无承伤"),
                objectives=p.objectives, spawned_value=p.spawned_value, supply=p.supply,
                outcome=p.outcome, outcome_source=None)


def outcome_view(result):
    known = result in ("win", "loss")
    return {"outcome": result if known else "unknown",
            "outcome_source": "analysis" if known else None,
            "outcome_conflict": result == "conflict"}


def map_name(match, player=None):
    if player is not None and match.map_id is not None:
        for row in player.analysis.get("mapPerformance", []):
            if row["mapId"] == match.map_id and row["mapName"]:
                return row["mapName"]
    return match.map_name


def recent_rows(matches, player_id, player=None, *, with_scores=True):
    rows = []
    analysis = player.analysis if player is not None and player.id == player_id else None
    outcomes = analysis_outcomes(analysis)
    for match in matches:
        person = participant_for(match, player_id)
        if person is None:
            continue
        row = participant_view(person)
        result = outcomes.get(match.id)
        if with_scores:
            score = recent_score(match, person, analysis)
            row.update(score=score, score_band=score_band(score),
                       score_outcome_source=("analysis" if result is not None else "elo_delta")
                       if score is not None else None)
        row.update(id=match.id, started_at=match.started_at, ended_at=match.ended_at,
                   map_name=map_name(match, player),
                   mode=match.mode, duration_seconds=match.duration_seconds,
                   source_url=match.source_url, **outcome_view(result))
        rows.append(row)
    return rows


def team_metrics(players):
    metrics = []
    for field, label, style in (
        ("destroyed_value", "摧毁分", "decimal"), ("lost_value", "损失分", "decimal"),
        ("elo", "赛后平均 ELO", "decimal"), ("kills", "击杀", "integer"),
        ("losses", "阵亡", "integer"), ("damage_dealt", "伤害", "decimal"),
        ("spawned_value", "出动价值", "decimal"), ("objectives", "占点", "integer"),
        ("supply", "补给", "decimal"),
    ):
        values = [p[field] for p in players if p[field] is not None]
        if not values:
            continue
        value = sum(values) / len(values) if field == "elo" else sum(values)
        complete = len(values) == len(players)
        metric = {"key": field, "label": label, "value": value, "format": style,
                  "observed_count": len(values), "participant_count": len(players),
                  "complete": complete}
        if not complete:
            metric["note"] = f"{len(values)}/{len(players)} 人有数据"
        metrics.append(metric)
    return metrics


def match_view(result, player=None, *, recent_match=None):
    match = result.data
    target = participant_for(match, player.id) if player is not None else None
    target_result = outcome_view(analysis_outcomes(player.analysis).get(match.id) if target is not None else None)
    target_damage = target_damage_source = None
    if target is not None:
        if target.damage_dealt is not None and target.damage_received is not None:
            target_damage = ratio(target.damage_dealt, target.damage_received, "无承伤")
            target_damage_source = "match" if target_damage is not None else None
        else:
            target_damage = next((point["dmr"] for point in player.analysis.get("trend", {}).get("points", [])
                                  if point.get("matchId") == match.id and point.get("dmr") is not None), None)
            target_damage_source = "analysis" if target_damage is not None else None
    teams = OrderedDict()
    winner_team = match.winner_team if match.winner_team in ("0", "1") else None
    for person in match.players:
        key = person.team
        if key not in teams:
            teams[key] = {"id": key,
                          "name": {"0": "A 队", "1": "B 队"}.get(key, f"分队 {key if key is not None else '未知'}"),
                          "outcome": "unknown", "players": []}
        row = participant_view(person)
        if person is target:
            row.update(target_result, damage_exchange=target_damage)
        teams[key]["players"].append(row)
        if winner_team is not None and key in ("0", "1"):
            teams[key]["outcome"] = "win" if key == winner_team else "loss"
    # Stable A/B ordering is independent of the source participant-map order.
    teams = OrderedDict(sorted(teams.items(), key=lambda pair: {"0": 0, "1": 1}.get(pair[0], 2)))
    for key, team in teams.items():
        team["metrics"] = team_metrics(team["players"])
        members = [p for p in match.players if p.team == key]
        if all(p.spawned_value is not None and p.refunded_value is not None
               and p.spawned_value >= p.refunded_value for p in members):
            team["metrics"].append({"label": "出兵净花费", "value": sum(p.spawned_value - p.refunded_value for p in members)})
        if match.complete and all(p.units_available for p in members):
            units = [unit for p in members for unit in p.units]
            team["metrics"].append({"label": "出兵数量", "value": len(units), "format": "integer"})
            if units and all(unit.get("death_time") is not None for unit in units):
                team["metrics"].append({"label": "单位存活率", "format": "percent", "unit": "%",
                                        "value": 100 * sum(unit["death_time"] == 0 for unit in units) / len(units)})
        composition = team_composition(match, key)
        team["composition"] = composition["categories"]
        team["composition_coverage"] = composition["coverage"]
        notes = []
        if composition["unknown_count"]:
            notes.append(f"{composition['unknown_count']} 次出场单位无法定价，构成暂无")
        if composition["missing_participants"]:
            notes.append(f"{composition['missing_participants']} 人缺单位数据，构成暂无")
        team["composition_note"] = "；".join(notes)
    coverage = {}
    coverage_labels = []
    if recent_match is not None and recent_match.id == match.id:
        for team_id, label in (("0", "A"), ("1", "B")):
            known = {p.player_id for p in recent_match.players if p.team == team_id}
            if not known:
                continue
            reported = {p["id"] for p in teams.get(team_id, {}).get("players", [])}
            matched = len(known & reported)
            coverage[team_id] = {"matched": matched, "known": len(known)}
            coverage_labels.append(f"{label} {matched}/{len(known)}")
    # The recent roster can prove a gap but cannot prove completeness.
    roster_incomplete = any(c["matched"] < c["known"] for c in coverage.values())
    prediction = None if roster_incomplete else match_prediction(match)
    honors, highlights = ({}, []) if roster_incomplete else match_honors(match, winner_team)
    participants = {p.player_id: p for p in match.players}
    for team in teams.values():
        for row in team["players"]:
            score = match_score(match, participants[row["id"]]) if match.complete and prediction else None
            row.update(score=score, score_band=match_score_band(score), honors=honors.get(row["id"], []))
    view = {"kind": "match", "match": {
        "id": match.id, "map_name": map_name(match, player), "mode": match.mode,
        "started_at": match.started_at, "ended_at": match.ended_at, "duration_seconds": match.duration_seconds,
        "winner": {"0": "A 队", "1": "B 队"}.get(winner_team),
        "target_damage_exchange": target_damage, "target_damage_exchange_source": target_damage_source,
        **{f"target_{key}": value for key, value in target_result.items()},
    }, "teams": list(teams.values()), "prediction": prediction, "highlights": highlights,
        "comparison_team_ids": ["0", "1"] if "0" in teams and "1" in teams else [],
        "scope": f"单局 · 实际收录 {len(match.players)} 名玩家",
        "source_url": match.source_url, **freshness(result)}
    if roster_incomplete:
        view["match"].update(roster_complete=False, roster_coverage=coverage)
        view["scope"] += " · 同场已知名单覆盖：" + "、".join(coverage_labels)
    if player:
        view["player"] = asdict(player)
    if not match.complete:
        view["notes"] = ["本局数据可能尚未完整。"]
    return view


def common_units(player, matches):
    if player.analysis:
        units = [{"id": u["unitId"], "name": u["unitName"] or f"单位#{u['unitId']}",
                  "count": u["spawnCount"], "damage": u["totalDamage"], "roi": u["avgRoi"]}
                 for u in player.analysis.get("highlightUnits", [])]
    else:
        # Older callers can still construct a Player without analysis data.
        counts, names = Counter(), {}
        for match in matches:
            person = participant_for(match, player.id)
            if person is not None:
                for unit in person.units:
                    uid = unit["id"]
                    counts[uid] += unit["count"]
                    if unit.get("name"):
                        names.setdefault(uid, unit["name"])
        units = [{"id": uid, "name": names.get(uid, f"单位#{uid}"), "count": count}
                 for uid, count in counts.most_common()]
    duplicates = Counter(unit["name"] for unit in units)
    for unit in units:
        if duplicates[unit["name"]] > 1:
            unit["name"] += f"（ID：{unit['id']}）"
    return units[:3]


def overview_metrics(player, rows):
    metrics = [
        {"label": "ELO", "value": player.elo},
    ]
    for label, numerator, denominator, zero_label in (
        ("近期分值 K/D", "destroyed_value", "lost_value", "无损失"),
        ("近期伤害交换比", "damage_dealt", "damage_received", "无承伤"),
    ):
        pairs = [(row[numerator], row[denominator]) for row in rows
                 if row[numerator] is not None and row[denominator] is not None]
        if not pairs and numerator == "damage_dealt":
            continue
        metric = {"label": label,
                  "value": ratio(sum(a for a, _ in pairs), sum(b for _, b in pairs), zero_label) if pairs else None,
                  "sample_size": len(pairs), "window_size": len(rows)}
        if len(pairs) != len(rows):
            metric["note"] = f"{len(pairs)}/{len(rows)} 场有数据"
        metrics.append(metric)
    metrics.insert(2, {"label": "分析样本胜率", "format": "percent",
                       **win_rate_sample(analysis_outcomes(player.analysis).values())})
    return metrics


class StatsService:
    def __init__(self, client, storage, settings):
        self.client, self.storage, self.settings = client, storage, settings
        self.pending = PendingManager()
        self._cooldowns = OrderedDict()
        self._mutations = OrderedDict()
        self._serial = itertools.count(1)
        self._binding_lock = asyncio.Lock()

    def help(self, caller):
        p = "ba"
        return f"""【断箭战报】使用指南
首次使用：{p} search 昵称 → 找到目标后发送 {p} bind stb:玩家ID → 按提示发送 {p} select <请求码> <序号> 确认一次即可。
说明：绑定仅用于设定默认查询对象，不代表 Steam 账号所有权认证。

常用命令：
• {p} me：查看自己的玩家概览
• {p} stats <玩家>：查看指定玩家概览（支持 me / @群友 / stb:ID / name:昵称）
• {p} recent [数量] / {p} me recent [数量]：查看自己近期战绩（默认 {self.settings.default_limit} 场）
• {p} recent <玩家> --limit <数量>：查看指定玩家近期战绩（如 {p} recent name:"Player 2" --limit 10）
• {p} last [玩家]：查看最近一场可用战报
• {p} last <序号> / {p} me last <序号>：按近期战绩顺序查看自己的第 1–10 场，例如 {p} last 2
• {p} match <对局ID>：查看指定单局战报
• {p} search <昵称>：搜索玩家候选
• {p} bind <玩家> / {p} binding / {p} unbind：绑定、查看或解除默认账号
• {p} select <请求码> <序号> / {p} cancel <请求码>：确认或取消候选选择
• {p} quota：查看个人、本群及全插件今日请求额度
• {p} help：查看本帮助

输入与额度提示：
• 纯数字昵称请加前缀（如 name:12345）；与关键词重名请写 name:me。
• steam:SteamID64 需等待权威映射确认，当前请先用昵称搜索再选择 STB ID。
• 管理员命令：{p} status（查看状态）、{p} cache clear（清理缓存）。
• 全插件每日固定上限 100 次（个人默认 20 次、每群默认 100 次），北京时间 0 点重置；按实际发出的上游 HTTP 请求计数（含重试与失败），命中缓存不计入。
• 群聊请先 @ 机器人再发送命令；绑定候选超过 3 人时，请到 https://dash.batrace.top/ 搜索并使用 STB ID 绑定。
• 图片渲染或发送未成功时会回复同内容的文字战报。"""

    def _mutation(self, caller):
        key = (caller.platform, caller.user)
        token = next(self._serial)
        self._mutations.pop(key, None)
        self._mutations[key] = token
        while len(self._mutations) > 1000:
            self._mutations.popitem(last=False)
        return token

    def _cooldown(self, caller):
        key = (caller.platform, caller.user)
        now = time.monotonic()
        previous = self._cooldowns.get(key, -float("inf"))
        remaining = self.settings.cooldown_seconds - (now - previous)
        if remaining > 0:
            raise ValueError(f"""查询过于频繁，请 {remaining:.0f} 秒后再试。""")
        self._cooldowns.pop(key, None)
        self._cooldowns[key] = now
        while len(self._cooldowns) > 1000:
            self._cooldowns.popitem(last=False)

    async def execute(self, request: Request, caller: Caller):
        action = request.action
        if action == "cancel":
            ok = self.pending.cancel(caller.scope, request.code)
            return Response("已取消本次操作。" if ok else "该请求已失效或不属于当前会话，无法取消。")
        if action == "select":
            original, candidate = self.pending.consume(caller.scope, request.code, request.index)
            generation = self.pending.begin(caller.scope)
            if original.action == "bind":
                mutation = self._mutation(caller)
                verified = await self.client.player(candidate.id, force=True)
                if verified.data.id != candidate.id or verified.stale:
                    raise ValueError("未能重新核实该账号身份，原绑定保持不变，请稍后重试。")
                async with self._binding_lock:
                    if (not self.pending.is_current(caller.scope, generation)
                            or self._mutations.get((caller.platform, caller.user)) != mutation):
                        return Response("本次选择已被后续操作覆盖，原绑定未作修改。")
                    player = verified.data
                    await self.storage.set_binding(caller.platform, caller.user, Binding(
                        self.client.namespace, player.id, player.name, player.source_url or ""))
                return Response(f"""已绑定默认账号：{player.name or '未命名玩家'}
STB ID：{player.id}
{player.source_url or ''}""", instructions=(("查看战报：", "ba me"),))
            return await self._report(original, candidate.id)

        generation = self.pending.begin(caller.scope)
        if action == "help":
            return Response(self.help(caller), instructions=(("发送示例：", "ba me"),))
        if action == "binding":
            binding = await self.storage.get_binding(caller.platform, caller.user)
            if binding is None:
                return Response("你还没有绑定默认账号。", instructions=(("绑定请发送：", "ba bind name:昵称"),))
            return Response(f"""当前默认账号：{binding.display_name or '未命名玩家'}
STB ID：{binding.player_id}
{binding.source_url}
注：昵称为绑定时记录，后续查询始终以该 STB ID 为准。""")
        if action == "unbind":
            self._mutation(caller)
            async with self._binding_lock:
                removed = await self.storage.unbind(caller.platform, caller.user)
            return Response("已解除默认账号绑定。" if removed else "当前尚未绑定任何账号。")
        self._cooldown(caller)
        if action == "match":
            return Response(view=match_view(await self.client.match(request.code)))
        target = request.target or Target("me", "")
        if target.kind == "steam":
            raise ValueError("SteamID64 与 STB ID 的对应关系尚未验证，两种 ID 不能直接互换。请先用 search <昵称> 搜索，再选择对应的 STB ID。")
        if target.kind in {"me", "mention"}:
            user = caller.user if target.kind == "me" else target.value
            binding = await self.storage.get_binding(caller.platform, user)
            if binding is None:
                if target.kind == "me":
                    return Response("你尚未绑定默认账号。", instructions=(("绑定请发送：", "ba bind name:昵称"),))
                raise ValueError("对方尚未绑定默认游戏账号。")
            if binding.namespace != self.client.namespace:
                raise ValueError("当前绑定的账号属于其他数据源环境，与当前接入不一致，请重新绑定。")
            target = Target("stb", binding.player_id)
        if target.kind == "stb":
            if action != "bind":
                return await self._report(request, target.value)
            result = await self.client.player(target.value)
            candidates = [result.data]
        else:
            result = await self.client.search(target.value)
            candidates = list({p.id: p for p in result.data}.values())
            candidates.sort(key=lambda p: p.name != target.value)
            if not candidates:
                return Response("未找到匹配的候选玩家，建议换用更准确的昵称重试（未搜到不代表该玩家不存在）。")
            if action not in {"bind", "search"} and len(candidates) == 1 and candidates[0].name == target.value:
                return await self._report(request, candidates[0].id)
        if action == "bind" and result.stale:
            raise ValueError("当前仅命中旧缓存，暂无法完成绑定核验，原绑定保持不变，请稍后重试。")
        if action == "bind" and len(candidates) > 3:
            if not self.pending.is_current(caller.scope, generation):
                return Response("本次查询已被新操作覆盖，请使用最新的候选列表。")
            return Response("匹配到超过 3 位玩家，请使用 STB ID 绑定。\n"
                            "请前往 https://dash.batrace.top/ 搜索昵称，打开目标玩家主页，复制链接末尾的数字 ID。",
                            instructions=(("取得 ID 后发送：", "ba bind stb:玩家ID"),))
        operation = Request("stats", limit=request.limit) if action == "search" else request
        selected = candidates[:10]
        pending = self.pending.publish(caller.scope, generation, operation, selected)
        if pending is None:
            return Response("本次查询已被新操作覆盖，请使用最新的候选列表。")
        lines = ["查询结果如下，请确认绑定账号：" if action == "bind" else "查询结果如下，请选择账号："]
        if len(candidates) == 1 and candidates[0].name != target.value and target.kind == "name":
            lines.append("仅找到相似昵称结果，请仔细核对 STB ID 与来源链接后再确认。")
        if result.stale:
            lines.append(f"旧缓存 · 抓取于 {result.fetched_at.isoformat()}")
        for index, player in enumerate(selected, 1):
            lines.extend([f"{index}. {player.name or '未命名玩家'} · STB ID：{player.id}",
                          player.source_url or "来源链接暂无"])
        lines.append("---")
        instructions = tuple((f"请在 120 秒内发送指令确认{'第 ' + str(index) + ' 项' if len(selected) > 1 else ''}：",
                              f"ba select {pending.code} {index}") for index in range(1, len(selected) + 1))
        instructions += (("取消请发送：", f"ba cancel {pending.code}"),)
        return Response("\n".join(lines), instructions=instructions)

    async def _report(self, request, player_id):
        player_result = await self.client.player(player_id)
        player = player_result.data
        if player.id != player_id:
            raise ValueError("上游返回的玩家身份与请求不一致。")
        limit = request.limit if request.action == "recent" else self.settings.max_limit
        if request.action == "last":
            limit = max(limit, request.index)
        result = await self.client.recent(player.id, limit)
        matches = result.data
        if request.action == "last":
            visible = (m for m in matches if participant_for(m, player.id) is not None)
            for position, match in enumerate(visible, start=1):
                if request.index and position != request.index:
                    continue
                try:
                    detail = await self.client.match(match.id)
                except Exception as exc:
                    if getattr(exc, "code", "") in {"not_found", "not_recorded", "no_data"}:
                        if request.index:
                            return Response(f"第 {position} 场（#{match.id}）暂无单局战报。")
                        continue
                    raise
                view = match_view(detail, player, recent_match=match)
                view.update(freshness(player_result, result, detail))
                return Response(view=view)
            if request.index:
                return Response(f"{player.name} 的近期战绩中没有第 {request.index} 场。")
            return Response(f"{player.name} · STB ID：{player.id}\n当前返回样本中暂无可用单局战报。\n{player.source_url or ''}")
        rows = recent_rows(matches, player.id, player, with_scores=request.action == "recent")
        times = [m.started_at for m in matches if m.started_at and any(p.player_id == player.id for p in m.players)]
        scope = f"近期{len(rows)}场"
        view = {"kind": "recent" if request.action == "recent" else "overview",
                "player": asdict(player), "rows": rows, "scope": scope,
                "source_url": player.source_url, **freshness(player_result, result)}
        if times:
            from zoneinfo import ZoneInfo
            zone = ZoneInfo(self.settings.timezone)
            view["scope"] += f" · {min(times).astimezone(zone):%m-%d}—{max(times).astimezone(zone):%m-%d}"
            if len(times) != len(rows):
                view["scope"] += f"（{len(times)}/{len(rows)} 场有开始时间）"
        if view["kind"] == "overview":
            view["profile_rating"] = profile_rating(matches, player.id, player.analysis)
            view["metrics"] = overview_metrics(player, rows)
            view["common_units"] = common_units(player, matches)
            view["map_stats"] = [{"name": m["mapName"] or f"地图#{m['mapId']}",
                                  "count": m["matchCount"], "win_rate": m["winRate"]}
                                 for m in player.analysis.get("mapPerformance", [])[:5]]
            analysis_count = player.analysis.get("matchCount")
            if analysis_count is None:
                analysis_count = len(player.analysis.get("trend", {}).get("points", []))
            view["style_scope"] = f"近{analysis_count}场风格" if player.analysis else ""
            view["categories"] = [{"name": CATEGORY_NAMES.get(c["categoryKey"], c["categoryKey"]),
                                   "percentage": c["percentage"]}
                                  for c in player.analysis.get("categoryPreferences", [])]
        return Response(view=view)

    def close(self):
        self.pending.clear()
        self._cooldowns.clear()
        self._mutations.clear()
