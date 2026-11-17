"""One presentation contract for HTML cards and their plain-text fallback.

Service -> Renderer view contract (independent of models.py):
  kind: "overview" | "recent" | "match"; title: optional display title.
  player: {id: str, name: str, source_url?: str}; required for player cards.
  metrics: [{label: str, value: number | str | None, unit?: str,
             format?: "integer" | "decimal" | "percent" | "signed", note?: str}].
    Numbers use two decimals, omitting .00 for exact integers; counts use integer. Percent values are
    already on the 0..100 scale. Strings are display text, never HTML.
  rows (recent): [{id, started_at, map_name, mode, outcome, duration_seconds,
    elo_delta, kills, losses, destroyed_value, lost_value, unit_exchange,
    value_exchange, source_url}]. Every field except id may be absent.
  match (match): {id, map_name, mode, started_at, duration_seconds, winner}.
  teams (match): [{name, outcome, players: [{id, name, elo, elo_before, elo_delta, kills,
    losses, destroyed_value, lost_value, unit_exchange, value_exchange}]}].
    Team sizes are unrestricted; never pad absent players with dummy rows.
  common_units (overview, optional): [{name: str, count?: int}].
  scope: str describing actual sample / mode / coverage (never inferred).
  fetched_at, source_updated_at: aware datetime or ISO 8601 with timezone.
  stale: bool; source_url: optional public http(s) link; notes?: list[str].
  synthetic?: bool marks previews explicitly as synthetic sample data.

Outcome: win/loss/unknown; mode: ranked/custom/unknown (other labels preserved).
Unit counts (kills/losses) and values (destroyed_value/lost_value) are displayed
separately. Renderer does not infer coverage, exchange ratios, or winners.
Team summaries average supplied ratings only when every member has that field;
bar widths use the original numeric values, never their rounded display text.
None, blank, nonfinite, and absent values display as 暂无; real zero stays zero.
Missing/naive timestamps display as 暂无, rather than guessing a timezone.

Renderer(theme='light', timezone='Asia/Shanghai').template(view) returns final
escaped HTML. For AstrBot use the FIXED template '{{ body | safe }}' with
data={'body': html}; never pass the generated HTML as another Jinja template.
This preserves nicknames containing literal Jinja tags. text(view) uses exactly
the same normalized presentation data. For recent cards, iterate
views(view), which splits rows into pages of 5, before calling either method.
Each page preserves scope, identity, timestamps and page/row coverage. These
methods never query a service or load a remote avatar/font/resource.
"""

from __future__ import annotations

from collections.abc import Mapping
import base64
from copy import deepcopy
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape

MISSING = "暂无"
_TITLES = {"overview": "玩家概览", "recent": "近期战绩", "match": "单局战报"}
_OUTCOMES = {"win": "胜利", "loss": "失利", "unknown": "胜负暂无"}
_MODES = {"ranked": "排位", "custom": "自定义", "unknown": "暂无"}
_ROLES = {"装甲": "armor", "步兵": "infantry", "侦察": "recon", "炮兵": "artillery",
          "防空": "air-defense", "直升机": "helicopter", "固定翼": "aircraft", "后勤/运输": "logistics"}


def _plain(value, default=MISSING) -> str:
    # str() also strips a caller-supplied Markup object's HTML trust flag.
    if value is None or isinstance(value, bool):
        return default
    result = str(value).strip()
    return result if result else default


def _number(value) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    try:
        parsed = Decimal(str(value))
        return parsed if parsed.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def _numeric(value, style="decimal") -> str:
    parsed = _number(value)
    if parsed is None:
        return MISSING
    if style == "integer":
        return str(int(parsed)) if parsed == parsed.to_integral_value() else MISSING
    if style == "decimal" and parsed == parsed.to_integral_value():
        return str(int(parsed))
    return format(parsed, "+.2f" if style == "signed" else ".2f")


def _split_width(values) -> float | None:
    numbers = [_number(value) for value in values]
    if len(numbers) != 2 or any(value is None or value < 0 for value in numbers) or sum(numbers) == 0:
        return None
    return float(numbers[0] / sum(numbers) * 100)


def _safe_url(value) -> str:
    if not isinstance(value, str) or not value:
        return ""
    # Links are shown, never fetched. Reject credentials and ambiguous spellings.
    if any(character.isspace() or ord(character) < 32 for character in value) or "\\" in value:
        return ""
    try:
        parts = urlsplit(value)
        if (
            parts.scheme.lower() not in {"http", "https"}
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
        ):
            return ""
        parts.port  # Invalid/out-of-range ports raise ValueError.
    except ValueError:
        return ""
    return str(value)


def _mapping(value) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def _records(value) -> list[Mapping]:
    return [_mapping(item) for item in value] if isinstance(value, (list, tuple)) else []


def _outcome(value) -> dict:
    key = value if isinstance(value, str) and value in _OUTCOMES else "unknown"
    return {"key": key, "label": _OUTCOMES[key]}


class Renderer:
    """Render self-contained cards without network access or model imports."""

    def __init__(self, theme: str = "light", timezone: str = "Asia/Shanghai"):
        if theme not in {"dark", "light"}:
            raise ValueError("theme must be dark or light")
        self.theme = theme
        self.timezone = ZoneInfo(timezone)
        logo = "github-mark-white.svg" if theme == "dark" else "github-mark-black.svg"
        self._github_logo = "data:image/svg+xml;base64," + base64.b64encode(
            (Path(__file__).parent / "assets" / logo).read_bytes()).decode("ascii")
        self._environment = Environment(
            loader=FileSystemLoader(Path(__file__).with_name("templates")),
            autoescape=select_autoescape(("html",)),
            trim_blocks=True,
            lstrip_blocks=True,
        )

    def views(self, view: dict, page_size: int = 5) -> list[dict]:
        """Return independent recent pages; all other cards have one page."""
        if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 5:
            raise ValueError("page_size must be an integer from 1 to 5")
        if view.get("kind", "overview") != "recent":
            return [deepcopy(view)]
        rows = view.get("rows") or []
        if not isinstance(rows, (list, tuple)):
            rows = []
        count = max(1, (len(rows) + page_size - 1) // page_size)
        result = []
        for index in range(count):
            page = deepcopy(view)
            page.update(
                rows=deepcopy(list(rows[index * page_size : (index + 1) * page_size])),
                page=index + 1,
                page_count=count,
                row_start=index * page_size + 1 if rows else 0,
                row_total=len(rows),
            )
            result.append(page)
        return result

    def _time(self, value) -> str:
        try:
            if isinstance(value, str):
                value = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if not isinstance(value, datetime) or value.utcoffset() is None:
                return MISSING
            return value.astimezone(self.timezone).strftime("%Y-%m-%d %H:%M %Z")
        except (ValueError, OverflowError):
            return MISSING

    @staticmethod
    def _duration(value) -> str:
        parsed = _number(value)
        if parsed is None or parsed < 0:
            return MISSING
        seconds = int(parsed)
        minutes, seconds = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        return f"{hours}时{minutes:02d}分{seconds:02d}秒" if hours else f"{minutes}分{seconds:02d}秒"

    @staticmethod
    def _metric(metric: Mapping) -> dict:
        value = metric.get("value")
        style = metric.get("format", "decimal")
        if style not in {"integer", "decimal", "percent", "signed"}:
            style = "decimal"
        formatted = _plain(value) if isinstance(value, str) else _numeric(value, style)
        unit = _plain(metric.get("unit"), "")
        if style == "percent" and not unit:
            unit = "%"
        if formatted == MISSING:
            unit = ""
        display = formatted + (unit if unit == "%" else " " + unit) if unit else formatted
        return {
            "label": _plain(metric.get("label")),
            "value": formatted,
            "unit": unit,
            "display": display,
            "note": _plain(metric.get("note"), ""),
        }

    def _exchange(self, item: Mapping, *, elo: bool = False) -> list[dict]:
        """Pair comparable values, preserving zero and unknown independently."""
        def value(key, style="decimal"):
            raw = item.get(key)
            return _plain(raw) if isinstance(raw, str) else _numeric(raw, style)

        def pair(first, second, style="decimal"):
            return f"{value(first, style)} / {value(second, style)}"

        metrics = []
        if elo:
            rating = value("elo")
            delta = value("elo_delta", "signed")
            metrics.append({"label": "ELO", "value": rating + (f" ({delta})" if delta != MISSING else "")})
        metrics.extend([
            {"label": "分值 K/D", "value": value("value_exchange")},
            {"label": "摧毁 / 损失", "value": pair("destroyed_value", "lost_value")},
            {"label": "击杀 / 阵亡", "value": pair("kills", "losses", "integer")},
        ])
        if elo:
            if any(item.get(key) is not None for key in ("damage_dealt", "damage_received")):
                metrics.append({"label": "伤害 / 承伤", "value": pair("damage_dealt", "damage_received")})
            for key, label, style in (("objectives", "占点", "integer"),
                                      ("spawned_value", "出兵花费", "decimal"),
                                      ("supply", "补给消耗", "decimal")):
                if item.get(key) is not None:
                    metrics.append({"label": label, "value": value(key, style)})
        return [self._metric(metric) for metric in metrics]

    def _prepare(self, view: dict) -> dict:
        kind = view.get("kind", "overview")
        if kind not in _TITLES:
            raise ValueError("unknown card kind")
        player = _mapping(view.get("player"))
        match = _mapping(view.get("match"))
        prepared = {
            "kind": kind,
            "github_logo": self._github_logo,
            "title": _plain(view.get("title"), _TITLES[kind]),
            "theme": self.theme,
            "player": {
                "name": _plain(player.get("name")),
                "id": _plain(player.get("id")),
                "source_url": _safe_url(player.get("source_url")),
            },
            "metrics": [self._metric(item) for item in _records(view.get("metrics"))],
            "scope": _plain(view.get("scope")),
            "fetched_at": self._time(view.get("fetched_at")),
            "source_updated_at": self._time(view.get("source_updated_at")),
            "stale": view.get("stale") is True,
            "source_url": _safe_url(view.get("source_url")) or _safe_url(player.get("source_url")),
            "synthetic": view.get("synthetic") is True,
            "notes": [_plain(note) for note in view.get("notes", [])] if isinstance(view.get("notes"), (list, tuple)) else [],
            "common_units": [
                {"name": _plain(item.get("name")), "count": _numeric(item.get("count"), "integer"),
                 "damage": _numeric(item.get("damage")), "roi": _numeric(item.get("roi"))}
                for item in _records(view.get("common_units"))
            ],
            "analysis_scope": _plain(view.get("analysis_scope"), ""),
            "categories": [{"name": _plain(item.get("name")), "percentage": _numeric(item.get("percentage"))}
                           for item in _records(view.get("categories"))],
            "map_stats": [{"name": _plain(item.get("name")), "count": _numeric(item.get("count"), "integer"),
                           "win_rate": _numeric(item.get("win_rate"))}
                          for item in _records(view.get("map_stats"))],
            "rows": [],
            "teams": [],
            "pagination": "",
        }
        for item in _records(view.get("rows")):
            score = _number(item.get("score"))
            prepared["rows"].append({
                "score": format(score, ".1f") if score is not None else MISSING,
                "score_band": item.get("score_band") if item.get("score_band") in {"low", "medium", "high"} else "medium",
                "id": _plain(item.get("id")),
                "started_at": self._time(item.get("started_at")),
                "map_name": _plain(item.get("map_name")),
                "mode": _MODES.get(_plain(item.get("mode")), _plain(item.get("mode"))),
                "outcome": _outcome(item.get("outcome")),
                "duration": self._duration(item.get("duration_seconds")),
                "elo_delta": _numeric(item.get("elo_delta"), "signed"),
                "exchange": self._exchange(item),
                "source_url": _safe_url(item.get("source_url")),
            })
        prepared["match"] = {
            "id": _plain(match.get("id")),
            "map_name": _plain(match.get("map_name")),
            "mode": _MODES.get(_plain(match.get("mode")), _plain(match.get("mode"))),
            "started_at": self._time(match.get("started_at")),
            "duration": self._duration(match.get("duration_seconds")),
            "winner": _plain(match.get("winner")),
            "target_outcome": _outcome(match.get("target_outcome")),
        }
        raw_teams = _records(view.get("teams"))
        for team in raw_teams:
            members = _records(team.get("players"))
            team_metrics = [self._metric(item) for item in _records(team.get("metrics"))]
            summary = []
            # The service's mean ELO is the post-match value, not the prediction's
            # pre-match input. Never reconstruct a missing pre-match rating.
            for label, field in (("赛前平均 ELO", "elo_before"), ("平均 ELO 变化", "elo_delta")):
                values = [_number(item.get(field)) for item in members]
                complete = bool(values) and all(value is not None for value in values)
                summary.append(self._metric({"label": label, "value": sum(values) / len(values) if complete else None,
                                             "format": "signed" if field == "elo_delta" else "decimal"}))
            for label in ("赛后平均 ELO", "摧毁分"):
                metric = next((item for item in team_metrics if item["label"] == label), self._metric({"label": label}))
                summary.append(metric)
            composition = []
            for item in _records(team.get("composition")):
                percentage = _number(item.get("percentage"))
                composition.append({"name": _plain(item.get("name")), "percentage": _numeric(percentage, "percent"),
                                    "number": percentage if percentage is not None and 0 <= percentage <= 100 else None,
                                    "role": _ROLES.get(item.get("name"), "other")})
            total = sum((item["number"] or 0) for item in composition)
            # Service omits zero-spend roles from a complete composition. Only
            # a full, unqualified 100% distribution makes that omission zero.
            composition_complete = (bool(composition) and all(item["number"] is not None for item in composition)
                                    and abs(total - 100) <= Decimal("0.01") and not team.get("composition_note"))
            prepared["teams"].append({
                "name": _plain(team.get("name")),
                "side": "a" if team.get("name") == "A 队" else "b" if team.get("name") == "B 队" else "neutral",
                "outcome": _outcome(team.get("outcome")),
                "metrics": team_metrics,
                "summary": [summary[index] for index in (0, 2, 1, 3)],
                "composition": composition,
                "composition_complete": composition_complete,
                "composition_note": _plain(team.get("composition_note"), ""),
                "players": [
                    {"id": _plain(item.get("id")), "name": _plain(item.get("name")),
                     "is_target": bool(player.get("id")) and item.get("id") == player.get("id"),
                     "metrics": self._exchange(item, elo=True)}
                    for item in members
                ],
            })
        prediction = _mapping(view.get("prediction"))
        a, b = _number(prediction.get("a")), _number(prediction.get("b"))
        prepared["prediction"] = ({"a": _numeric(a, "percent"), "b": _numeric(b, "percent"), "width": float(a)}
                                  if a is not None and b is not None and 0 <= a <= 100 and 0 <= b <= 100 else None)
        # Each comparison row keeps the actual team order and both denominators.
        labels = list(dict.fromkeys(m["label"] for team in prepared["teams"][:2] for m in team["metrics"]))
        prepared["comparisons"] = []
        for label in labels:
            pair = [next((m for m in team["metrics"] if m["label"] == label), None) for team in prepared["teams"][:2]]
            if len(pair) == 2:
                numbers = [_number(next((m.get("value") for m in _records(team.get("metrics"))
                                         if _plain(m.get("label")) == label), None)) for team in raw_teams[:2]]
                prepared["comparisons"].append({"label": label,
                                                 "values": [m["display"] if m else MISSING for m in pair],
                                                 "notes": [m["note"] if m else "" for m in pair], "width": _split_width(numbers)})
        category_names = list(dict.fromkeys(category["name"] for team in prepared["teams"][:2]
                                             for category in team["composition"]))
        category_names.sort(key=lambda name: list(_ROLES).index(name) if name in _ROLES else len(_ROLES))
        prepared["composition_rows"] = []
        if len(prepared["teams"]) >= 2:
            for name in category_names:
                values = [next((category["number"] for category in team["composition"] if category["name"] == name),
                               Decimal(0) if team["composition_complete"] else None) for team in prepared["teams"][:2]]
                prepared["composition_rows"].append({"label": name, "role": _ROLES.get(name, "other"),
                                                     "values": [_numeric(value, "percent") + "%" if value is not None else MISSING for value in values],
                                                     "width": _split_width(values)})
        page = _number(view.get("page"))
        page_count = _number(view.get("page_count"))
        row_total = _number(view.get("row_total"))
        row_start = _number(view.get("row_start"))
        if kind == "recent" and all(value is not None and value == value.to_integral_value() for value in (page, page_count, row_total, row_start)):
            end = int(row_start) + len(prepared["rows"]) - 1 if prepared["rows"] else 0
            prepared["pagination"] = f"第 {int(page)}/{int(page_count)} 页 · 本页 {int(row_start)}–{end} 场 / 共 {int(row_total)} 场"
        return prepared

    def template(self, view: dict) -> str:
        prepared = self._prepare(view)
        return self._environment.get_template(f"{prepared['kind']}.html").render(view=prepared)

    def text(self, view: dict) -> str:
        """Compact report using the same formatted facts as the image card."""
        data = self._prepare(view)
        lines = [f"断箭战报 · {data['title']}"]
        if data["synthetic"]:
            lines.append("合成样本 · 非真实玩家战绩")
        if data["kind"] != "match" or data["player"]["id"] != MISSING:
            lines.append(f"{data['player']['name']} · ID {data['player']['id']}")
            if data["kind"] == "match" and data["match"]["target_outcome"]["key"] != "unknown":
                lines[-1] += " · " + data["match"]["target_outcome"]["label"]
        if data["scope"] != MISSING:
            lines.append(data["scope"])
        if data["pagination"]:
            lines.append(data["pagination"])

        def metrics(items):
            return " · ".join(f"{m['label']}：{m['display']}" + (f"（{m['note']}）" if m["note"] else "") for m in items)

        if data["kind"] == "overview":
            for offset in range(0, len(data["metrics"]), 2):
                lines.append(metrics(data["metrics"][offset:offset + 2]))
            if not data["metrics"]:
                lines.append("统计指标：暂无")
            if data["analysis_scope"]:
                lines.append(data["analysis_scope"])
            if data["categories"]:
                lines.append("兵种：" + " · ".join(f"{c['name']} {c['percentage']}%" for c in data["categories"]))
            if data["common_units"]:
                lines.append("常用单位")
                for unit in data["common_units"]:
                    detail = [f"{unit['count']} 次出场"] if unit['count'] != MISSING else []
                    if unit['damage'] != MISSING:
                        detail.append(f"伤害 {unit['damage']}")
                    if unit['roi'] != MISSING:
                        detail.append(f"回报 {unit['roi']}")
                    lines.append(unit['name'] + (" · " + " · ".join(detail) if detail else ""))
            if data["map_stats"]:
                lines.append("地图表现")
                for row in data["map_stats"]:
                    rate = row['win_rate'] + '%' if row['win_rate'] != MISSING else MISSING
                    lines.append(f"{row['name']} · {row['count']} 场 · 胜率 {rate}")
        elif data["kind"] == "recent":
            for row in data["rows"]:
                heading = [f"评分 {row['score']}", row['outcome']['label'], row['map_name']]
                if row['duration'] != MISSING:
                    heading.append(row['duration'])
                lines.append(" · ".join(heading))
                detail = [f"ELO {row['elo_delta']}"] if row['elo_delta'] != MISSING else []
                detail.extend(f"{m['label']} {m['display']}" for m in row['exchange'])
                lines.append(" · ".join(detail))
                lines[-1] += f" · #{row['id']}"
            if not data["rows"]:
                lines.append("近期对局：暂无")
        else:
            match = data["match"]
            lines.append(f"#{match['id']} · {match['map_name']} · {match['duration']}")
            meta = []
            if match['mode'] != MISSING:
                meta.append(match['mode'])
            if match['started_at'] != MISSING:
                meta.append(match['started_at'])
            if match['winner'] != MISSING:
                meta.append(f"胜方：{match['winner']}")
            if meta:
                lines.append(" · ".join(meta))
            prediction = data["prediction"]
            lines.append(f"赛前预测 A {prediction['a']}% : B {prediction['b']}%（按赛前 ELO 估算）" if prediction else "赛前预测：暂无")
            if data["comparisons"]:
                lines.append("双方对比（" + " / ".join(t["name"] for t in data["teams"][:2]) + "）")
                for row in data["comparisons"]:
                    lines.append(f"{row['label']}：" + " / ".join(v + (f"（{n}）" if n else "") for v, n in zip(row['values'], row['notes'])))
            lines.append("兵种构成（基础花费估算）")
            for team in data["teams"]:
                lines.append(team['name'] + "：" + (" · ".join(f"{c['name']} {c['percentage']}%" for c in team['composition']) or "暂无"))
                if team['composition_note']:
                    lines.append(team['composition_note'])
            lines.append("玩家")
            for team in data["teams"]:
                heading = f"{team['name']} · {len(team['players'])} 人"
                if team['outcome']['key'] != 'unknown':
                    heading += " · " + team['outcome']['label']
                lines.extend(["", heading])
                for player in team["players"]:
                    lines.append(player['name'] + ("（查询玩家）" if player['is_target'] else ""))
                    for offset in range(0, len(player['metrics']), 3):
                        lines.append(metrics(player['metrics'][offset:offset + 3]))
                if not team["players"]:
                    lines.append("队员：暂无")
            if not data["teams"]:
                lines.append("分队与玩家：暂无")
        lines.extend(data["notes"])
        if data["stale"]:
            lines.append(f"旧缓存 · 抓取时间：{data['fetched_at']}")
        if data["source_url"]:
            lines.append(f"BATrace：{data['source_url']}")
        return "\n".join(lines)
