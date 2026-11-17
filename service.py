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
from .models import FetchResult, Match, Participant, Player
from .pending import PendingManager
from .storage import Binding


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


def ratio(numerator, denominator):
    if numerator is None or denominator is None:
        return None
    if denominator == 0:
        return "无损失" if numerator > 0 else None
    return numerator / denominator


def freshness(*results):
    return {
        "fetched_at": min(r.fetched_at for r in results),
        "stale": any(r.stale for r in results),
        "source_updated_at": min((r.source_updated_at for r in results if r.source_updated_at), default=None),
    }


def participant_view(p: Participant):
    return dict(id=p.player_id, name=p.name, elo=p.elo, elo_delta=p.elo_delta,
                kills=p.kills, losses=p.losses, destroyed_value=p.destroyed_value,
                lost_value=p.lost_value, unit_exchange=ratio(p.kills, p.losses),
                value_exchange=ratio(p.destroyed_value, p.lost_value))


def recent_rows(matches, player_id):
    rows = []
    for match in matches:
        person = participant_for(match, player_id)
        if person is None:
            continue
        row = participant_view(person)
        row.update(id=match.id, started_at=match.started_at, map_name=match.map_name,
                   mode=match.mode, outcome=person.outcome,
                   duration_seconds=match.duration_seconds, source_url=match.source_url)
        rows.append(row)
    return rows


def match_view(result, player=None):
    match = result.data
    if player is not None:
        participant_for(match, player.id)
    teams = OrderedDict()
    for person in match.players:
        key = person.team if person.team is not None else "未知"
        if key not in teams:
            teams[key] = {"name": f"分队 {key}", "outcome": "unknown", "players": []}
        teams[key]["players"].append(participant_view(person))
        if match.winner_team is not None and person.team is not None:
            teams[key]["outcome"] = "win" if person.team == match.winner_team else "loss"
    view = {"kind": "match", "match": {
        "id": match.id, "map_name": match.map_name, "mode": match.mode,
        "started_at": match.started_at, "duration_seconds": match.duration_seconds,
        "winner": f"分队 {match.winner_team}" if match.winner_team is not None else None,
    }, "teams": list(teams.values()), "scope": f"单局 · 实际收录 {len(match.players)} 名玩家",
        "source_url": match.source_url, **freshness(result)}
    if player:
        view["player"] = asdict(player)
    if not match.complete:
        view["notes"] = ["本局数据可能尚未完整。"]
    return view


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
• {p} last [玩家] / {p} me last：查看最近一场可用单局战报
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
        result = await self.client.recent(player.id, limit)
        matches = result.data
        if request.action == "last":
            for match in matches:
                if participant_for(match, player.id) is None:
                    continue
                try:
                    detail = await self.client.match(match.id)
                except Exception as exc:
                    if getattr(exc, "code", "") in {"not_found", "not_recorded", "no_data"}:
                        continue
                    raise
                view = match_view(detail, player)
                view.update(freshness(player_result, result, detail))
                return Response(view=view)
            return Response(f"{player.name} · STB ID：{player.id}\n当前返回样本中暂无可用单局战报。\n{player.source_url or ''}")
        rows = recent_rows(matches, player.id)
        times = [m.started_at for m in matches if m.started_at and any(p.player_id == player.id for p in m.players)]
        scope = f"近期接口样本 · 实际 {len(rows)} 场（请求最多 {limit} 场）；非完整历史，模式以单局标注为准"
        view = {"kind": "recent" if request.action == "recent" else "overview",
                "player": asdict(player), "rows": rows, "scope": scope,
                "source_url": player.source_url, **freshness(player_result, result)}
        if times:
            from zoneinfo import ZoneInfo
            zone = ZoneInfo(self.settings.timezone)
            view["scope"] += f" · {min(times).astimezone(zone):%Y-%m-%d %H:%M} 至 {max(times).astimezone(zone):%Y-%m-%d %H:%M}"
        if view["kind"] == "overview":
            outcomes = Counter(row["outcome"] for row in rows)
            known = outcomes["win"] + outcomes["loss"]
            view["metrics"] = [
                {"label": "ELO", "value": player.elo},
                {"label": "样本场数", "value": len(rows), "format": "integer"},
                {"label": "胜 / 负 / 未知", "value": f"{outcomes['win']} / {outcomes['loss']} / {outcomes['unknown']}"},
                {"label": "已知结果胜率", "value": 100 * outcomes["win"] / known if known else None,
                 "format": "percent", "note": f"分母为 {known} 场已知胜负样本"},
            ]
            counts = Counter()
            names = {}
            for match in matches:
                person = participant_for(match, player.id)
                if person is not None:
                    for unit in person.units:
                        uid = unit["id"]
                        counts[uid] += unit["count"]
                        if unit.get("name"):
                            # Matches arrive newest first; names never define identity.
                            names.setdefault(uid, unit["name"])
            labels = {uid: names.get(uid, f"单位#{uid}") for uid in counts}
            duplicates = Counter(labels.values())
            view["common_units"] = [
                {"id": uid, "name": f"{labels[uid]}（ID：{uid}）" if duplicates[labels[uid]] > 1 else labels[uid],
                 "count": count}
                for uid, count in counts.most_common(5)
            ]
            view["notes"] = ["常用单位按当前样本中的出场记录数统计；未收录名称时显示来源单位 ID。"]
            if player.elo_observed_at:
                from zoneinfo import ZoneInfo
                at = player.elo_observed_at.astimezone(ZoneInfo(self.settings.timezone))
                view["notes"].append(f"ELO 对应时间：{at:%Y-%m-%d %H:%M %Z}")
        return Response(view=view)

    def close(self):
        self.pending.clear()
        self._cooldowns.clear()
        self._mutations.clear()
