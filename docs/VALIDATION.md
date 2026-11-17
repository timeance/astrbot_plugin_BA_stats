# 首版验证记录

验证日期：2026-11-17（Asia/Hong_Kong）。版本：v0.1.0。

## 结果边界

| 项目 | 结果 |
| --- | --- |
| 首版代码与文档 | 已实现；按项目框架、插件实现两个 PR 审阅 |
| 离线业务与契约测试 | **392 passed，19.49 秒**（含新增每日额度） |
| 本地卡片渲染 | 18 组 HTML/文字、36 张 PNG；布局与外部请求检查全通过 |
| 成品目视检查 | 主代理查看 4 张代表图；渲染子代理另查看 9 张 |
| Python 语法检查 | 全部通过 |
| 两张 Logo 原样核对 | SHA-256 与交接包一致 |
| AstrBot API 兼容性研究 | 核对 v4.29.4 固定源码，插件适配器用本地桩测试 |
| 完整 AstrBot 服务启动/真实 html_render | **尚未执行** |
| BATrace 真实接口联调 | **尚未执行，缺本插件独立获准接入** |
| QQ OneBot v11 群聊与私聊 | **尚未执行，未发送真实消息** |
| GitHub PR / 发布 / 部署 | 远端检查以对应 PR 为准；本记录不代表发布或部署验收 |

API 与模型 fixture、渲染预览均为合成数据。HTTP 测试使用 `httpx.MockTransport`，不调用 BATrace。`tests/test_main.py` 在测试作用域注入 AstrBot 桩并恢复，不代表真实框架启动成功。

## 文案导入与 CI

Gemini 返回的 331 条文案全部匹配导出基线；回填 68 条变化，并校正其中 4 处语义。README 沿用用户要求的简版，详细说明保留在 `docs`。同步旧文案断言后，推荐候选的 392 项离线测试全部通过；正式导入逐文件核对与候选一致。

项目 CI 使用 Python 3.11 / 3.12，执行依赖兼容性、元数据和语法检查；插件实现 PR 增加完整离线测试和安装包构建检查。CI 不调用真实战绩 API，也不发送 QQ 消息。

## 核对基线

- 目标仓库初始 `main`：`c4272e87413734ec317fe937faa868bbbf70eb08`，原来仅有 Apache-2.0 LICENSE。
- AstrBot：v4.29.4，`61335414433db721b3f7ecc93f90deafe0a59d78`。
- 发布列表中有更新的 v4.30.0-alpha.1；虽然 GitHub `prerelease` 字段为 false，版本名仍为 alpha，本次没有作为稳定目标。
- ba-dragon：`072dc9bc2d73ffc87000c1d3db4c0f9b4dcab549`，接口研究细节见 API_NOTES.md。
- wows-stats-bot：`80c86970685887578054e61e1a76d1589ea1dc31`，只参考 README 中绑定/@ 查询的交互方式。
- OpenAI Native 插件：`98bd76784bb2a35df0aea968138f87fd4d9ee207`，按用户指定参考额度交互和配置设计；本次采用用户默认 20、群默认 100、总额固定 100 及北京时间 0 点重置。
- AstrBot T2I：`bcc08e47274c69e5ace19bbf738b95260ce17ca7`，核对视口、像素比、截图类型参数。运行插件使用 390 CSS 像素视口与 ultra 像素比；JPEG 质量 85。实际用户部署的渲染服务版本仍需联调。

## 环境和执行命令

Windows，Python 3.11.4；httpx 0.28.1、aiosqlite 0.22.1、Jinja2 3.1.6、pytest 9.2.0、pytest-asyncio 1.5.0、Playwright 1.65.2、tzdata 2026.6。
本地 Chromium 为 159.0.8154.6（Playwright build 1255）。这是测试环境记录，不是对所有环境的兼容承诺。

会话工作目录：`H:\Codex\01a1e923-f766-7562-b804-35854a994fc4`；以下命令在其中的 `astrbot_plugin_ba_stats` 仓库执行。

```powershell
& ../.venv/Scripts/python.exe -m pytest -q --basetemp=../tmp/pytest-quota-final
# 392 passed in 19.49s

$env:PLAYWRIGHT_BROWSERS_PATH = '../.playwright'
& ../.venv/Scripts/python.exe -B tools/render_previews.py --screenshots
# Synthetic previews: 18 HTML/text pairs; 36 screenshots; 0 layout/network failures.

& ../.venv/Scripts/python.exe -m compileall -q .
# exit 0
```

最初依赖安装遇到 Windows 临时目录写入限制，改用会话内临时目录后安装成功。Chromium 下载的一次连接重置由下载器切换官方镜像后恢复；最终安装成功。未更改共享环境、系统 ACL、SSH 配置或用户密钥。

## 测试覆盖的风险

- 命令别名一致、`recent 10` 和 `me recent 10`、带目标 `--limit`、空格昵称、数字/关键词昵称、真实结构化 @ 与手写标记拒绝。
- STB/Steam 分离、未验证映射明确失败、规范 ID 回显核对、同名去重及候选截断提示。
- 平台/用户/会话隔离、旧异步结果晚到、过期和越界、单次选择、选择期间新操作与跨群解绑竞争。
- 只读选择不改绑定、绑定重启持久、失败保留原绑定、清缓存不删绑定。
- 已确认字段的归一化、未知胜负/模式、数量和价值分离、缺失/零损失、重复与乱序对局。
- 新鲜/旧缓存、同请求合并、force 刷新竞争、有界队列、限流/超时/鉴权/结构错误、取消和关闭恢复。
- 管理员权限、未配置时本地命令、同一结果渲染失败回退、模板转义与 `{{7*7}}` 作为文字保留、卸载关闭资源。
- 每用户默认 20、每群默认 100 可调，总上限 100 固定；并发及多 SQLite 连接同时扣额不超限，任一桶不足时其他桶不扣额。
- 用户跨群及私聊累计、群跨用户累计、不同平台身份隔离、查询 @ 群友扣发送者；管理员查询无豁免，缓存与本地命令不扣额。
- 北京时间 23:59:59/00:00 日期切换、跨午夜排队、重开数据库/清缓存/改凭据保留计数、提交前取消及异常回滚。
- 每次 HTTP 尝试在发送前记账，失败及重试计入；设置项没有总额度或日界时区的可调入口。
- 子代理复现的「额度耗尽后受退避阻塞，先超时再回退旧缓存」已修复：增加只读预检及回退前复核，新增回归测试通过。

## 图像验证

预览矩阵：overview/recent/match × normal/long-name/missing × dark/light；每组检查 980px 和 390px。
浏览器 context 使用离线模式并拦截外部请求，未打开外部网页、账号或登录态。

主代理实际打开并检查：

- `overview-normal-dark-desktop.png`：主要数字、单位、样本范围与来源层级。
- `recent-normal-light-mobile.png`：手机尺寸的逐局阅读、明确胜负样本标签、数量/价值指标区分。
- `match-missing-light-mobile.png`：缺失字段、不完整队伍、零人队伍与旧缓存说明。
- `overview-long-name-light-mobile.png`：长中文/英文、长 ID、Jinja 字面文本、折行不裁切。

预览明确标注「合成样本 · 非真实玩家战绩」。其中用于展示胜负、排位等状态的合成字段，不表示当前 BATrace 适配器已获得这些字段的权威语义。生产适配器对此保持未知。
截图与 `manifest.json` 随总交付 ZIP 提供；布局自动检查与目视检查分别记录，不替代真实 QQ 图片显示验收。

新增额度功能的可见界面变化为命令文字和原生设置；三类卡片模板和样式未修改，复用上述已经通过的本地渲染检查。

## 安装包与文案交付

构建工具：`python tools/build_delivery.py`。

- 插件 ZIP 有单一 `astrbot_plugin_ba_stats/` 根目录及原生入口、元数据、配置、依赖、模板和文档。
- 完整源码另含测试和开发工具；安装 ZIP 不包含测试环境、真实凭据、个人数据库、缓存、Git 目录或参考项目源码。
- 文案优化 ZIP 包含 Gemini 任务说明、事实约束、带位置的 JSON/CSV 清单、README、原始提示与模板，以及合成预览。占位符、命令、配置键和数据口径标为不可随意修改。
- 总 ZIP 与嵌套 ZIP 使用 `testzip()` 检查，保存源码 SHA-256，原始 Logo 哈希如下。

```text
logo.png
7b93b4513cc84535155e4e4edf63b7a36b17321823e05ce0b494369d03b6e616

assets/readme-logo.png
2acb2855440c8ec609370789866857ca80e9c4c73a1cd1379f9b23d5c15e65a7
```

## 下一步真实验收

先取得本插件可用的 API origin、获准身份/鉴权方式、接口范围、请求额度与缓存政策。按 API_NOTES.md 的最小调用步骤验证 ID 回显、时间单位、胜负/模式与字段结构，再在明确授权的 AstrBot/QQ 测试会话验收绑定、搜索和三类战报。没有这些条件时保持在线开关关闭。
