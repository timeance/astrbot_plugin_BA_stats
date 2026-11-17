"""Exercise the local settings view with the real schema and a synthetic bridge.

Uses only an installed headless Chromium. No AstrBot, BATrace or QQ connection.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SESSION = ROOT.parent
OUTPUT = SESSION / "previews"
sys.path.insert(0, str(SESSION))

from astrbot_plugin_ba_stats.diagnostics import DiagnosticLog
from astrbot_plugin_ba_stats.settings import Settings
from astrbot_plugin_ba_stats.web_settings import WebSettings


def fixture():
    directory = SESSION / "tmp" / "settings-preview"
    directory.mkdir(parents=True, exist_ok=True)
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    values = {group: {key: spec["default"] for key, spec in node["items"].items()}
              for group, node in schema.items()}
    path = directory / "synthetic-config.json"
    path.write_text(json.dumps(values), encoding="utf-8")

    class Config(dict):
        pass

    config = Config(values)
    config.config_path = str(path)
    config.schema = schema
    recorder = DiagnosticLog(directory / "logs")
    snapshot = WebSettings(config, Settings.from_config(config), recorder).snapshot()
    snapshot["diagnostics"].update(bytes=12288, files=1)
    return snapshot


MOCK = r"""({snapshot, theme}) => {
  window.__state = structuredClone(snapshot);
  window.__posts = [];
  window.__downloads = [];
  window.__conflict = false;
  window.AstrBotPluginView = {
    ready: async () => { document.documentElement.dataset.theme = theme; },
    apiGet: async path => {
      if (path === 'settings/config') return structuredClone(window.__state);
      if (path === 'diagnostics/status') return structuredClone(window.__state.diagnostics);
      throw new Error('Unexpected API path');
    },
    apiPost: async (path, payload) => {
      window.__posts.push({path, payload: structuredClone(payload)});
      if (window.__conflict) throw new Error('配置已在其他页面更新。草稿已保留。');
      const state = window.__state;
      for (const [key, value] of Object.entries(payload.changes)) {
        state.fields.find(field => field.path === key).value = value;
        if (key === 'diagnostics.enabled') state.diagnostics.enabled = value;
        else if (!state.pending_restart.includes(key)) state.pending_restart.push(key);
      }
      state.revision += '-saved';
      state.reload_required = state.pending_restart.length > 0;
      state.message = state.reload_required ? '已保存，其他更改需手动重载。' : '已保存并应用诊断开关，无需重载。';
      return structuredClone(state);
    },
    download: async (...args) => { window.__downloads.push(args); }
  };
}"""


async def main():
    from playwright.async_api import async_playwright

    OUTPUT.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(SESSION / ".playwright"))
    temporary = SESSION / "tmp" / "settings-preview"
    temporary.mkdir(parents=True, exist_ok=True)
    os.environ["TMP"] = os.environ["TEMP"] = str(temporary)
    tempfile.tempdir = str(temporary)
    snapshot = fixture()
    checks = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            for theme, width, label in [("light", 1100, "desktop"), ("dark", 390, "mobile")]:
                context = await browser.new_context(viewport={"width": width, "height": 850}, offline=True)
                page = await context.new_page()
                errors, external = [], []
                page.on("pageerror", lambda error: errors.append(str(error)))

                async def route(request):
                    name = request.request.url.removeprefix("http://ba-stats.local/")
                    if name not in {"", "settings.js"}:
                        external.append(request.request.url)
                        await request.abort()
                        return
                    path = ROOT / "views" / "settings" / (name or "index.html")
                    await request.fulfill(body=path.read_bytes(), content_type="text/javascript" if name else "text/html")

                await context.route("**/*", route)
                await page.add_init_script("(" + MOCK + ")(" + json.dumps({"snapshot": snapshot, "theme": theme}) + ")")
                await page.goto("http://ba-stats.local/")
                await page.wait_for_function("document.getElementById('save-status').textContent === '已读取设置。'")
                assert await page.locator("html").get_attribute("data-theme") == theme
                assert await page.locator("#save").is_disabled()
                assert await page.locator("#diagnostics details").get_attribute("open") is None
                assert await page.locator("#maintenance details").get_attribute("open") is None
                assert await page.locator("#setting-access-client_id, #setting-access-api_token").count() == 0
                await page.evaluate("""() => {
                  const p = document.createElement('p'); p.textContent = '合成预览 · 非运行实例';
                  p.className = 'muted'; document.querySelector('h1').after(p);
                }""")
                # Full-document exports place the sticky action row at the end;
                # interaction checks use the unchanged production CSS.
                await page.screenshot(path=str(OUTPUT / f"settings-{theme}-{label}.png"), full_page=True,
                                      style=".save-row { position: static !important; }")

                await page.locator('[data-section="diagnostics"]').click()
                assert not await page.locator("#setting-diagnostics-enabled").is_visible()
                await page.locator("#diagnostics summary").click()
                await page.locator("#setting-diagnostics-enabled").check()
                await page.locator("#save").click()
                await page.wait_for_function("document.getElementById('save-status').textContent.includes('无需重载')")
                assert await page.locator("#diagnostics-mode").inner_text() == "已开启"
                post = await page.evaluate("window.__posts.at(-1)")
                assert post["path"] == "settings/config" and post["payload"]["changes"] == {"diagnostics.enabled": True}
                await page.locator("#download").click()
                await page.wait_for_function("window.__downloads.length === 1")
                assert (await page.evaluate("window.__downloads[0]")) == ["diagnostics/download", {}, "ba-stats-diagnostics.zip"]
                await page.screenshot(path=str(OUTPUT / f"settings-diagnostics-{theme}-{label}.png"), full_page=True)

                await page.locator('[data-section="limits"]').click()
                await page.locator("#setting-limits-user_daily_limit").fill("21")
                await page.evaluate("window.__conflict = true")
                await page.locator("#save").click()
                await page.wait_for_function("document.getElementById('save-status').dataset.error === 'true'")
                assert await page.locator("#setting-limits-user_daily_limit").input_value() == "21"
                await page.evaluate("window.__conflict = false; window.__state.revision += '-external'")
                await page.locator("#reload").click()
                await page.wait_for_function("document.getElementById('save-status').textContent.includes('保留草稿')")
                assert await page.locator("#setting-limits-user_daily_limit").input_value() == "21"
                await page.locator("#save").click()
                await page.wait_for_function("document.getElementById('save-status').textContent.includes('手动重载')")
                assert await page.locator("#save").is_disabled()
                assert await page.locator("#setting-limits-group_daily_limit").input_value() == "100"
                assert await page.locator("#setting-limits-global_daily_limit").count() == 0
                assert "重载" in await page.locator("#apply-state").inner_text()
                dimensions = await page.evaluate("({width:innerWidth, documentWidth:document.documentElement.scrollWidth})")
                assert dimensions["documentWidth"] <= width and not errors and not external
                checks.append({"theme": theme, "viewport": label, **dimensions, "passed": True,
                               "checks": ["host theme", "default collapsed", "diagnostic save", "download route",
                                          "conflict preserves draft", "reload preserves draft", "restart indication", "fixed global quota"],
                               "page_errors": errors, "external_requests": external})
                await context.close()
        finally:
            await browser.close()
    (OUTPUT / "settings-manifest.json").write_text(json.dumps({"synthetic": True, "checks": checks}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Settings view: {len(checks)} local browser scenarios passed; 4 screenshots; no external requests.")


if __name__ == "__main__":
    asyncio.run(main())
