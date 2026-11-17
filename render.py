"""One presentation contract for HTML cards and their plain-text fallback.

Service -> Renderer view contract (independent of models.py):
  kind: "overview" | "recent" | "match"; title: optional display title.
  player: {id: str, name: str, source_url?: str}; required for player cards.
  metrics: [{label: str, value: number | str | None, unit?: str,
             format?: "integer" | "decimal" | "percent" | "signed", note?: str}].
    Numbers default to two decimals; counts use integer. Percent values are
    already on the 0..100 scale. Strings are display text, never HTML.
  rows (recent): [{id, started_at, map_name, mode, outcome, duration_seconds,
    elo_delta, kills, losses, destroyed_value, lost_value, unit_exchange,
    value_exchange, source_url}]. Every field except id may be absent.
  match (match): {id, map_name, mode, started_at, duration_seconds, winner}.
  teams (match): [{name, outcome, players: [{id, name, elo, elo_delta, kills,
    losses, destroyed_value, lost_value, unit_exchange, value_exchange}]}].
    Team sizes are unrestricted; never pad absent players with dummy rows.
  common_units (overview, optional): [{name: str, count?: int}].
  scope: str describing actual sample / mode / coverage (never inferred).
  fetched_at, source_updated_at: aware datetime or ISO 8601 with timezone.
  stale: bool; source_url: optional public http(s) link; notes?: list[str].
  synthetic?: bool marks previews explicitly as synthetic sample data.

Outcome: win/loss/unknown; mode: ranked/custom/unknown (other labels preserved).
Unit counts (kills/losses) and values (destroyed_value/lost_value) are displayed
separately. Renderer does not derive statistics, coverage, ratios, or winners.
None, blank, nonfinite, and absent values display as 暂无; real zero stays zero.
Missing/naive timestamps display as 暂无, rather than guessing a timezone.

Renderer(theme='dark', timezone='Asia/Shanghai').template(view) returns final
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
    return format(parsed, "+.2f" if style == "signed" else ".2f")


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

    def __init__(self, theme: str = "dark", timezone: str = "Asia/Shanghai"):
        if theme not in {"dark", "light"}:
            raise ValueError("theme must be dark or light")
        self.theme = theme
        self.timezone = ZoneInfo(timezone)
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
        fields = [
            ("kills", "摧毁单位", "integer", "个"),
            ("losses", "损失单位", "integer", "个"),
            ("unit_exchange", "单位交换比", "decimal", ""),
            ("destroyed_value", "摧毁价值", "decimal", ""),
            ("lost_value", "损失价值", "decimal", ""),
            ("value_exchange", "价值交换比", "decimal", ""),
        ]
        if elo:
            fields = [("elo", "ELO", "decimal", ""), ("elo_delta", "ELO 变化", "signed", "")] + fields
        return [
            self._metric({"label": label, "value": item.get(key), "format": style, "unit": unit})
            for key, label, style, unit in fields
        ]

    def _prepare(self, view: dict) -> dict:
        kind = view.get("kind", "overview")
        if kind not in _TITLES:
            raise ValueError("unknown card kind")
        player = _mapping(view.get("player"))
        match = _mapping(view.get("match"))
        prepared = {
            "kind": kind,
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
                {"name": _plain(item.get("name")), "count": _numeric(item.get("count"), "integer")}
                for item in _records(view.get("common_units"))
            ],
            "rows": [],
            "teams": [],
            "pagination": "",
        }
        for item in _records(view.get("rows")):
            prepared["rows"].append({
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
        }
        for team in _records(view.get("teams")):
            prepared["teams"].append({
                "name": _plain(team.get("name")),
                "outcome": _outcome(team.get("outcome")),
                "players": [
                    {"id": _plain(item.get("id")), "name": _plain(item.get("name")), "metrics": self._exchange(item, elo=True)}
                    for item in _records(team.get("players"))
                ],
            })
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
        """Use the HTML presentation values verbatim in a readable fallback."""
        data = self._prepare(view)
        lines = [f"断箭战报 · {data['title']}"]
        if data["synthetic"]:
            lines.append("合成样本 · 非真实玩家战绩")
        if data["kind"] != "match" or data["player"]["id"] != MISSING:
            lines.extend([data["player"]["name"], f"玩家 ID：{data['player']['id']}"])
        lines.append(f"统计范围：{data['scope']}")
        if data["pagination"]:
            lines.append(data["pagination"])
        for metric in data["metrics"]:
            lines.append(f"{metric['label']}：{metric['display']}" + (f"（{metric['note']}）" if metric["note"] else ""))
        if data["kind"] == "overview":
            if not data["metrics"]:
                lines.append("统计指标：暂无")
            lines.append("常用单位 / 兵种")
            lines.extend(f"{unit['name']}：{unit['count']}" + (" 个" if unit["count"] != MISSING else "") for unit in data["common_units"])
            if not data["common_units"]:
                lines.append(MISSING)
        elif data["kind"] == "recent":
            for index, row in enumerate(data["rows"], 1):
                lines.extend([
                    f"{index}. {row['map_name']} · {row['outcome']['label']}",
                    f"对局 ID：{row['id']}",
                    f"时间：{row['started_at']} · 模式：{row['mode']} · 时长：{row['duration']}",
                    f"ELO 变化：{row['elo_delta']}",
                    "；".join(f"{metric['label']}：{metric['display']}" for metric in row["exchange"]),
                ])
                if row["source_url"]:
                    lines.append(f"对局来源：{row['source_url']}")
            if not data["rows"]:
                lines.append("近期对局：暂无")
        else:
            match = data["match"]
            lines.extend([
                f"对局 ID：{match['id']}",
                f"地图：{match['map_name']} · 模式：{match['mode']}",
                f"时间：{match['started_at']} · 时长：{match['duration']}",
                f"胜方：{match['winner']}",
            ])
            for team in data["teams"]:
                lines.append(f"{team['name']} · {team['outcome']['label']} · {len(team['players'])} 人")
                for player in team["players"]:
                    lines.append(f"{player['name']} · 玩家 ID：{player['id']}")
                    lines.append("；".join(f"{metric['label']}：{metric['display']}" for metric in player["metrics"]))
                if not team["players"]:
                    lines.append("队员：暂无")
            if not data["teams"]:
                lines.append("分队与玩家：暂无")
        lines.extend(data["notes"])
        if data["stale"]:
            lines.append("旧缓存 · 以抓取时间为准")
        lines.extend([
            f"抓取时间：{data['fetched_at']}",
            f"来源更新：{data['source_updated_at']}",
            f"数据来源：{data['source_url'] or MISSING}",
        ])
        if data["player"]["source_url"] and data["player"]["source_url"] != data["source_url"]:
            lines.append(f"玩家来源：{data['player']['source_url']}")
        return "\n".join(lines)
