"""Build an install ZIP, Gemini copywriting ZIP, and one final delivery ZIP.

Reads only repository sources and the session's reviewed previews. No runtime
database, configuration, cache, credentials or virtual environment is included.
"""
from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import io
import json
from pathlib import Path
import re
import zipfile

import yaml

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.1.2"
CHINESE = re.compile(r"[\u3400-\u9fff]")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def repository_files():
    root_names = {"README.md", "CHANGELOG.md", "LICENSE", "metadata.yaml", "_conf_schema.json",
                  "requirements.txt", "requirements-dev.txt", "pytest.ini", ".gitignore", ".gitattributes", "logo.png"}
    files = []
    for p in ROOT.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(ROOT)
        if "__pycache__" in rel.parts or ".pytest_cache" in rel.parts:
            continue
        if len(rel.parts) == 1 and (p.suffix == ".py" or p.name in root_names):
            files.append(p)
        elif rel.parts[0] in {"templates", "views", "assets", "docs", "tests", "tools", ".github", ".astrbot-plugin"} and p.suffix in {".py", ".html", ".js", ".css", ".md", ".json", ".png", ".txt", ".yml", ".yaml"}:
            files.append(p)
    return sorted(files)


def archive(entries):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for name, data in sorted(entries.items()):
            # Stable ZIP metadata keeps an unchanged export reproducible.
            info = zipfile.ZipInfo(name, date_time=(2026, 11, 17, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, data)
    data = stream.getvalue()
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        assert z.testzip() is None
        assert all(not name.startswith("/") and ".." not in Path(name).parts for name in z.namelist())
    return data


def text_catalog(files):
    result = []
    for path in files:
        rel = path.relative_to(ROOT).as_posix()
        if path.parent != ROOT or path.suffix != ".py":
            continue
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
                first = node.body[0]
                if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                    docstrings.add(first.value)
        for node in ast.walk(tree):
            if isinstance(node, ast.JoinedStr):
                text = "".join(v.value if isinstance(v, ast.Constant) else "{" + ast.unparse(v.value)
                               + ("!" + chr(v.conversion) if v.conversion != -1 else "")
                               + (":" + "".join(x.value if isinstance(x, ast.Constant) else "{" + ast.unparse(x.value) + "}"
                                               for x in v.format_spec.values) if v.format_spec else "") + "}"
                               for v in node.values)
                kind = "格式化提示（占位符必须原样保留）"
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node not in docstrings:
                if isinstance(parents.get(node), ast.JoinedStr):
                    continue
                text, kind = node.value, "文字常量（请结合上下文确认是否用户可见）"
            else:
                continue
            ancestors, current = [], node
            error_text = False
            while current in parents:
                current = parents[current]
                if isinstance(current, ast.Raise):
                    error_text = True
                if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    ancestors.append(current.name)
            if not CHINESE.search(text) and not error_text:
                continue
            line = node.lineno
            result.append(dict(id=f"{rel}:{line}:{len(result)+1}", file=rel, line=line,
                               context=" / ".join(reversed(ancestors)), kind=kind, original=text,
                               source_literal=ast.get_source_segment(source, node), suggestion=""))
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    def walk(value, pointer=""):
        if isinstance(value, dict):
            for key, child in value.items():
                target = pointer + "/" + key
                if key in {"description", "hint", "labels", "subtitles"}:
                    values = child if isinstance(child, list) else [child]
                    for i, text in enumerate(values):
                        result.append(dict(id="schema:" + target + (f"/{i}" if isinstance(child, list) else ""),
                                           file="_conf_schema.json", line=None, context=pointer, kind="配置界面文案",
                                           original=text, suggestion=""))
                else:
                    walk(child, target)
    walk(schema)
    result.append(dict(id="i18n:views.settings.title", file=".astrbot-plugin/i18n/zh-CN.json", line=None,
                       context="views.settings.title", kind="设置中心入口名称",
                       original=json.loads((ROOT / ".astrbot-plugin/i18n/zh-CN.json").read_text(encoding="utf-8"))["views"]["settings"]["title"], suggestion=""))
    for number, line in enumerate((ROOT / "metadata.yaml").read_text(encoding="utf-8").splitlines(), 1):
        if line.startswith(("display_name:", "short_desc:", "desc:")):
            key, text = line.split(":", 1)
            result.append(dict(id="metadata:" + key, file="metadata.yaml", line=number,
                               context=key, kind="插件展示信息（名称需用户确认）", original=yaml.safe_load(text), suggestion=""))
    # Keep complete template/view lines so translators see markup and identifiers.
    view_files = [path for path in (ROOT / "views").rglob("*") if path.suffix in {".html", ".js"}]
    for path in sorted([*(ROOT / "templates").glob("*.html"), *view_files]):
        is_template = path.parent == ROOT / "templates"
        prefix = "template:" + path.name if is_template else "view:" + path.relative_to(ROOT / "views").as_posix()
        script = path.suffix == ".js"
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if CHINESE.search(line):
                result.append(dict(id=f"{prefix}:{number}", file=path.relative_to(ROOT).as_posix(),
                                   line=number, context="保留 JavaScript 标识符、DOM 选择器、API 路径与占位符" if script else "保留 HTML 标签、Jinja 表达式和属性",
                                   kind="视图脚本行" if script else "卡片模板行" if is_template else "设置中心视图行",
                                   original=line, suggestion=""))
    return result


PROMPT = """# 交给 Gemini 的文案优化任务

你将润色 AstrBot 插件「断箭战报」v0.1.2 的中文提示、配置说明、图片战报文字、README 和 CHANGELOG。只提出文案修改，不执行代码、不调用服务、不改变功能或统计逻辑。

## 先读这些材料
1. `事实与约束.md`：哪些能力已实现、哪些尚未验证。
2. `文案清单.json` 或 `文案清单.csv`：带文件/行号/上下文的原文和待填写建议。
3. `原文件/README.md` 与 `原文件/CHANGELOG.md`：全文润色对象；README 保持简洁，CHANGELOG 只保留面向用户的简短更新。
4. `原文件/` 中代码、配置、模板和 `views/settings/` 的 HTML/JS：确认实际触发条件及动态占位符。自动清单可能收录内部日志或数据标签，请标注「保持原样」，不要擅改程序标识。
5. `参考预览/`：只用来理解呈现位置，战报均为合成样本；`match-normal-dark-desktop.png` 展示 900px 宽的完整单局布局（顶部预测、中部两栏、底部 10 名玩家），同时保留缺失数据示例。设置中心预览存在时一并提供：`settings-light-desktop.png` 为常用设置，`settings-diagnostics-dark-mobile.png` 为已展开的诊断日志。

## 文风
自然、准确、简洁的中文，像成熟工具的帮助信息；先告诉用户发生了什么及下一步。删重复说明和生硬技术用语，但保留影响操作的条件、范围、单位和后果。不要营销腔，不添加未经证实的能力、性能、兼容性或成功承诺。管理员诊断与普通玩家提示分别处理。

## 不可改动
- `ba`、英文子命令、现有中文别名、`stb:`、`steam:`、`name:`、`--limit`、示例 ID 的类型和参数顺序。
- Python f-string 的所有占位符及格式说明，如 `{pending.code}`、`{remaining:.0f}`；JSON 键、枚举值、路径、URL、版本、默认值与数字边界。
- Jinja 的 `{{ ... }}`、`{% ... %}`、HTML 标签属性、CSS、自动转义及安全规则。
- STB ID 与 Steam ID 的区分；绑定不是所有权认证；一次选择即确认；查询不修改绑定；原绑定在失败时保留。
- 缺失与零、未知胜负、近期样本与生涯、数量与价值、抓取时间与来源时间的区分。
- 每用户默认 20 次、每群默认 100 次可调，全插件每日 100 次固定不可调；北京时间 0 点重置；按 HTTP 请求计数，失败与重试计入、缓存不扣额。不要将请求次数润色为战报查询次数。
- `X-Client` 固定为 `Astrbot_BA_Stats`，不发送 `Authorization`，不要求填写客户端标识或令牌；`access.enabled.hint` 必须原样保留为「数据来源 BATrace ，如有帮助请支持该平台」。
- 0.1.2 的图片发送相关改动是补充失败诊断信息；不能写成 QQ 超时已修复或实际收发已验收。
- 近期战绩首列为基于 ba-dragon MIT 模型的逐局「评分」，按分数着色、不附称号；不引入时间线或 Kalman 生涯聚合算法。评分与来源 ELO 的含义不得混淆；缺少必要数据时保留 `None`、显示「暂无」，不补零或猜测评分。
- `ba last` 顶部是按赛前 ELO 估算的赛前预测，中部左右为双方对比与兵种构成，底部为玩家；兵种构成按基础花费估算，不能写成真实结算数据。
- 所有图片页脚保留 GitHub 头像、`timeance/astrbot_plugin_BA_stats` 和「数据来源 BATracer」。固定设置提示中的 `BATrace` 也按原文保留。
- 统一入口是插件详情 → 可视化视图 → 设置中心，与原生插件配置共用同一份设置；常用设置也可原生调整。维护设置和诊断日志的内容默认折叠。诊断开关保存后即时生效，其他设置保存后需在插件页手动重载，页面不自动重载。排查时选择「诊断日志」并展开，开启诊断并保存后复现问题，再点击「下载诊断日志（ZIP）」。API 由宿主鉴权。日志总容量为 `30 * 1024 * 1024` 字节，面向用户写 30 MB，旧日志自动轮换。
- 不把测试或合成截图写成已上线成果；README 保持简洁，详细接口边界和验证记录放在 docs。
- 许可证、来源致谢与原 Logo；`scoring.py` 的 ba-dragon 移植部分、`assets/scoring-model.json` 的 MIT 来源，以及 `docs/BA_DRAGON_LICENSE.txt` 完整文本不得删改或润色。不要改变设计、代码或配置结构。

## 需要返回
1. `README_润色建议.md`：完整 README，保留命令表、安装步骤、链接与必要限制。
2. `CHANGELOG_润色建议.md`：完整 CHANGELOG，保留版本、日期与已核实的更新事实，面向用户简短表述。
3. `文案修改建议.json`：数组，每条含 `id`、`original`、`suggestion`、`reason`；保持清单 id，不改键名。建议应为文案文本，不是自动执行补丁。
4. `需确认事项.md`：仅列会改变含义、与代码不一致或无法确认的事项；不要自作假设。

请按文件分组核对整份清单。可保持已清晰的文案；对多条原文相同但触发条件不同的提示，不盲目合并。先给可回填的完整结果，再简述少量关键取舍。最终建议由用户审阅并交回开发者，不自动覆盖源文件。
"""

FACTS = """# 事实与约束

- 本包是文案审阅材料，非生产配置。源码、提示清单、README 与 CHANGELOG 来自同一次交付快照；SHA256.json 用于核对回填基线。
- 首版功能：英文主命令、可选中文子命令别名、玩家解析、候选请求码、默认绑定、玩家概览/近期/单局、图片失败文字回退、缓存/限流/管理员状态。
- 在线查询默认关闭，开启后使用配置的 HTTPS API 地址；`X-Client` 固定为 `Astrbot_BA_Stats`，不发送 `Authorization`，不提供客户端标识或令牌设置。真实接口联调尚未完成。
- `access.enabled.hint` 固定为「数据来源 BATrace ，如有帮助请支持该平台」，不可润色。0.1.2 补充图片发送失败诊断，不能表述为 QQ 超时已修复。
- 近期战绩首列展示 ba-dragon 模型逐局「评分」，按分数着色，不附称号；没有时间线或 Kalman 生涯聚合。`ba last` 顶部为赛前预测，中部左右为双方对比/兵种构成，底部为玩家数据。预测按赛前 ELO 估算，兵种构成按基础花费估算。
- 评分已通过与上游 TypeScript 的 160 组合成样本对照；缺少必要数据时保持 `None`，展示为「暂无」。离线模型对照不代表真实 API 或 QQ 收发验收。
- 图片页脚统一为 GitHub 头像、`timeance/astrbot_plugin_BA_stats` 与「数据来源 BATracer」。
- 设置中心位于插件详情 → 可视化视图 → 设置中心，对应 `views/settings/index.html` 和同目录 `settings.js`。完整表单与原生插件配置共用同一份设置，常用设置也可原生调整。HTML/JS 为独立实现，未复制 `meme_manager` 源码。
- 维护设置和诊断日志的内容默认折叠。在设置中心选择「诊断日志」并展开，开启诊断并保存后复现问题，再点击「下载诊断日志（ZIP）」；开关、日志状态与下载位于同一区域，API 由宿主鉴权。日志总上限 `30 * 1024 * 1024` 字节，用户说明写 30 MB，旧日志自动轮换。
- SteamID64 权威映射尚未确认；语法识别后给出明确限制，不返回猜测映射。
- API 字段来自固定参考源码研究及合成契约测试；未确认的胜负、模式、地图/单位名称保持未知或来源 ID。当前样本不代表全生涯。
- 三类卡片已在本地 Chromium 用合成样本渲染并检查；实际 AstrBot 渲染服务与 QQ 收发仍待验收。
- 诊断开关保存后即时生效；其他设置保存后需在插件页手动重载，页面不自动重载，以免中断当前请求。本地默认值不是上游许可额度。
- 每用户默认 20 次、每群默认 100 次，均可在设置调整为 0–100；0 不单独限制。全插件每日 100 次为固定上限，不提供调整项或管理员豁免。所有额度北京时间每天 0 点重置，与显示时区无关。
- 用户额度按同平台实例的实际发送者跨群及私聊累计，群额度由群内用户共享。按实际提交 HTTP 的尝试计数，重试、失败计入；一条战报命令可能耗多次。命中缓存、本地命令、图片渲染不计入。并发原子记账、重载/清缓存/更换 API 地址不清零；发送前已记账的中断保守保留。
- `ba quota` 或 `ba 额度` 可查看个人、本群、全插件用量；总额度固定 100，不得描述为可配置。达到额度仍可读取新鲜缓存，需要刷新则提示额度已用完，不使用旧缓存隐藏提示。
- 用户偏好：界面简洁、主次清晰、必要解释靠近字段，普通小数两位、计数整数、精简冗余但保留口径和操作后果。
- 项目自有代码使用 Apache-2.0；`scoring.py` 的 ba-dragon 移植部分及 `assets/scoring-model.json` 保留 MIT，完整上游版权与许可在 `docs/BA_DRAGON_LICENSE.txt`，不得润色。既有 Logo 原样保留。来源资料见原文件/docs。
- 文案清单覆盖根目录 Python 中中文字符串及英文异常提示、配置展示字段、插件展示信息、含中文模板行及设置中心的 HTML/JS 文案；README、CHANGELOG 与文档全文随包。测试、工具、注释不作为用户文案润色目标。
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT.parent / "delivery")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    files = repository_files()
    entries = {p.relative_to(ROOT).as_posix(): p.read_bytes() for p in files}
    install = {"astrbot_plugin_ba_stats/" + name: data for name, data in entries.items()
               if name.split("/")[0] not in {"tests", "tools", ".github"}
               and name not in {"requirements-dev.txt", "pytest.ini", ".gitignore", ".gitattributes"}}
    for required in ("main.py", "metadata.yaml", "_conf_schema.json", "README.md", "CHANGELOG.md", "logo.png", "LICENSE",
                     "scoring.py", "assets/scoring-model.json", "assets/github-avatar.png",
                     "docs/THIRD_PARTY.md", "docs/BA_DRAGON_LICENSE.txt", "views/settings/index.html", "views/settings/settings.js", ".astrbot-plugin/i18n/zh-CN.json"):
        assert "astrbot_plugin_ba_stats/" + required in install
    for name in install:
        assert not any(p in name for p in (".env", ".sqlite", "__pycache__", ".git/"))
    plugin_name = f"astrbot_plugin_ba_stats_v{VERSION}.zip"
    plugin = archive(install)
    (args.output / plugin_name).write_bytes(plugin)

    catalog = text_catalog(files)
    copy_entries = {"给Gemini的任务说明.md": PROMPT.encode(), "事实与约束.md": FACTS.encode(),
                    "文案清单.json": json.dumps(catalog, ensure_ascii=False, indent=2).encode()}
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=["id", "file", "line", "context", "kind", "original", "suggestion"], extrasaction="ignore")
    writer.writeheader()
    writer.writerows(catalog)
    copy_entries["文案清单.csv"] = buffer.getvalue().encode("utf-8-sig")
    for name, data in entries.items():
        if name in {"README.md", "CHANGELOG.md", "metadata.yaml", "_conf_schema.json", "assets/scoring-model.json", "assets/github-avatar.png"} or ("/" not in name and name.endswith(".py")) or name.startswith(("templates/", "views/", "docs/", ".astrbot-plugin/")):
            copy_entries["原文件/" + name] = data
    previews = ROOT.parent / "previews"
    for name in ["overview-normal-dark-mobile.png", "recent-normal-light-mobile.png",
                 "match-normal-dark-desktop.png", "match-missing-light-mobile.png",
                 "settings-light-desktop.png", "settings-diagnostics-dark-mobile.png"]:
        path = previews / name
        if path.is_file():
            copy_entries["参考预览/" + name] = path.read_bytes()
    hashes = {name: digest(data) for name, data in entries.items()}
    copy_entries["SHA256.json"] = json.dumps(hashes, indent=2).encode()
    copy_name = f"断箭战报_Gemini文案优化包_v{VERSION}.zip"
    copy_zip = archive(copy_entries)
    (args.output / copy_name).write_bytes(copy_zip)

    bundle = {"安装包/" + plugin_name: plugin, "文案优化/" + copy_name: copy_zip}
    bundle.update({"源码/astrbot_plugin_ba_stats/" + name: data for name, data in entries.items()})
    for p in previews.glob("*"):
        if p.is_file() and p.suffix in {".png", ".html", ".txt", ".json"}:
            bundle["验证/卡片预览/" + p.name] = p.read_bytes()
    validation = ROOT / "docs" / "VALIDATION.md"
    assert validation.is_file(), "Write final validation evidence before delivery."
    bundle["验证/VALIDATION.md"] = validation.read_bytes()
    log = ROOT.parent / "validation" / "pytest.txt"
    if log.is_file():
        bundle["验证/pytest.txt"] = log.read_bytes()
    bundle["开始使用.md"] = f"""# 断箭战报 v{VERSION}

1. 导入 AstrBot：使用「安装包/{plugin_name}」，不要直接导入本总 ZIP。
2. 进入插件详情 → 可视化视图 → 设置中心，在「常用设置」中开启在线查询，API 地址通常保持默认；保存后在插件页手动重载，无需填写客户端标识或令牌。常用设置也可在原生插件配置调整。详见源码目录 README 和 docs/API_NOTES.md。
3. 「文案优化/{copy_name}」可单独交给 Gemini。先让 Gemini 阅读「给Gemini的任务说明.md」，润色提示、配置文字、卡片、README 和 CHANGELOG；回填前仍需核对占位符及功能语义。
4. 「验证」包含真实执行记录及标注为合成样本的预览。离线通过不代表真实 API 或 QQ 平台验收。
5. 完整源码、测试和重建工具位于「源码/astrbot_plugin_ba_stats」。仓库审阅状态以 GitHub PR 为准；此包不表示已发布市场或部署生产。
6. 每日默认额度：个人 20 次、每群 100 次可调，全插件固定 100 次。北京时间 0 点重置；发送 `ba quota` 查看用量。按上游 HTTP 请求计数，重试及失败计入，命中缓存不计入。
7. 排查时在设置中心选择「诊断日志」并展开，开启诊断并保存后即时生效；复现问题后点击「下载诊断日志（ZIP）」。开关、日志状态和下载在同一区域，日志总容量上限 30 MB，由宿主登录鉴权。其他设置保存后需手动重载，页面不会自动重载。
8. 近期战绩含逐局评分；单局战报含赛前预测、双方对比与兵种构成。评分模型保留 ba-dragon MIT 许可，详见源码目录 docs/THIRD_PARTY.md 和 docs/BA_DRAGON_LICENSE.txt。
""".encode()
    bundle["SHA256.json"] = json.dumps({name: digest(data) for name, data in bundle.items()}, ensure_ascii=False, indent=2).encode()
    final_name = f"断箭战报_v{VERSION}_交付包.zip"
    final = archive(bundle)
    (args.output / final_name).write_bytes(final)
    print(json.dumps({"delivery": str(args.output / final_name), "bytes": len(final),
                      "sha256": digest(final), "source_files": len(entries), "copy_items": len(catalog),
                      "plugin_entries": len(install), "bundle_entries": len(bundle)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
