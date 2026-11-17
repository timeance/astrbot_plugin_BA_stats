"""Build labelled synthetic card previews, with optional local-only Chromium.

Run from the repository using the session's .venv/Scripts/python.exe -B:
  tools/render_previews.py                 # HTML + matching text only
  tools/render_previews.py --screenshots   # production, 390px reflow, and 390px chat previews

Chromium must already be installed under the session's .playwright directory.
No installation, server, external page, account, or live API is used. Browser
contexts are offline and all requests are aborted as an additional boundary.
All outputs, browser temporary profiles and the manifest stay under the
selected output directory. Images show synthetic samples, never real players.
"""

from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import tempfile

sys.dont_write_bytecode = True
REPOSITORY = Path(__file__).resolve().parents[1]
SESSION = REPOSITORY.parent
OUTPUT = SESSION / "tmp" / "report-readability" / "previews"
sys.path.insert(0, str(REPOSITORY))

from render import Renderer

PHONE_WIDTH = 390


def sample_views() -> dict[str, dict]:
    """Deterministic synthetic data, including long names and missing fields."""
    base = {
        "player": {"id": "sample-000001", "name": "蓝线观察员"},
        "scope": "近期5场 · 11-13—11-17",
        "fetched_at": "2026-11-17T04:10:00Z",
        "source_updated_at": "2026-11-17T04:00:00Z",
        "source_url": "https://example.invalid/synthetic-preview",
        "synthetic": True,
    }
    overview = deepcopy(base)
    overview["player"]["analysis"] = {"matchCount": 83}
    overview.update(
        kind="overview",
        scope="近期12场 · 11-06—11-17",
        metrics=[
            {"label": "ELO", "value": 1854.02},
            {"label": "近期分值 K/D", "value": 1.42},
            {"label": "分析样本胜率", "value": 40, "format": "percent", "wins": 20, "sample_size": 50},
            {"label": "近期伤害交换比", "value": 1.3, "note": "8/10 场有数据"},
        ],
        profile_rating={"score": 5.8, "title": "泯然众人", "tone": "medium", "sample_size": 4,
                        "win_rate_sample": {"value": 60, "wins": 6, "losses": 4, "sample_size": 10, "window_size": 12},
                        "dimensions": [{"key": key, "value": value} for key, value in
                                       (("kd", 37), ("contrib", 59), ("obj", 30), ("outcome", 51), ("win_rate", 60))]},
        common_units=[{"name": "Tu-95", "count": 72, "damage": 11682.87, "roi": 1.62},
                      {"name": "Mi-8AMTSh-VN Sapsan", "count": 165, "damage": 7074.14, "roi": .37}],
        style_scope="近83场风格", categories=[{"name": name, "percentage": value} for name, value in
                                             (("支援", 31.4), ("固定翼", 30.7), ("步兵", 10.7), ("车辆", 10.1),
                                              ("侦察", 9.6), ("直升机", 7.5))],
        map_stats=[{"name": "合成港区", "count": 8, "win_rate": 50},
                   {"name": "合成山区", "count": 6, "win_rate": 100 / 3}],
    )
    recent = deepcopy(base)
    recent.update(kind="recent", rows=[
        {
            "score": [8.6, 7.1, 2.3, 5.8, None][index], "score_band": "high" if index == 0 else "low" if index == 2 else "medium",
            "id": f"sample-match-{index + 1:03d}", "map_name": f"合成地图 {index + 1}",
            "mode": "custom" if index == 4 else "ranked", "outcome": "loss" if index == 2 else "win",
            "started_at": f"2026-11-{17 - index:02d}T02:20:00Z", "duration_seconds": 1327 + index * 53,
            "elo_delta": -12.5 if index == 2 else 8.25, "kills": 36 + index, "losses": 24,
            "unit_exchange": 1.5 + index / 24, "destroyed_value": 9750.5 + index * 100,
            "lost_value": 6520.25, "value_exchange": (9750.5 + index * 100) / 6520.25,
        }
        for index in range(5)
    ])
    match = deepcopy(base)
    match["player"]["name"] += " · 合成样本"
    match.update(
        kind="match", scope="单场记录 · 排位 · 5 对 5 人",
        match={"id": "sample-match-001", "map_name": "合成港区", "mode": "ranked",
               "started_at": "2026-11-17T02:20:00Z", "duration_seconds": 2700, "winner": "A 队", "target_outcome": "win"},
        prediction={"a": 50, "b": 50},
        teams=[
            {"id": str(side), "name": name, "outcome": "win" if side == 0 else "loss",
             "metrics": [{"label": label, "value": values[side], "format": "decimal" if label == "赛后平均 ELO" else "integer"}
                         for label, values in [("摧毁分", (32960, 31000)), ("损失分", (31000, 32960)),
                                               ("赛后平均 ELO", (1833.25, 1816.75)), ("击杀", (234, 227)), ("阵亡", (227, 234)),
                                               ("伤害", (4983, 6480)), ("出动价值", (40200, 43450)),
                                               ("占点", (15, 16)), ("补给消耗", (62685, 82319))]],
             "composition": [{"name": role, "percentage": percentages[side]}
                             for role, percentages in [("装甲", (20, 17)), ("步兵", (13, 13)), ("侦察", (10, 8)),
                                                        ("炮兵", (5, 7)), ("防空", (17, 19)), ("直升机", (10, 15)),
                                                        ("固定翼", (24, 20)), ("后勤/运输", (1, 1))]],
             "players": [
                {"id": base["player"]["id"] if side == 0 and index == 1 else f"sample-{side}-{index}",
                 "name": match["player"]["name"] if side == 0 and index == 1 else f"合成队员 {side + 1}-{index + 1}",
                 "elo_before": 1800 + index * 12.5,
                 "elo": 1800 + index * 12.5 + (8.25 if side == 0 else -8.25), "elo_delta": 8.25 if side == 0 else -8.25,
                 "damage_dealt": 1253, "damage_received": 964, "objectives": 3,
                 "spawned_value": 7570, "supply": 6926,
                 "kills": 36, "losses": 24, "unit_exchange": 1.5,
                 "destroyed_value": 9750.5, "lost_value": 6520.25, "value_exchange": 9750.5 / 6520.25}
                for index in range(size)
            ]}
            for side, (name, size) in enumerate([("A 队", 5), ("B 队", 5)])
        ],
    )
    samples = {}
    for kind, normal in [("overview", overview), ("recent", recent), ("match", match)]:
        samples[f"{kind}-normal"] = normal
        long = deepcopy(normal)
        long["player"]["name"] = "合成长昵称_中文MixedPlayer_" * (7 if kind == "match" else 3) + '{{7*7}} <昵称>&'
        long["player"]["id"] = "sample-long-id-" + "0123456789" * 8
        if kind == "overview":
            long["metrics"][0]["value"] = 12345.67
            long["profile_rating"]["dimensions"][0]["value"] = 100
            long["common_units"][0]["name"] = "合成长单位名称_" * 3
            long["common_units"][1].update(name="LongUnitDesignationWithoutSpaces" * 2, damage=1234567.89)
            long["map_stats"][0].update(name="合成长地图名称_" * 3, win_rate=0)
        elif kind == "recent":
            long["rows"][0].update(map_name="合成长地图名称_" * 3, id=long["player"]["id"],
                                   destroyed_value=9876543.21, lost_value=1234567.89,
                                   value_exchange=9876543.21 / 1234567.89)
        else:
            long["teams"][0]["name"] = "合成长分队名称_" * 7
            long["teams"][0]["players"][0]["name"] = long["player"]["name"]
        samples[f"{kind}-long-name"] = long
        missing = deepcopy(base)
        missing.update(kind=kind, player={"id": "sample-missing", "name": None},
                       source_url=None, source_updated_at=None, stale=True, scope="已取得样本 · 覆盖范围暂无")
        if kind == "overview":
            missing["metrics"] = [dict(metric, value=None) for metric in overview["metrics"]]
            missing["metrics"][2].update(wins=0, sample_size=0)
            missing["profile_rating"] = deepcopy(overview["profile_rating"])
            missing["profile_rating"].update(score=None, title=None, tone=None, sample_size=0,
                                             win_rate_sample={"value": 0, "wins": 0, "losses": 5,
                                                              "sample_size": 5, "window_size": 8})
            for axis in missing["profile_rating"]["dimensions"]:
                axis["value"] = 0 if axis["key"] in ("kd", "win_rate") else None
            missing["style_scope"] = "近0场风格"
            missing["common_units"] = [{"name": "合成未完整单位", "count": 0, "roi": 0}]
            missing["map_stats"] = [{"name": "合成未完整地图", "count": 2}]
        elif kind == "recent":
            missing["rows"] = [{"id": "sample-partial", "kills": 0, "duration_seconds": 0}]
        else:
            missing["match"] = {"id": "sample-partial"}
            missing["teams"] = [{"name": "合成未完整分队", "players": [{"id": "sample-player", "name": None}]},
                                {"name": "合成空分队", "players": []}]
        samples[f"{kind}-missing"] = missing
    small = deepcopy(overview)
    small["profile_rating"]["win_rate_sample"] = {"value": 100, "wins": 1, "losses": 0,
                                                   "sample_size": 1, "window_size": 12}
    samples["overview-small-sample"] = small
    multi = deepcopy(recent)
    multi["scope"] = "近期7场"
    multi["rows"] += [dict(recent["rows"][i], id=f"sample-match-{i + 6:03d}") for i in range(2)]
    samples["recent-multi"] = multi
    partial = deepcopy(match)
    partial["match"].update(roster_complete=False, target_outcome="unknown")
    partial["scope"] = "单局 · 详情收录 9 人 · 近期名单 11 人"
    partial["teams"][0]["players"].pop()
    partial["teams"][1]["players"].pop()
    partial["teams"][1].update(composition=[], composition_note="1 人缺单位数据，构成暂无")
    partial["teams"][0]["metrics"][5].update(value=3062, complete=False, note="3/4 人有数据")
    partial["teams"].append({"id": "4", "name": "观战", "players": [{"name": "合成观战者"}]})
    samples["match-partial"] = partial
    direct = deepcopy(match)
    del direct["player"]
    direct["match"].pop("target_outcome")
    samples["match-direct"] = direct
    return samples


async def screenshots(artifacts: list[dict]) -> list[dict]:
    from PIL import Image
    from playwright.async_api import async_playwright

    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(SESSION / ".playwright")
    temporary = OUTPUT / ".tmp"
    temporary.mkdir(exist_ok=True)
    os.environ["TMP"] = str(temporary)
    os.environ["TEMP"] = str(temporary)
    tempfile.tempdir = str(temporary)
    checks = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            for width in [*Renderer.WIDTHS.values(), PHONE_WIDTH]:
                context = await browser.new_context(viewport={"width": width, "height": 900},
                                                    device_scale_factor=1, offline=True)
                await context.route("**/*", lambda route: route.abort())
                try:
                    page = await context.new_page()
                    requests = []
                    page.on("request", lambda request: requests.append(request.url))
                    for item in artifacts:
                        if width not in (Renderer.WIDTHS[item["kind"]], PHONE_WIDTH):
                            continue
                        requests.clear()
                        await page.set_content((OUTPUT / item["html"]).read_text(encoding="utf-8"), wait_until="load")
                        await page.evaluate("document.fonts.ready")
                        dimensions = await page.evaluate(r"""() => {
                            const report = document.querySelector('.report');
                            const bounds = report.getBoundingClientRect();
                            const numbers = report.querySelectorAll(
                                '.metric-number, .report-table td:not(:first-child), .score, .style-list b, ' +
                                '.compact-line b, .row-meta b, .row-elo, .summary-metrics dd, .roster .participant td, ' +
                                '.comparison-values b, .prediction-values b'
                            );
                            const overflowingNumbers = [...numbers].filter(el => {
                                if (!el.getClientRects().length) return false;
                                if (el.clientWidth && el.scrollWidth > el.clientWidth + 1) return true;
                                const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
                                for (let node = walker.nextNode(); node; node = walker.nextNode()) {
                                    for (const token of node.data.matchAll(/[+-]?\d[\d,.]*%?/g)) {
                                        const range = document.createRange();
                                        range.setStart(node, token.index);
                                        range.setEnd(node, token.index + token[0].length);
                                        const rects = [...range.getClientRects()];
                                        if (rects.length !== 1 || rects.some(r => r.left < bounds.left - 1 ||
                                                r.right > bounds.right + 1)) return true;
                                    }
                                }
                                return false;
                            }).map(el => el.textContent.trim());
                            const radar = report.querySelector('.radar');
                            const frame = radar?.getBoundingClientRect();
                            const clippedRadarLabels = [...(radar?.querySelectorAll('.radar-label') ?? [])]
                                .filter(el => {
                                    const box = el.getBoundingClientRect();
                                    return box.left < frame.left || box.right > frame.right ||
                                        box.top < frame.top || box.bottom > frame.bottom;
                                }).map(el => el.textContent.trim());
                            const roster = report.querySelector('.roster');
                            const rosterColumns = [...(roster?.querySelectorAll('thead th') ?? [])]
                                .filter(el => el.getClientRects().length).map(el => el.textContent.trim());
                            const badRoster = roster && (roster.tHead.rows.length !== 1 ||
                                rosterColumns.length !== (innerWidth <= 680 ? 3 : 5) ||
                                roster.querySelectorAll('.participant-detail').length !==
                                roster.querySelectorAll('.participant.target').length);
                            return {
                                width: innerWidth, documentWidth: document.documentElement.scrollWidth,
                                height: document.documentElement.scrollHeight,
                                reportWidth: bounds.width, reportHeight: bounds.height,
                                synthetic: report.querySelector('.synthetic')?.textContent,
                                font: getComputedStyle(document.body).fontFamily,
                                overflowingNumbers, clippedRadarLabels, rosterColumns, badRoster
                            };
                        }""")
                        name = f"{item['name']}-{width}.png"
                        await page.locator(".report").screenshot(path=str(OUTPUT / name), animations="disabled")
                        check = {"image": name, "viewport": width, **dimensions,
                                 "external_requests": list(requests),
                                 "passed": dimensions["reportWidth"] == width and dimensions["documentWidth"] == width
                                           and not requests and not dimensions["overflowingNumbers"]
                                           and not dimensions["clippedRadarLabels"] and not dimensions["badRoster"]
                                           and bool(dimensions["synthetic"])}
                        if width != PHONE_WIDTH:
                            chat_image = f"{item['name']}-{width}-chat-{PHONE_WIDTH}.png"
                            with Image.open(OUTPUT / name) as image:
                                height = round(image.height * PHONE_WIDTH / image.width)
                                image.resize((PHONE_WIDTH, height), Image.Resampling.LANCZOS).save(OUTPUT / chat_image)
                            check["chat_preview"] = chat_image
                        checks.append(check)
                finally:
                    await context.close()
        finally:
            await browser.close()
    return checks


def main() -> int:
    global OUTPUT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--screenshots", action="store_true", help="use already installed local headless Chromium")
    parser.add_argument("--output", type=Path, default=OUTPUT, help="preview directory inside this session")
    args = parser.parse_args()
    OUTPUT = args.output.resolve()
    if not OUTPUT.is_relative_to(SESSION):
        parser.error("--output must stay inside this session directory")
    OUTPUT.mkdir(parents=True, exist_ok=True)
    artifacts = []
    for sample_name, view in sample_views().items():
        for theme in ("dark", "light"):
            renderer = Renderer(theme=theme)
            for page in renderer.views(view):
                name = f"{sample_name}-{theme}"
                if page.get("page_count", 1) > 1:
                    name += f"-page-{page['page']}"
                html_name, text_name = name + ".html", name + ".txt"
                (OUTPUT / html_name).write_text(renderer.template(page), encoding="utf-8")
                (OUTPUT / text_name).write_text(renderer.text(page), encoding="utf-8")
                artifacts.append({"name": name, "kind": view["kind"], "html": html_name, "text": text_name})
    checks = asyncio.run(screenshots(artifacts)) if args.screenshots else []
    manifest = {
        "synthetic": True, "live_api_used": False,
        "browser_mode": "local-only offline headless Chromium" if args.screenshots else "not launched",
        "artifacts": artifacts, "screenshots": checks,
        "visual_review": "Screenshots require manual image review; layout checks alone are not visual acceptance.",
    }
    (OUTPUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    failures = [check["image"] for check in checks if not check["passed"]]
    chat_count = sum("chat_preview" in check for check in checks)
    print(f"Synthetic previews: {len(artifacts)} HTML/text pairs; {len(checks)} screenshots; "
          f"{chat_count} chat-scale previews; {len(failures)} layout/network failures.")
    print(f"Output: {OUTPUT}")
    if failures:
        print("Failed: " + ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
