"""One presentation contract for HTML cards and their plain-text fallback.

Service -> Renderer view contract:
  kind: "overview" | "recent" | "match"; title: optional display title.
  player: {id: str, name: str, source_url?: str}; required for player cards.
  metrics: [{label: str, value: number | str | None, unit?: str,
             format?: "integer" | "decimal" | "percent" | "signed", note?: str}].
    Numbers use two decimals, omitting .00 for exact integers; counts use integer. Percent values are
    already on the 0..100 scale. Strings are display text, never HTML.
  rows (recent): [{id, started_at, map_name, mode, outcome, duration_seconds,
    elo_delta, kills, losses, destroyed_value, lost_value, unit_exchange,
    value_exchange, source_url}]. Every field except id may be absent.
  match (match): {id, map_name, mode, started_at, duration_seconds, winner,
    roster_complete?: bool}.
  teams (match): [{name, outcome, players: [{id, name, elo, elo_before, elo_delta, kills,
    losses, destroyed_value, lost_value, unit_exchange, value_exchange}]}].
    Team sizes are unrestricted; never pad absent players with dummy rows.
  common_units (overview, optional): [{name: str, count?: int}].
  profile_rating (overview): {score: 1..10 | None, title, tone, sample_size,
    win_rate_sample: {value, wins, losses, sample_size},
    dimensions: [{key: kd|contrib|obj|outcome|win_rate, label, value: 0..100 | None}]}.
    The two sample sizes are independent. Missing axes never become zero.
  style_scope (overview): actual analysis sample label, e.g. 近100场风格.
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
from math import cos, pi, sin
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape

if __package__:
    from .models import PROFILE_AXES as _PROFILE_AXES
else:
    from models import PROFILE_AXES as _PROFILE_AXES

MISSING = "暂无"
_TITLES = {"overview": "玩家概览", "recent": "近期战绩", "match": "单局战报"}
_OUTCOMES = {"win": "胜利", "loss": "失利", "unknown": "胜负暂无"}
_MODES = {"ranked": "排位", "custom": "自定义", "unknown": "暂无"}
_ROLES = {"装甲": "armor", "步兵": "infantry", "侦察": "recon", "炮兵": "artillery",
          "防空": "air-defense", "直升机": "helicopter", "固定翼": "aircraft", "后勤/运输": "logistics"}
_MIN_WIN_RATE_SAMPLE = 5
_RECENT_WIN_RATE_WINDOW = 20
_FORMAL_TEAMS = ("A 队", "B 队")


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


def _count(value) -> int | None:
    number = _number(value)
    return int(number) if number is not None and number >= 0 and number == number.to_integral_value() else None


def _win_rate(value, sample, wins) -> tuple[str, str, Decimal | None]:
    """Keep the known-result count separate from the rating's usable matches."""
    number = _number(value)
    sample, wins = _count(sample), _count(wins)
    if sample is not None and wins is not None and wins > sample:
        return MISSING, "", None
    if not sample:
        return MISSING, "0 场有赛果" if sample == 0 else "", None
    note = f"{sample} 场有赛果"
    if sample < _MIN_WIN_RATE_SAMPLE:
        return (f"{wins} 胜 {sample - wins} 负" if wins is not None else MISSING), note, None
    if number is None or not 0 <= number <= 100:
        return MISSING, note, None
    if wins is not None:
        note = f"{wins} 胜 {sample - wins} 负 · {note}"
    return _numeric(number, "percent", compact=True) + "%", note, number


def _numeric(value, style="decimal", *, compact=False) -> str:
    parsed = _number(value)
    if parsed is None:
        return MISSING
    if style == "integer":
        return format(int(parsed), ",d" if compact else "d") if parsed == parsed.to_integral_value() else MISSING
    if style == "decimal" and parsed == parsed.to_integral_value():
        return format(int(parsed), ",d" if compact else "d")
    sign = "+" if style == "signed" else ""
    result = format(parsed, sign + ("," if compact else "") + ".2f")
    return result.rstrip("0").rstrip(".") if compact else result


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


def _outcome(value, conflict=False) -> dict:
    if conflict is True:
        return {"key": "conflict", "label": "赛果冲突"}
    key = value if isinstance(value, str) and value in _OUTCOMES else "unknown"
    return {"key": key, "label": _OUTCOMES[key]}


class Renderer:
    """Render self-contained cards without network access."""

    WIDTHS = {"overview": 640, "recent": 800, "match": 900}

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
    def _metric(metric: Mapping, *, compact=False) -> dict:
        value = metric.get("value")
        style = metric.get("format", "decimal")
        if style not in {"integer", "decimal", "percent", "signed"}:
            style = "decimal"
        formatted = _plain(value) if isinstance(value, str) else _numeric(value, style, compact=compact)
        unit = _plain(metric.get("unit"), "")
        note = _plain(metric.get("note"), "")
        if style == "percent" and "sample_size" in metric and "wins" in metric:
            display, sample_note, _ = _win_rate(value, metric.get("sample_size"), metric.get("wins"))
            formatted, unit = (display[:-1], "%") if display.endswith("%") else (display, "")
            return {"label": _plain(metric.get("label")), "value": formatted, "unit": unit,
                    "display": display, "note": " · ".join(dict.fromkeys(part for part in (sample_note, note) if part))}
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
            "note": note,
        }

    @staticmethod
    def _profile(value, recent_rows) -> dict:
        rating = _mapping(value)
        score = _number(rating.get("score"))
        if score is not None and not 1 <= score <= 10:
            score = None
        tone = _plain(rating.get("tone"), "")
        supplied = {item.get("key"): item for item in _records(rating.get("dimensions"))
                    if isinstance(item.get("key"), str)}

        center = (240, 162)
        radius = 112

        def point(index, radius):
            angle = -pi / 2 + index * 2 * pi / 5
            return (center[0] + radius * cos(angle), center[1] + radius * sin(angle))

        def coordinates(points):
            return " ".join(f"{x:.2f},{y:.2f}" for x, y in points)

        axes = []
        positions = ((240, 25, "middle"), (387, 121, "start"), (335, 269, "start"),
                     (145, 269, "end"), (93, 121, "end"))
        results = _mapping(rating.get("win_rate_sample"))
        win_window = _count(results.get("window_size"))
        if win_window is None:
            win_window = min(len(_records(recent_rows)), _RECENT_WIN_RATE_WINDOW)
        for index, (key, label) in enumerate(_PROFILE_AXES):
            dimension = supplied.get(key, {})
            number = _number(dimension.get("value"))
            if number is not None and not 0 <= number <= 100:
                number = None
            display, note = _numeric(number, compact=True), ""
            has_sample = False
            if key == "win_rate":
                display, note, number = _win_rate(results.get("value", number), results.get("sample_size"),
                                                  results.get("wins"))
                has_sample = bool(_count(results.get("sample_size")))
                if has_sample and win_window:
                    note = f"近 {win_window} 场 · {note}" if note else f"近 {win_window} 场"
            x, y, anchor = positions[index]
            xy = point(index, float(number) * radius / 100) if number is not None else None
            axes.append({"key": key, "label": label, "display": display, "note": note,
                         "x": x, "y": y, "anchor": anchor,
                         "line": coordinates([center, point(index, radius)]),
                         "point": xy, "has_fact": display != MISSING or has_sample})
        available = sum(axis["point"] is not None for axis in axes)
        sample = _count(rating.get("sample_size"))
        return {"score": format(score, ".1f") if score is not None else MISSING,
                "title": _plain(rating.get("title"), "") if score is not None else "",
                "tone": tone if score is not None and tone in {"low", "medium", "high"} else "medium",
                "sample": f"{sample} 场有效" if sample is not None else "",
                "axes": axes,
                "facts": [axis for axis in axes if axis["has_fact"]],
                "radar": available >= 4,
                "rings": [coordinates([point(index, radius * step / 4) for index in range(5)]) for step in range(1, 5)],
                "polygon": coordinates([axis["point"] for axis in axes]) if available == len(axes) else "",
                }

    def _prepare(self, view: dict) -> dict:
        kind = view.get("kind", "overview")
        if kind not in _TITLES:
            raise ValueError("unknown card kind")
        player = _mapping(view.get("player"))
        match = _mapping(view.get("match"))
        metrics = []
        for item in _records(view.get("metrics")):
            if kind == "overview" and item.get("label") == "分析样本胜率":
                window = _count(item.get("window_size"))
                if window is None:
                    window = _count(_mapping(player.get("analysis")).get("matchCount"))
                if window:
                    item = {**item, "label": f"近{window}场胜率"}
            metrics.append(self._metric(item, compact=kind != "match"))
        prepared = {
            "kind": kind,
            "width": self.WIDTHS[kind],
            "github_logo": self._github_logo,
            "title": _plain(view.get("title"), _TITLES[kind]),
            "theme": self.theme,
            "player": {
                "name": _plain(player.get("name")),
                "id": _plain(player.get("id")),
                "source_url": _safe_url(player.get("source_url")),
            },
            "metrics": metrics,
            "scope": _plain(view.get("scope")),
            "fetched_at": self._time(view.get("fetched_at")),
            "source_updated_at": self._time(view.get("source_updated_at")),
            "stale": view.get("stale") is True,
            "source_url": _safe_url(view.get("source_url")) or _safe_url(player.get("source_url")),
            "synthetic": view.get("synthetic") is True,
            "notes": [_plain(note) for note in view.get("notes", [])] if isinstance(view.get("notes"), (list, tuple)) else [],
            "common_units": [
                {"name": _plain(item.get("name")), "count": _numeric(item.get("count"), "integer", compact=True),
                 "damage": _numeric(item.get("damage"), compact=True), "roi": _numeric(item.get("roi"), compact=True)}
                for item in _records(view.get("common_units"))
            ],
            "style_scope": _plain(view.get("style_scope"), ""),
            "categories": [{"name": _plain(item.get("name")), "percentage": self._metric(
                {"value": item.get("percentage"), "format": "percent"}, compact=True)["display"]}
                           for item in _records(view.get("categories"))],
            "map_stats": [{"name": _plain(item.get("name")), "count": _numeric(item.get("count"), "integer", compact=True),
                           "win_rate": _numeric(item.get("win_rate"), compact=True)}
                          for item in _records(view.get("map_stats"))],
            "rows": [],
            "teams": [],
            "pagination": "",
        }
        if kind == "overview":
            prepared["profile_rating"] = self._profile(view.get("profile_rating"), view.get("rows"))
        for item in _records(view.get("rows")):
            score = _number(item.get("score"))
            kd = item.get("value_exchange")
            prepared["rows"].append({
                "score": format(score, ".1f") if score is not None else MISSING,
                "score_band": item.get("score_band") if item.get("score_band") in {"low", "medium", "high"} else "medium",
                "id": _plain(item.get("id")),
                "started_at": self._time(item.get("started_at")),
                "map_name": _plain(item.get("map_name")),
                "mode": _MODES.get(_plain(item.get("mode")), _plain(item.get("mode"))),
                "outcome": _outcome(item.get("outcome"), item.get("outcome_conflict")),
                "duration": self._duration(item.get("duration_seconds")),
                "elo_delta": _numeric(item.get("elo_delta"), "signed", compact=True),
                "kd": _plain(kd) if isinstance(kd, str) else _numeric(kd, compact=True),
                "source_url": _safe_url(item.get("source_url")),
            })
        prepared["match"] = {
            "id": _plain(match.get("id")),
            "map_name": _plain(match.get("map_name")),
            "mode": _MODES.get(_plain(match.get("mode")), _plain(match.get("mode"))),
            "started_at": self._time(match.get("started_at")),
            "duration": self._duration(match.get("duration_seconds")),
            "winner": _plain(match.get("winner")),
            "target_outcome": _outcome(match.get("target_outcome"), match.get("target_outcome_conflict")),
        }
        prepared["winner_side"] = dict(zip(_FORMAL_TEAMS, ("a", "b"))).get(prepared["match"]["winner"], "")
        prepared["target_member"] = None
        prepared["highlights"] = [
            {key: _plain(row.get(key)) for key in ("label", "value", "names")}
            for row in _records(view.get("highlights"))[:3]
        ]
        raw_teams = _records(view.get("teams"))
        side_members = {}
        target_id = prepared["player"]["id"]
        for team in raw_teams:
            members = _records(team.get("players"))
            side = ({"0": "a", "1": "b"}.get(_plain(team.get("id")), "neutral") if "id" in team
                    else dict(zip(_FORMAL_TEAMS, ("a", "b"))).get(team.get("name"), "neutral"))
            if side != "neutral":
                side_members[side] = members
            team_metrics = {
                _plain(item.get("label")): {
                    **self._metric(item), "number": _number(item.get("value")),
                }
                for item in _records(team.get("metrics"))
            }
            summary = []
            # The service's mean ELO is the post-match value, not the prediction's
            # pre-match input. Never reconstruct a missing pre-match rating.
            for label, field in (("赛前平均 ELO", "elo_before"), ("平均 ELO 变化", "elo_delta")):
                values = [_number(item.get(field)) for item in members]
                complete = bool(values) and all(value is not None for value in values)
                summary.append(self._metric({"label": label, "value": sum(values) / len(values) if complete else None,
                                             "format": "signed" if field == "elo_delta" else "decimal"}))
            for label in ("赛后平均 ELO", "摧毁分"):
                summary.append(team_metrics.get(label, self._metric({"label": label})))
            composition = []
            for item in _records(team.get("composition")):
                percentage = _number(item.get("percentage"))
                composition.append({"name": _plain(item.get("name")),
                                    "number": percentage if percentage is not None and 0 <= percentage <= 100 else None})
            total = sum((item["number"] or 0) for item in composition)
            # Service omits zero-spend roles from a complete composition. Only
            # a full, unqualified 100% distribution makes that omission zero.
            composition_complete = (bool(composition) and all(item["number"] is not None for item in composition)
                                    and abs(total - 100) <= Decimal("0.01") and not team.get("composition_note"))
            roster = []
            for item in sorted(members, key=lambda row: (
                _number(row.get("score")) is None, -(_number(row.get("score")) or 0)
            )):
                is_target = target_id != MISSING and _plain(item.get("id")) == target_id
                score = _number(item.get("score"))
                destroyed, lost = (_number(item.get(field)) for field in ("destroyed_value", "lost_value"))
                member = {
                    "name": _plain(item.get("name")), "is_target": is_target,
                    "score": format(score, ".1f") if score is not None else MISSING,
                    "score_band": item.get("score_band") if item.get("score_band") in ("low", "medium", "high") else "",
                    "net": _numeric(destroyed - lost if destroyed is not None and lost is not None else None,
                                    "signed", compact=True),
                    "honors": [{"name": _plain(h["name"]), "positive": h.get("positive") is True,
                                "reason": _plain(h.get("reason"), "")}
                               for h in _records(item.get("honors")) if h.get("name")][:2],
                }
                for field, raw, style in (("elo", "elo", "decimal"), ("elo_delta", "elo_delta", "signed"),
                                          ("kills", "kills", "integer"), ("deaths", "losses", "integer"),
                                          ("caps", "objectives", "integer"), ("destruction", "destroyed_value", "decimal"),
                                          ("losses", "lost_value", "decimal")):
                    member[field] = _numeric(item.get(raw), style, compact=True)
                roster.append(member)
                if is_target:
                    prepared["target_member"] = member
            prepared["teams"].append({
                "name": _plain(team.get("name")),
                "side": side,
                "outcome": _outcome(team.get("outcome")),
                "metrics": team_metrics,
                "summary": [summary[index] for index in (0, 2, 1, 3)],
                "composition": composition,
                "composition_complete": composition_complete,
                "composition_note": _plain(team.get("composition_note"), ""),
                "players": roster,
            })
        team_by_side = {team["side"]: team for team in prepared["teams"]}
        sides = [team_by_side.get(side, {}) for side in ("a", "b")]
        prepared["formal_teams"] = [team for team in sides if team]
        prepared["teams"] = prepared["formal_teams"] + [team for team in prepared["teams"] if team["side"] == "neutral"]
        if prepared["winner_side"]:
            prepared["teams"].sort(key=lambda team: team["side"] != prepared["winner_side"])
        a_members, b_members = (side_members.get(side, []) for side in ("a", "b"))
        comparable_rosters = match.get("roster_complete") is not False and len(a_members) == len(b_members)
        prediction = _mapping(view.get("prediction"))
        a, b = _number(prediction.get("a")), _number(prediction.get("b"))
        prepared["prediction"] = None
        if (comparable_rosters and a_members
                and all(_number(member.get("elo_before")) is not None or
                        (_number(member.get("elo")) is not None and _number(member.get("elo_delta")) is not None)
                        for member in a_members + b_members)
                and a is not None and b is not None and 0 <= a <= 100 and 0 <= b <= 100
                and abs(a + b - 100) <= Decimal("0.01")):
            rounded = int(a + Decimal("0.5"))
            prepared["prediction"] = {"a": rounded, "b": 100 - rounded, "width": float(a)}
        prepared["prediction_note"] = "按赛前 ELO 估算" if prepared["prediction"] else "暂无可用预测"

        # ELO is shown in the team header. The scoreboard uses net spend after refunds.
        prepared["comparisons"] = []
        for label in ("摧毁分", "击杀", "伤害", "出兵净花费", "出兵数量", "补给", "占点", "单位存活率"):
            pair = [team.get("metrics", {}).get(label, {}) for team in sides]
            prepared["comparisons"].append({
                "label": label, "values": [m.get("display", MISSING) for m in pair],
                "width": _split_width([m.get("number") for m in pair]),
            })
        category_names = list(dict.fromkeys(category["name"] for team in sides
                                             for category in team.get("composition", [])))
        category_names.sort(key=lambda name: list(_ROLES).index(name) if name in _ROLES else len(_ROLES))
        prepared["composition_rows"] = []
        for name in category_names:
            values = [next((category["number"] for category in team.get("composition", []) if category["name"] == name),
                           Decimal(0) if team.get("composition_complete") else None) for team in sides]
            width = (_split_width(values)
                     if comparable_rosters and all(team.get("composition_complete") for team in sides) else None)
            prepared["composition_rows"].append({
                "label": name, "role": _ROLES.get(name, "other"),
                "values": [_numeric(v, "percent", compact=True) + "%" if v is not None else MISSING for v in values],
                "width": width,
            })
        page = _number(view.get("page"))
        page_count = _number(view.get("page_count"))
        row_total = _number(view.get("row_total"))
        row_start = _number(view.get("row_start"))
        if (kind == "recent" and page_count is not None and page_count > 1
                and all(value is not None and value == value.to_integral_value()
                        for value in (page, page_count, row_total, row_start))):
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
            profile = data["profile_rating"]
            lines.append(" · ".join(part for part in (f"综合评分 {profile['score']}", profile["title"], profile["sample"])
                                    if part))
            for offset in range(0, len(data["metrics"]), 2):
                lines.append(metrics(data["metrics"][offset:offset + 2]))
            if not data["metrics"]:
                lines.append("统计指标：暂无")
            if profile["facts"]:
                lines.append("五维表现 · 近期：" + " · ".join(
                    f"{axis['label']} {axis['display']}" + (f"（{axis['note']}）" if axis["note"] else "")
                    for axis in (profile["axes"] if profile["radar"] else profile["facts"])))
            else:
                lines.append("五维表现：暂无数据")
            if data["style_scope"]:
                lines.append(data["style_scope"] + ("：暂无" if not data["categories"] else ""))
            if data["categories"]:
                lines.append("兵种：" + " · ".join(f"{c['name']} {c['percentage']}" for c in data["categories"]))
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
                lines.append(f"评分 {row['score']} · {row['map_name']} · {row['outcome']['label']} · ELO {row['elo_delta']}")
                lines.append(f"分值 K/D {row['kd']} · 时长 {row['duration']} · #{row['id']}")
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
            target = data["target_member"]
            if target:
                lines.append(f"本局评分 {target['score']} · 净交换 {target['net']} · ELO {target['elo']} ({target['elo_delta']})")
            prediction = data["prediction"]
            lines.append(
                f"赛前预测：A 队 {prediction['a']}% · B 队 {prediction['b']}%（{data['prediction_note']}）"
                if prediction else f"赛前预测：{data['prediction_note']}"
            )
            for team in data["formal_teams"]:
                lines.append(f"{team['name']} · {len(team['players'])} 人 · {team['outcome']['label']}")
                lines.append(metrics(team["summary"]))
            lines.append("兵种构成（按基础出动价值估算 · A 队 / B 队）")
            for row in data["composition_rows"]:
                lines.append(f"{row['label']}：" + " / ".join(row["values"]))
            if not data["composition_rows"]:
                lines.append("暂无完整兵种构成")
            for team in data["formal_teams"]:
                if team['composition_note']:
                    lines.append(f"{team['name']}：{team['composition_note']}")
            lines.append("双方对比（A 队 / B 队）")
            known_rows = [row for row in data["comparisons"] if any(v != MISSING for v in row["values"])]
            for row in known_rows:
                lines.append(f"{row['label']}：" + " / ".join(row["values"]))
            if not known_rows:
                lines.append("暂无对比数据")
            if data["highlights"]:
                lines.append("本局亮点")
                lines.extend(f"{h['label']} · {h['names']} {h['value']}" for h in data["highlights"])
            if target and target["honors"]:
                lines.append("个人亮点")
                lines.extend(f"{h['name']} · {h['reason']}" for h in target["honors"])
            lines.append("队伍玩家")
            for team in data["teams"]:
                heading = f"{team['name']} · {len(team['players'])} 人"
                if team['outcome']['key'] != 'unknown':
                    heading += " · " + team['outcome']['label']
                lines.extend(["", heading])
                for player in team["players"]:
                    name = " · ".join([player["name"], *(h["name"] for h in player["honors"])])
                    lines.append(f"{name} · 评分 {player['score']} · 净交换 {player['net']} · "
                                 f"击杀 / 阵亡 {player['kills']} / {player['deaths']} · 占点 {player['caps']}")
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
