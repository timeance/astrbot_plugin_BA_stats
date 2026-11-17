"""Presentation/security behavior; fixed synthetic values, no browser or API."""

from copy import deepcopy
from datetime import datetime, timezone
from html.parser import HTMLParser
import re

from jinja2 import Environment
from markupsafe import Markup
import pytest

from render import Renderer


class VisibleHTML(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.in_body = False
        self.parts = []
        self.tags = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        if tag == "body":
            self.in_body = True

    def handle_endtag(self, tag):
        if tag == "body":
            self.in_body = False

    def handle_data(self, data):
        if self.in_body:
            self.parts.append(data)

    @property
    def text(self):
        return " ".join(self.parts)


def compact(value):
    return re.sub(r"\s+", "", value)


@pytest.fixture
def view():
    return {
        "kind": "overview",
        "player": {"id": "00076561198012345678", "name": "合成样本玩家"},
        "metrics": [
            {"label": "近期胜率", "value": 62.5, "format": "percent"},
            {"label": "实际样本", "value": 8, "format": "integer", "unit": "场"},
            {"label": "单位交换比", "value": 1.234},
            {"label": "价值交换比", "value": 2.789},
        ],
        "scope": "已取得的 8 场排位样本 · 非完整历史",
        "fetched_at": "2026-11-17T02:30:00Z",
        "source_updated_at": "2026-11-17T02:20:00+00:00",
        "source_url": "https://example.invalid/player/preview?scope=recent&n=8",
        "synthetic": True,
    }


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_identity_scope_precision_and_same_text_values(view, theme):
    renderer = Renderer(theme=theme)
    html = renderer.template(view)
    visible = VisibleHTML(html).text
    text = renderer.text(view)
    for value in [
        "00076561198012345678", "合成样本玩家", view["scope"],
        "62.50%", "8场", "1.23", "2.79", "2026-11-17 10:30 CST",
        "2026-11-17 10:20 CST", "合成样本", view["source_url"],
    ]:
        assert compact(value) in compact(visible)
        assert compact(value) in compact(text)
    assert f'data-theme="{theme}"' in html
    assert visible.index("合成样本玩家") < visible.index(view["scope"]) < visible.index("近期胜率")


@pytest.mark.parametrize("kind", ["overview", "recent", "match"])
def test_external_text_is_escaped_in_every_card(view, kind):
    attack = '<img src="https://example.invalid/x" onerror="alert(1)"> & "玩家"'
    view.update(
        kind=kind, title=attack, scope=attack, notes=[attack],
        player={"id": attack, "name": Markup(attack)},
        metrics=[{"label": attack, "value": attack, "unit": attack, "note": attack}],
        rows=[{"id": attack, "map_name": attack, "mode": attack}],
        match={"id": attack, "map_name": attack, "winner": attack},
        teams=[{"name": attack, "players": [{"id": attack, "name": attack}]}],
        common_units=[{"name": attack}],
    )
    renderer = Renderer()
    html = renderer.template(view)
    visible = VisibleHTML(html)
    assert "&lt;img" in html
    assert not any(tag in {"img", "script", "iframe"} for tag, _ in visible.tags)
    assert attack in visible.text
    assert attack in renderer.text(view)


def test_jinja_literal_nickname_survives_fixed_astrbot_wrapper(view):
    nickname = '{{7*7}} {% if true %}字面昵称{% endif %} {#注释也保留#} <玩家>&'
    view["player"]["name"] = nickname
    renderer = Renderer()
    html = renderer.template(view)
    wrapped = Environment(autoescape=True).from_string("{{ body | safe }}").render(body=html)
    assert nickname in VisibleHTML(wrapped).text
    assert nickname in renderer.text(view)
    assert "<玩家>" not in wrapped
    assert "{{7*7}}" in wrapped


@pytest.mark.parametrize("url", [
    "javascript:alert(1)", "data:text/html,test", "file:///C:/secret", "//example.invalid",
    "https://name:password@example.invalid/", "https://example.invalid\n/evil",
    "https:\\example.invalid", "https://", "https://example.invalid:99999/", "https://[broken/",
])
def test_unsafe_source_urls_are_omitted_everywhere(view, url):
    view.update(source_url=url, player={"id": "id", "name": "name", "source_url": url})
    view["kind"] = "recent"
    view["rows"] = [{"id": "match", "source_url": url}]
    renderer = Renderer()
    assert not any("href" in attrs for _, attrs in VisibleHTML(renderer.template(view)).tags)
    assert url not in renderer.text(view)
    assert "数据来源：暂无" in renderer.text(view)


def test_safe_http_links_are_escaped_not_loaded(view):
    renderer = Renderer()
    html = renderer.template(view)
    parsed = VisibleHTML(html)
    assert ("a", {"href": view["source_url"], "rel": "noreferrer noopener"}) in parsed.tags
    assert "&amp;n=8" in html
    assert not any(tag in {"img", "iframe", "script", "link"} for tag, _ in parsed.tags)
    assert "@import" not in html and "url(" not in html


@pytest.mark.parametrize("value", [None, "", " ", float("nan"), float("inf"), True])
def test_missing_metric_is_unknown_without_fake_zero_or_unit(view, value):
    view["metrics"] = [{"label": "ELO", "value": value, "unit": "分"}]
    renderer = Renderer()
    assert "ELO：暂无" in renderer.text(view)
    assert "暂无分" not in compact(VisibleHTML(renderer.template(view)).text)
    assert "0.00" not in renderer.text(view)


def test_real_zero_and_unknown_stay_distinct(view):
    view.update(kind="recent", rows=[{
        "id": "zero", "kills": 0, "losses": None, "destroyed_value": 0,
        "lost_value": None, "unit_exchange": None, "value_exchange": None,
        "duration_seconds": 0, "elo_delta": 0,
    }])
    text = Renderer().text(view)
    assert "摧毁单位：0 个" in text
    assert "损失单位：暂无" in text
    assert "摧毁价值：0.00" in text
    assert "损失价值：暂无" in text
    assert "价值交换比：暂无" in text
    assert "胜负暂无" in text
    assert "时长：0分00秒" in text
    assert "ELO 变化：+0.00" in text


def test_exchange_values_are_separate_and_not_recalculated(view):
    view.update(kind="recent", rows=[{
        "id": "ratios", "kills": 8, "losses": 2, "unit_exchange": 4,
        "destroyed_value": 2500.5, "lost_value": 2000.75, "value_exchange": 1.249,
    }])
    renderer = Renderer()
    html_text = compact(VisibleHTML(renderer.template(view)).text)
    text = renderer.text(view)
    for expected in ["摧毁单位：8 个", "损失单位：2 个", "单位交换比：4.00",
                     "摧毁价值：2500.50", "损失价值：2000.75", "价值交换比：1.25"]:
        assert expected in text
        assert compact(expected.replace("：", "")) in html_text


def test_stale_and_absent_timestamps_are_visible(view):
    view.update(stale=True, fetched_at=None, source_updated_at="2026-11-17T02:20:00")
    renderer = Renderer()
    for output in [renderer.text(view), VisibleHTML(renderer.template(view)).text]:
        assert "旧缓存" in output
        assert "抓取时间：暂无" in compact(output)
        assert "来源更新：暂无" in compact(output)


def test_timezone_aware_datetime_conversion(view):
    view["fetched_at"] = datetime(2026, 11, 17, 2, 30, tzinfo=timezone.utc)
    assert "2026-11-16 21:30 EST" in Renderer(timezone="America/New_York").text(view)


def test_recent_pagination_preserves_every_row_scope_and_input(view):
    view.update(kind="recent", rows=[{"id": f"match-{i:03}"} for i in range(20)])
    before = deepcopy(view)
    renderer = Renderer()
    pages = renderer.views(view)
    assert [len(page["rows"]) for page in pages] == [5, 5, 5, 5]
    assert [row["id"] for page in pages for row in page["rows"]] == [row["id"] for row in view["rows"]]
    assert "第 2/4 页 · 本页 6–10 场 / 共 20 场" in renderer.text(pages[1])
    assert "match-005" in renderer.text(pages[1])
    assert "match-000" not in renderer.text(pages[1])
    assert all(page["scope"] == view["scope"] and page["player"] == view["player"] for page in pages)
    pages[0]["rows"][0]["id"] = "changed"
    assert view == before


@pytest.mark.parametrize("kind,empty_label", [
    ("overview", "统计指标：暂无"), ("recent", "近期对局：暂无"), ("match", "分队与玩家：暂无"),
])
def test_empty_card_has_explicit_unknown_state(kind, empty_label):
    renderer = Renderer()
    view = {"kind": kind}
    assert empty_label in renderer.text(view)
    assert compact(empty_label) in compact(VisibleHTML(renderer.template(view)).text)
    assert len(renderer.views(view)) == 1


def test_variable_team_sizes_keep_all_players_and_no_placeholder_opponents(view):
    view.update(kind="match", match={"id": "000123", "map_name": "合成地图", "winner": "蓝队"}, teams=[
        {"name": "蓝队", "outcome": "win", "players": [{"id": f"a{i}", "name": f"蓝方{i}"} for i in range(3)]},
        {"name": "红队", "outcome": "loss", "players": [{"id": "b", "name": "红方0"}]},
        {"name": "未分配", "players": []},
    ])
    renderer = Renderer()
    html = renderer.template(view)
    assert html.count('class="participant"') == 4
    for expected in ["3 人", "1 人", "0 人", "队员：暂无", "蓝方2", "红方0", "000123"]:
        assert compact(expected) in compact(VisibleHTML(html).text)
        assert expected in renderer.text(view)


def test_long_nickname_is_not_truncated(view):
    name = "中文非常长的合成昵称MixedNickname_" * 20
    view["player"]["name"] = name
    renderer = Renderer()
    assert name in VisibleHTML(renderer.template(view)).text
    assert name in renderer.text(view)


def test_invalid_count_does_not_silently_round(view):
    view["metrics"] = [{"label": "实际样本", "value": 1.5, "format": "integer", "unit": "场"}]
    assert "实际样本：暂无" in Renderer().text(view)


@pytest.mark.parametrize("size", [0, 6, True, 2.5])
def test_page_size_rejects_unbounded_cards(view, size):
    with pytest.raises(ValueError):
        Renderer().views(view, page_size=size)


def test_unknown_theme_and_kind_are_configuration_errors():
    with pytest.raises(ValueError):
        Renderer(theme="other")
    with pytest.raises(ValueError):
        Renderer().template({"kind": "other"})
