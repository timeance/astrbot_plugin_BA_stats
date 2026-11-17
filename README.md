<p align="center"><img src="assets/readme-logo.png" width="420" alt="断箭战报"></p>

# 断箭战报

AstrBot 原生《断箭》（Broken Arrow）战绩查询插件。以简洁排版展示玩家概览、近期战绩与单局核心数据，支持图片战报和默认账号绑定，不依赖大模型。

用 `ba me` 查看综合评分、称号和五维雷达图。图中的「胜负预期」衡量近期战果相对赛前预期的表现；「实际胜率」是近期已知胜负对局中获胜的比例。`ba recent` 查看逐局评分，`ba last` 查看最近一局的赛前预测、双方对比和玩家数据。

> 数据来源 [BATrace](https://dash.batrace.top/)，如有帮助请支持该平台。更新内容见 [更新日志](CHANGELOG.md)。

## 快速开始

1. **安装插件**：在 AstrBot 的插件页面通过仓库链接在线安装：`https://github.com/timeance/astrbot_plugin_BA_stats`。
2. **开启查询**：进入插件详情 → 可视化视图 → 设置中心，在「常用设置」中开启「启用在线查询」（默认关闭）；API 地址通常保持默认即可。常用设置也可在 AstrBot 原生插件配置中调整。
3. **保存重载**：自定义设置中心保存后，在 AstrBot 插件页手动重载；原生插件设置保存时自动重载。
4. **绑定与查询**：
   - 发送 `ba search 昵称` 搜索玩家
   - 发送 `ba bind stb:玩家ID` 并按提示发送 `ba select 请求码 序号` 确认绑定
   - 发送 `ba me` 或 `ba recent` 查看战报

接入说明见 [API 说明](docs/API_NOTES.md)。

## 命令列表

下表为命令正文。群聊请先通过聊天软件的提及功能 @ 机器人，再输入 `ba …`；私聊通常直接发送，若宿主要求唤醒词则加在命令前。英文子命令不区分大小写，开启中文别名后也可直接使用中文指令（如 `ba 帮助`、`ba 我`、`ba 战绩`、`ba 近期`、`ba 上一场`、`ba 单局`、`ba 搜索`、`ba 绑定`、`ba 当前绑定`、`ba 解绑`、`ba 选择`、`ba 取消`、`ba 额度`）。

| 命令 | 用途 |
| --- | --- |
| `ba help` | 查看使用帮助与示例 |
| `ba me` | 查看自己默认绑定账号的玩家概览 |
| `ba stats <target>` | 查看指定玩家概览 |
| `ba recent [count]` | 查看自己近期战绩（默认 5 场，上限 20 场） |
| `ba me recent [count]` | 与 `ba recent [count]` 等价 |
| `ba recent <target> --limit N` | 查看指定玩家近期最多 `N` 场战绩 |
| `ba last [target]` | 查看自己或指定玩家最近一场可用单局战报 |
| `ba me last` | 与 `ba last` 等价 |
| `ba match <match-id>` | 查看指定对局 ID 的单局战报 |
| `ba search <query>` | 搜索玩家候选，选择后查看概览 |
| `ba bind <target>` | 设置默认查询账号（不验证账号归属） |
| `ba binding` | 查看当前绑定（纯本地，不耗额度） |
| `ba unbind` | 解除当前绑定（纯本地，不耗额度） |
| `ba select <request-code> <index>` | 确认候选编号（120 秒内有效，单次有效） |
| `ba cancel <request-code>` | 取消当前待确认选择 |
| `ba quota` | 查看个人、本群与全插件今日已用和剩余额度 |
| `ba status` | 管理员查看接入配置、请求与缓存状态 |
| `ba cache clear` | 管理员清理 API 与图片缓存（不影响绑定与额度） |

### 目标玩家（`target`）写法

支持 `me`、`@群友`、`stb:规范ID`、`name:完整昵称` 或普通非数字昵称：

```text
ba stats "Player 2"
ba bind name:"Player 2"
ba recent name:"Player 2" --limit 10
ba recent 10
ba stats name:12345
ba stats name:me
ba stats @群友
```

- `@群友` 需使用聊天软件的真实提及功能，查询对方已绑定的账号。
- 纯数字请加 `stb:`、`steam:` 或 `name:` 前缀（`ba recent 10` 除外，该写法专指查询自己近期 10 场）。
- `steam:SteamID64` 当前因映射尚未确认暂不可直接查询，请改用昵称搜索后选择 STB ID。

## 主要配置

自定义设置中心和 AstrBot 原生插件配置共用同一份设置。自定义设置中心保存后，诊断开关即时生效，其他设置需在插件页手动重载；原生插件设置保存时自动重载。

- **数据源接入**：在线查询开关、API 地址，无需填写客户端标识或令牌。
- **查询与命令**：默认/最大近期场数（5 / 20）、用户冷却（3 秒）、中文子命令别名开关。
- **每日请求额度**：每用户默认 20 次、每群默认 100 次（可在 `0–100` 间调整，`0` 表示不单独限制）；**全插件每日总上限固定为 100 次不可调整**。所有额度于北京时间每天 0 点重置，按上游 HTTP 请求计数，失败与重试也计入；命中缓存及本地命令不扣额。
- **战报显示**：图片默认浅色，可在「图片主题」中切换深色；支持图片战报开关（渲染或发送未成功时回复同内容文字）和显示时区（默认 `Asia/Shanghai`）。
- **维护与缓存**：请求间隔、并发、超时、重试及缓存容量/旧缓存回退时间，设置中心中默认折叠。
- **诊断日志**：诊断开关、日志状态和 ZIP 下载集中显示，内容默认折叠；总容量上限 30 MB，旧日志自动轮换。

图片发送方式（`output.image_send_mode`）可选「Base64（默认）」「本地文件（NapCat 直读）」「图片链接（NapCat 下载）」。Windows 同机部署可选本地文件，NapCat 必须能访问 AstrBot 的图片路径；图片链接需要渲染器提供 NapCat 可访问的 HTTP(S) 链接，返回本地文件时不可用。图片生成或发送失败会在控制台记录 `ERROR` 并回复原文字战报，不自动切换 Base64 或重试其他发送方式。

排查问题时，在设置中心选择「诊断日志」并展开，开启诊断并保存后复现问题，再点击「下载诊断日志（ZIP）」。下载由宿主登录鉴权。

## 数据与常见问题

- **为什么有字段显示「暂无」？**：当数据源未提供、未收录地图/单位名称，或胜负/模式语义尚未确认时，插件会显示「暂无」或来源 ID，不以猜测数据替代。
- **为什么没查满 20 次就提示额度用完？**：额度按上游 HTTP 请求次数计算，单条战报命令可能触发多次接口请求（如未命中缓存的概览需请求搜索、分析与近期列表），个人额度跨群及私聊累计，可用 `ba quota` 查看用量；重载或清缓存不会重置额度。
- **只收到文字没有图片？**：请检查「发送图片战报」是否开启及 AstrBot HTML 渲染服务是否正常。
- **同名玩家怎么区分？**：绑定候选不超过 3 人时，核对 STB ID 与来源链接后按提示确认；超过 3 人请前往 [BATrace](https://dash.batrace.top/) 搜索，在玩家主页链接末尾取得 ID，再发送 `ba bind stb:玩家ID`。

## 开发与许可

- 运行要求：AstrBot **4.29.4 及以上**，QQ（OneBot v11）；详细测试与验证记录见 [验证记录](docs/VALIDATION.md)。
- 开源协议：项目采用 **Apache-2.0**；`scoring.py` 的 ba-dragon 移植部分及 `assets/scoring-model.json` 保留上游 **MIT** 许可，完整文本见 [ba-dragon 许可](docs/BA_DRAGON_LICENSE.txt)。
- 致谢与参考：[BATrace](https://dash.batrace.top/)（战绩数据源）、[AlexanderHYu/ba-dragon](https://github.com/AlexanderHYu/ba-dragon)（评分模型及接口参考）、[benx1n/wows-stats-bot](https://github.com/benx1n/wows-stats-bot)、[timeance/astrbot_plugin_openai_native](https://github.com/timeance/astrbot_plugin_openai_native)、AstrBot（详见 [第三方说明](docs/THIRD_PARTY.md)）。

```sh
python -m pip install -r requirements-dev.txt
python -m pytest -q
python -m playwright install chromium
python tools/render_previews.py --help
```
