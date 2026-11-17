"""Build labelled synthetic card previews, with optional local-only Chromium.

Run from the repository using the session's .venv/Scripts/python.exe -B:
  tools/render_previews.py                 # HTML + matching text only
  tools/render_previews.py --screenshots   # desktop + 390px mobile PNGs

Chromium must already be installed under the session's .playwright directory.
No installation, server, external page, account, or live API is used. Browser
contexts are offline and all requests are aborted as an additional boundary.
All outputs, browser temporary profiles and the manifest stay under the
session root's previews/. Images show synthetic samples, never real players.
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
OUTPUT = SESSION / "previews"
sys.path.insert(0, str(REPOSITORY))

from render import Renderer


def sample_views() -> dict[str, dict]:
    """Deterministic synthetic data; deliberately unequal 3v2 team sizes."""
    base = {
        "player": {"id": "sample-000001", "name": "蓝线观察员 · 合成样本"},
        "scope": "最近取得 5 场 · 其中排位样本 4 场 · 非完整历史",
        "fetched_at": "2026-11-17T04:10:00Z",
        "source_updated_at": "2026-11-17T04:00:00Z",
        "source_url": "https://example.invalid/synthetic-preview",
        "synthetic": True,
    }
    overview = deepcopy(base)
    overview.update(
        kind="overview",
        metrics=[
            {"label": "ELO", "value": 1834.25, "note": "来源更新时点"},
            {"label": "分析样本胜率", "value": 75, "format": "percent", "note": "75 胜 / 100 场已知结果"},
            {"label": "近期分值 K/D", "value": 150 / 96, "note": "近期 5 场摧毁分 ÷ 损失分"},
            {"label": "伤害交换比", "value": 39602 / 26081, "note": "分析单局 · #sample-match-001"},
        ],
        common_units=[{"name": "合成装甲单位 A", "count": 12,"damage":5200,"roi":1.6}, {"name": "合成支援兵种 B", "count": 8,"damage":2100,"roi":1.2}],
        analysis_scope="分析样本 100 场",
        map_stats=[{"name":"合成港区","count":8,"win_rate":50}],
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
    match.update(
        kind="match", scope="单场记录 · 排位 · 5 对 5 人",
        match={"id": "sample-match-001", "map_name": "合成港区", "mode": "ranked",
               "started_at": "2026-11-17T02:20:00Z", "duration_seconds": 2700, "winner": "A 队", "target_outcome": "win"},
        prediction={"a": 64, "b": 36},
        teams=[
            {"name": name, "outcome": "win" if side == 0 else "loss",
             "metrics": [{"label": label, "value": values[side], "format": "integer"}
                         for label, values in [("摧毁分", (32960, 31000)), ("损失分", (31000, 32960)),
                                               ("击杀", (234, 227)), ("阵亡", (227, 234)),
                                               ("伤害", (4983, 6480)), ("出兵花费", (40200, 43450)),
                                               ("占点", (15, 16)), ("补给消耗", (62685, 82319))]],
             "composition": [{"name":"装甲", "percentage":40}, {"name":"步兵", "percentage":30}, {"name":"固定翼", "percentage":30}],
             "composition_note":"合成样本", "players": [
                {"id": base["player"]["id"] if side == 0 and index == 1 else f"sample-{side}-{index}",
                 "name": base["player"]["name"] if side == 0 and index == 1 else f"合成队员 {side + 1}-{index + 1}",
                 "elo": 1800 + index * 12.5, "elo_delta": 8.25 if side == 0 else -8.25,
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
        long["player"]["name"] = "合成长昵称_中文MixedPlayer_" * 7 + '{{7*7}} <昵称>&'
        long["player"]["id"] = "sample-long-id-" + "0123456789" * 8
        if kind == "overview":
            long["common_units"][0]["name"] = "合成长单位名称_" * 8
        elif kind == "recent":
            long["rows"][0]["map_name"] = "合成长地图名称_" * 6
        else:
            long["teams"][0]["name"] = "合成长分队名称_" * 7
            long["teams"][0]["players"][0]["name"] = long["player"]["name"]
        samples[f"{kind}-long-name"] = long
        missing = deepcopy(base)
        missing.update(kind=kind, player={"id": "sample-missing", "name": None},
                       source_url=None, source_updated_at=None, stale=True, scope="已取得样本 · 覆盖范围暂无")
        if kind == "overview":
            missing["metrics"] = [
                {"label": "ELO", "value": None}, {"label": "样本胜率", "value": None, "format": "percent"},
                {"label": "实际样本", "value": 0, "format": "integer", "unit": "场"},
                {"label": "价值交换比", "value": None},
            ]
        elif kind == "recent":
            missing["rows"] = [{"id": "sample-partial", "kills": 0, "duration_seconds": 0}]
        else:
            missing["match"] = {"id": "sample-partial"}
            missing["teams"] = [{"name": "合成未完整分队", "players": [{"id": "sample-player", "name": None}]},
                                {"name": "合成空分队", "players": []}]
        samples[f"{kind}-missing"] = missing
    return samples


async def screenshots(artifacts: list[dict]) -> list[dict]:
    from playwright.async_api import async_playwright

    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(SESSION / ".playwright"))
    temporary = OUTPUT / ".tmp"
    temporary.mkdir(exist_ok=True)
    os.environ["TMP"] = str(temporary)
    os.environ["TEMP"] = str(temporary)
    tempfile.tempdir = str(temporary)
    checks = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            for label, width in [("desktop", 900), ("mobile", 390)]:
                context = await browser.new_context(viewport={"width": width, "height": 900},
                                                    device_scale_factor=1, offline=True)
                await context.route("**/*", lambda route: route.abort())
                try:
                    page = await context.new_page()
                    requests = []
                    page.on("request", lambda request: requests.append(request.url))
                    for item in artifacts:
                        requests.clear()
                        await page.set_content((OUTPUT / item["html"]).read_text(encoding="utf-8"), wait_until="load")
                        await page.evaluate("document.fonts.ready")
                        dimensions = await page.evaluate("""() => ({
                            width: innerWidth,
                            documentWidth: document.documentElement.scrollWidth,
                            height: document.documentElement.scrollHeight,
                            reportHeight: document.querySelector('.report').getBoundingClientRect().height,
                            synthetic: document.querySelector('.synthetic')?.textContent,
                            font: getComputedStyle(document.body).fontFamily
                        })""")
                        name = f"{item['name']}-{label}.png"
                        await page.locator(".report").screenshot(path=str(OUTPUT / name), animations="disabled")
                        check = {"image": name, "viewport": label, **dimensions,
                                 "external_requests": list(requests),
                                 "passed": dimensions["documentWidth"] <= width and not requests
                                           and bool(dimensions["synthetic"])}
                        checks.append(check)
                finally:
                    await context.close()
        finally:
            await browser.close()
    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--screenshots", action="store_true", help="use already installed local headless Chromium")
    args = parser.parse_args()
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
                artifacts.append({"name": name, "html": html_name, "text": text_name})
    checks = asyncio.run(screenshots(artifacts)) if args.screenshots else []
    manifest = {
        "synthetic": True, "live_api_used": False,
        "browser_mode": "local-only offline headless Chromium" if args.screenshots else "not launched",
        "artifacts": artifacts, "screenshots": checks,
        "visual_review": "Screenshots require manual image review; layout checks alone are not visual acceptance.",
    }
    (OUTPUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    failures = [check["image"] for check in checks if not check["passed"]]
    print(f"Synthetic previews: {len(artifacts)} HTML/text pairs; {len(checks)} screenshots; {len(failures)} layout/network failures.")
    print(f"Output: {OUTPUT}")
    if failures:
        print("Failed: " + ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
