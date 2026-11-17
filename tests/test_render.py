"""Presentation/security behavior; fixed synthetic values, no browser or API."""

from copy import deepcopy
import base64
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
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
        "62.5%", "8场", "1.23", "2.79", "合成样本",
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
    assert not any(tag in {"script", "iframe"} or (tag == "img" and not attrs.get("src", "").startswith("data:image/svg+xml;base64,")) for tag, attrs in visible.tags)
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
    assert all(attrs.get("href") != url for _, attrs in VisibleHTML(renderer.template(view)).tags)
    assert url not in renderer.text(view)
    assert "BATrace：" not in renderer.text(view)


def test_safe_http_links_are_escaped_not_loaded(view):
    renderer = Renderer()
    html = renderer.template(view)
    parsed = VisibleHTML(html)
    assert any(attrs.get("href") == "https://github.com/timeance/astrbot_plugin_BA_stats" for _, attrs in parsed.tags)
    assert view["source_url"] in renderer.text(view)
    assert not any(tag in {"iframe", "script", "link"} for tag, _ in parsed.tags)
    assert all(attrs["src"].startswith("data:") for tag, attrs in parsed.tags if tag == "img")
    assert "@import" not in html and "url(" not in html


@pytest.mark.parametrize("value", [None, "", " ", float("nan"), float("inf"), True])
def test_missing_metric_is_unknown_without_fake_zero_or_unit(view, value):
    view["metrics"] = [{"label": "ELO", "value": value, "unit": "分"}]
    renderer = Renderer()
    assert "ELO：暂无" in renderer.text(view).splitlines()
    assert re.findall(r'<p class="metric-number">(.*?)</p>', renderer.template(view), re.S) == ["暂无"]


def test_real_zero_and_unknown_stay_distinct(view):
    view.update(kind="recent", rows=[{
        "id": "zero", "kills": 0, "losses": None, "destroyed_value": 0,
        "lost_value": None, "unit_exchange": None, "value_exchange": None,
        "duration_seconds": 0, "elo_delta": 0,
    }])
    text = Renderer().text(view)
    assert "击杀 / 阵亡 0 / 暂无" in text
    assert "摧毁 / 损失 0 / 暂无" in text
    assert "分值 K/D 暂无" in text
    assert "胜负暂无" in text
    assert "0分00秒" in text
    assert "ELO +0" in text


def test_exchange_values_are_separate_and_not_recalculated(view):
    view.update(kind="recent", rows=[{
        "id": "ratios", "kills": 8, "losses": 2, "unit_exchange": 4,
        "destroyed_value": 2500.5, "lost_value": 2000.75, "value_exchange": 1.249,
    }])
    renderer = Renderer()
    html_text = compact(VisibleHTML(renderer.template(view)).text)
    text = renderer.text(view)
    for expected in ["击杀 / 阵亡 8 / 2", "摧毁 / 损失 2,500.5 / 2,000.75", "分值 K/D 1.25"]:
        assert expected in text
        assert compact(expected.replace("：", "")) in html_text


def test_stale_and_absent_timestamps_are_visible(view):
    view.update(stale=True, fetched_at=None, source_updated_at="2026-11-17T02:20:00")
    renderer = Renderer()
    for output in [renderer.text(view), VisibleHTML(renderer.template(view)).text]:
        assert "旧缓存" in output
        assert "抓取时间：暂无" in compact(output)
        assert "来源更新" not in output


def test_timezone_aware_datetime_conversion(view):
    view["stale"] = True
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


def test_fresh_report_omits_empty_sections_and_repeated_provenance(view):
    renderer = Renderer()
    for output in (renderer.text(view), VisibleHTML(renderer.template(view)).text):
        assert "常用单位" not in output
        assert "抓取时间" not in output and "来源更新" not in output
        assert output.count(view["source_url"]) <= 1


def test_analysis_units_maps_and_categories_share_values(view):
    view.update(style_scope="近83场风格", categories=[{"name": "固定翼", "percentage": 31},
                                                     {"name": "防空", "percentage": None}],
                common_units=[{"name": "Tu-95", "count": 72, "damage": 11683, "roi": 1.62}],
                map_stats=[{"name": "Coast", "count": 8, "win_rate": 50},
                           {"name": "Other", "count": 0, "win_rate": None}])
    view["metrics"].append({"label": "单局伤害交换比", "value": 1.3})
    renderer = Renderer()
    for output in (renderer.text(view), VisibleHTML(renderer.template(view)).text):
        for value in ("近83场风格", "固定翼 31%", "防空 暂无", "Tu-95", "72", "11,683", "1.62", "Coast", "50%",
                      "Other", "单局伤害交换比"):
            assert compact(value) in compact(output)
        assert "暂无%" not in compact(output)


@pytest.fixture
def profile():
    return {"score": 5.8, "title": "泯然众人", "tone": "medium", "sample_size": 4,
            "dimensions": [{"key": key, "value": value}
                           for key, value in (("win_rate", 0), ("outcome", 71), ("obj", 16),
                                              ("contrib", 59), ("kd", 37))]}


@pytest.mark.parametrize("kind", ["overview", "recent", "match"])
def test_title_and_five_distinct_axes_belong_only_to_overview(view, profile, kind):
    view.update(kind=kind, profile_rating=profile, rows=[{"id": "42", "title": profile["title"], "score": 5.8}])
    renderer = Renderer()
    parsed = VisibleHTML(renderer.template(view))
    for output in (parsed.text, renderer.text(view)):
        for value in ("泯然众人", "五维表现", "分值 K/D 37", "摧毁贡献 59", "占点贡献 16", "胜负预期 71", "实际胜率 0%"):
            assert (compact(value) in compact(output)) == (kind == "overview")
    assert sum(attrs.get("class") == "radar-area" for _, attrs in parsed.tags) == (kind == "overview")
    assert sum(attrs.get("class") == "radar-dot" for _, attrs in parsed.tags) == (5 if kind == "overview" else 0)


@pytest.mark.parametrize("value", [None, float("nan"), -1, 101])
def test_missing_radar_axis_keeps_real_zero_without_filling_polygon(view, profile, value):
    profile["dimensions"][1]["value"] = value
    profile["score"] = None
    view["profile_rating"] = profile
    renderer = Renderer()
    parsed = VisibleHTML(renderer.template(view))
    for output in (parsed.text, renderer.text(view)):
        assert "综合评分暂无" in compact(output)
        assert "胜负预期暂无" in compact(output) and "实际胜率0%" in compact(output)
        assert profile["title"] not in output
    assert not any(attrs.get("class") == "radar-area" for _, attrs in parsed.tags)
    assert sum(attrs.get("class") == "radar-dot" for _, attrs in parsed.tags) == 4


def test_recent_score_precedes_match_and_has_color_without_title(view):
    view.update(kind="recent", rows=[{"id":"42", "map_name":"Coast", "score":8.6, "score_band":"high"}])
    renderer = Renderer()
    html = renderer.template(view)
    assert 'class="score high"' in html
    text = renderer.text(view)
    assert text.index("评分 8.6") < text.index("Coast") < text.index("#42")
    assert "龙蛆" not in text and "称号" not in text


def test_match_retains_known_target_result_and_fractional_resource_values(view):
    view.update(kind="match", match={"id": "42", "target_outcome": "loss"}, teams=[
        {"name": "A 队", "players": [{"id": view["player"]["id"], "name": "查询玩家",
                                      "objectives": 3, "spawned_value": 1234.5, "supply": 12.5}]},
    ])
    renderer = Renderer()
    for output in (renderer.text(view), VisibleHTML(renderer.template(view)).text):
        for value in ("失利", "1234.50", "12.50", "占点"):
            assert value in output


def test_match_sections_and_footer_are_shared_across_cards(view):
    view.update(kind="match", match={"id":"42"}, prediction={"a":64,"b":36}, teams=[
        {"name":"A 队", "metrics":[{"label":"击杀", "value":234,"format":"integer"}],
         "composition":[{"name":"步兵", "percentage":60}], "players":[{"id":"a","name":"甲"}]},
        {"name":"B 队", "metrics":[{"label":"击杀", "value":227,"format":"integer"}],
         "composition":[{"name":"装甲", "percentage":40}], "players":[{"id":"b","name":"乙"}]},
    ])
    renderer = Renderer()
    html = renderer.template(view)
    assert html.index("赛前预测") < html.index("兵种构成") < html.index("双方对比") < html.index("队伍玩家") < html.index('class="team"')
    assert 'class="match-columns"' in html
    for output in (renderer.text(view), VisibleHTML(html).text):
        for value in ("64.00%", "36.00%", "234", "227", "步兵", "60.00%", "甲", "乙"):
            assert value in output
    assert "timeance/astrbot_plugin_BA_stats" in html and "数据来源 BATracer" in html
    assert "data:image/svg+xml;base64," in html


@pytest.mark.parametrize("kind", ["overview", "recent", "match"])
@pytest.mark.parametrize("theme,color", [("light", "black"), ("dark", "white")])
def test_official_github_mark_is_embedded_in_every_card(view, kind, theme, color):
    view["kind"] = kind
    html = Renderer(theme=theme).template(view)
    images = [attrs for tag, attrs in VisibleHTML(html).tags if tag == "img"]
    assert len(images) == 1 and images[0]["alt"] == "GitHub Logo"
    logo = base64.b64decode(images[0]["src"].split(",", 1)[1])
    assert logo == (Path(__file__).parents[1] / "assets" / f"github-mark-{color}.svg").read_bytes()
    assert "timeance/astrbot_plugin_BA_stats" in html and "数据来源 BATracer" in html


def test_renderer_defaults_to_light():
    assert 'data-theme="light"' in Renderer().template({"kind": "overview"})


@pytest.mark.parametrize("values,expected", [
    ((3, 1), 75), ((0, 3), 0), ((3, 0), 100),
    ((1.005, 1.004), 1.005 / 2.009 * 100),
    ((0, 0), None), ((None, 3), None), ((3, None), None),
])
def test_comparison_uses_one_continuous_bar_and_raw_denominator(view, values, expected):
    view.update(kind="match", teams=[
        {"name": name, "metrics": [{"label": "伤害", "value": value}]}
        for name, value in zip(("A 队", "B 队"), values)
    ])
    html = Renderer().template(view)
    tags = VisibleHTML(html).tags
    tracks = [attrs for tag, attrs in tags if "comparison-track" in attrs.get("class", "").split()]
    segments = [attrs for tag, attrs in tags if "segment" in attrs.get("class", "").split()]
    assert len(tracks) == 1
    if expected is None:
        assert segments == [] and "no-comparison" in tracks[0]["class"]
    else:
        widths = [float(attrs["style"].removeprefix("width:").removesuffix("%")) for attrs in segments]
        assert widths == pytest.approx([expected, 100 - expected])
        assert [attrs["class"] for attrs in segments] == ["segment side-a", "segment side-b"]
    if None in values:
        assert "暂无" in tracks[0]["aria-label"]
    elif values == (0, 0):
        assert "A 队 0，B 队 0" in tracks[0]["aria-label"]


def test_composition_compares_each_original_share_with_one_shared_bar(view):
    view.update(kind="match", teams=[
        {"name": "A 队", "composition": [{"name": "装甲", "percentage": 40}, {"name": "步兵", "percentage": 60}]},
        {"name": "B 队", "composition": [{"name": "装甲", "percentage": 20}, {"name": "步兵", "percentage": 30}]},
    ])
    html = Renderer().template(view)
    tags = VisibleHTML(html).tags
    tracks = [attrs for _, attrs in tags if attrs.get("role") == "img"]
    segments = [attrs for _, attrs in tags if "role-a" in attrs.get("class", "") or "role-b" in attrs.get("class", "")]
    assert len(tracks) == 2 and len(segments) == 4
    assert [attrs["class"] for attrs in segments] == ["segment role-armor role-a", "segment role-armor role-b",
                                                      "segment role-infantry role-a", "segment role-infantry role-b"]
    widths = [float(attrs["style"].removeprefix("width:").removesuffix("%")) for attrs in segments]
    assert widths == pytest.approx([100 * 40 / 60, 100 * 20 / 60, 100 * 60 / 90, 100 * 30 / 90])
    assert "A 队 40.00%，B 队 20.00%" in tracks[0]["aria-label"]
    assert "A 队 60.00%，B 队 30.00%" in tracks[1]["aria-label"]
    assert "左 A 队（深）· 右 B 队（浅）" in VisibleHTML(html).text
    assert "composition-stack" not in html


@pytest.mark.parametrize("a,b,expected", [(0, 0, None), (None, 20, None), (20, None, None), (0, 20, 0)])
def test_composition_distinguishes_zero_and_missing_share(view, a, b, expected):
    view.update(kind="match", teams=[
        {"name": name, "composition": [{"name": "装甲", "percentage": value}]}
        for name, value in zip(("A 队", "B 队"), (a, b))
    ])
    tags = VisibleHTML(Renderer().template(view)).tags
    segments = [attrs for _, attrs in tags if "segment" in attrs.get("class", "").split()]
    track = next(attrs for _, attrs in tags if attrs.get("role") == "img")
    if expected is None:
        assert not segments and "no-comparison" in track["class"]
    else:
        assert float(segments[0]["style"].removeprefix("width:").removesuffix("%")) == expected
    assert ("暂无" in track["aria-label"]) is (a is None or b is None)
    if a == b == 0:
        assert "A 队 0.00%，B 队 0.00%" in track["aria-label"]


def test_only_complete_composition_can_supply_an_omitted_zero_category(view):
    view.update(kind="match", teams=[
        {"name": "A 队", "composition": [{"name": "步兵", "percentage": 100}]},
        {"name": "B 队", "composition": [{"name": "装甲", "percentage": 30}]},
    ])
    tracks = [attrs for _, attrs in VisibleHTML(Renderer().template(view)).tags if attrs.get("role") == "img"]
    assert "A 队 0.00%，B 队 30.00%" in tracks[0]["aria-label"]
    assert "A 队 100.00%，B 队 暂无" in tracks[1]["aria-label"]


def test_unknown_composition_is_not_rendered_as_zero_or_renormalized(view):
    view.update(kind="match", teams=[{"name": "A 队", "composition": [], "composition_note": "1 人缺单位数据，构成暂无"}])
    html = Renderer().template(view)
    assert not any(attrs.get("role") == "img" for _, attrs in VisibleHTML(html).tags)
    assert "暂无完整兵种构成" in VisibleHTML(html).text
    assert "1 人缺单位数据" in VisibleHTML(html).text


def test_team_summary_distinguishes_post_match_elo_and_preserves_unknown_result(view):
    view.update(kind="match", prediction={"a": 60, "b": 40}, teams=[
        {"name": "A 队", "outcome": "win", "metrics": [{"label": "赛后平均 ELO", "value": 1500}, {"label": "摧毁分", "value": 5000}],
         "players": [{"elo": 1400, "elo_delta": 10}, {"elo": 1600, "elo_delta": None}]},
        {"name": "B 队", "outcome": "unknown", "players": [{"elo": 1500, "elo_delta": 20}]},
    ])
    html = Renderer().template(view)
    summary = html.split('<div class="team-summaries">', 1)[1].split('<div class="match-columns">', 1)[0]
    text = compact(VisibleHTML('<body>' + summary + '</body>').text)
    assert "赛前平均ELO暂无" in text and "赛后平均ELO1500" in text
    assert "平均ELO变化暂无" in text and "平均ELO变化+20.00" in text
    assert "胜负暂无" in text and text.count("胜利") == 1
    assert html.index('class="prediction"') < html.index('class="team-summaries"') < html.index('class="match-columns"')
