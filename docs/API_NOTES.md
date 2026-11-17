# BATrace 接口核对与本地适配契约

核对日期：2026-11-17。**本地客户端与合成响应测试已实现；真实接口联调未完成。**
本次只读取公开 GitHub 源码，没有请求 BATrace 数据接口，没有复用其他项目身份、联系第三方或进行浏览器验证。

## 证据与版本

- 参考仓库：`AlexanderHYu/ba-dragon`，默认分支 `main`。
- 执行时 GitHub connector 查询的分支提交：`072dc9bc2d73ffc87000c1d3db4c0f9b4dcab549`，提交时间 `2026-11-13T02:22:27Z`；`package.json` 版本 `2.0.1`。
- 客户端实现：[src/main/services/batrace.ts](https://github.com/AlexanderHYu/ba-dragon/blob/072dc9bc2d73ffc87000c1d3db4c0f9b4dcab549/src/main/services/batrace.ts)。
- 字段定义：[src/shared/types/batrace.ts](https://github.com/AlexanderHYu/ba-dragon/blob/072dc9bc2d73ffc87000c1d3db4c0f9b4dcab549/src/shared/types/batrace.ts)。
- 玩家与评分来源：[src/main/services/players.ts](https://github.com/AlexanderHYu/ba-dragon/blob/072dc9bc2d73ffc87000c1d3db4c0f9b4dcab549/src/main/services/players.ts)。
- 列表时间语义：[src/main/services/matchSync.ts](https://github.com/AlexanderHYu/ba-dragon/blob/072dc9bc2d73ffc87000c1d3db4c0f9b4dcab549/src/main/services/matchSync.ts)。
- 公开页面路径：[PlayerDetail.tsx 第 203、403 行](https://github.com/AlexanderHYu/ba-dragon/blob/072dc9bc2d73ffc87000c1d3db4c0f9b4dcab549/src/renderer/src/features/players/PlayerDetail.tsx)。
- 使用许可说明：[README](https://github.com/AlexanderHYu/ba-dragon/blob/072dc9bc2d73ffc87000c1d3db4c0f9b4dcab549/README.md)；源码许可证：[LICENSE](https://github.com/AlexanderHYu/ba-dragon/blob/072dc9bc2d73ffc87000c1d3db4c0f9b4dcab549/LICENSE)。
- 主代理选定的 AstrBot 稳定基线：`v4.29.4`，`61335414433db721b3f7ecc93f90deafe0a59d78`；框架适配与总验收由主代理记录。

研究快照位于本次会话根目录 `references/ba-dragon-072dc9bc/`，不进入插件 ZIP。
这是参考客户端的实际使用契约，**不是 BATrace 运营方发布的完整 API 规范，也不证明本插件获准访问**。

## 接入与鉴权边界

参考源码的 API origin 是 `https://bot.batrace.top`，公开页面 origin 是 `https://dash.batrace.top`。
参考源码使用 `X-Client` 标识，取值属于该项目自己的身份；README 的使用同意针对该工具，不延伸为其他插件的许可。
本插件独立使用固定标识 `X-Client: Astrbot_BA_Stats`，不复用参考项目标识，不提供客户端标识或令牌设置，也不发送 `Authorization`。

接入设置只有 `access.enabled=false` 和 `access.base_url=https://bot.batrace.top`；保存后重载生效。
`enabled=true` 且 API 地址为合法 HTTPS origin 时才可查询；未开启或地址无效时，在缓存与网络操作之前抛出 `NotConfiguredError`。
构造、状态查看、关闭均不探活，不回退到网页抓取或验证码绕行。

本插件同时发送 `User-Agent: astrbot-plugin-ba-stats` 与 `Accept: application/json`。固定客户端标识只描述请求行为，不代表真实接口已联调通过。
设置中的在线查询提示固定为「数据来源 BATrace ，如有帮助请支持该平台」。

参考客户端文件头还保留“不发自定义 UA”的旧注释，但该提交的 `CLIENT_HEADERS()` 实际发送自定义 UA 和 `X-Client`；本说明以现行代码为准，未据旧注释模拟浏览器。
参考工具采用串行、约 1.2 秒间隔，网络/5xx 约 30 秒后重试一次。这些是该工具的本地策略，**不能宣传为本插件获准的上游限额或通用放行办法**。

## 观察到的 GET 路径

| 路径 | 参数 | 观察到的响应形状 | 本插件用途与边界 |
| --- | --- | --- | --- |
| `/api/players/search` | `q`、`limit` | `{players:[{id,name,level?,rating?,rating_games?,updated_at?}]}` | 候选按字符串 ID 去重，返回前 20 项；不假装已搜遍全库 |
| `/api/analysis/player` | `stbid` | `{matchCount?,trend?:{points?},highlightUnits?,categoryPreferences?,mapPerformance?,playStyle?}` | 玩家身份核对后读取有时间的评分观察；不输出生涯胜率 |
| `/api/players/info` | `stbid` | `{info?:{name?},statInfo?:{...}}` | 已研究但未调用；已知 shape 不含规范 ID，不能单独完成身份校验 |
| `/api/players/matches` | `stbid`、`limit` | `{matches:[{matchId,data:MatchInfo}]}` | 最近样本；reference 说明单次最多 20，未独立验证；不实现历史分页 |
| `/api/match` | `matchid` | `{matchInfo:MatchInfo}` | 单局；若额外返回根级 `matchId`，必须与请求一致 |

公开页面路径在参考 UI 中明确出现：`/player/<规范ID>`、`/match/<对局ID>`。
模型据此生成 URL 并对 ID 做路径编码；**只确认源码中的使用方式，没有确认当前页面在线可达、是否需要登录或是否完整展示身份资料**。
绑定应同时显示最终名称、规范 ID 和该玩家链接，不声称完成 Steam 所有权认证。

`q=<规范ID>` 是否必定返回该规范 ID，缺少运营方文档及本插件真实响应验证。
`player(id)` 当前先调用该搜索，再要求候选中出现完全相同的 `id`，之后才请求分析；没有回显则抛 `IdentityMismatchError`，不能接受第一条或同名结果。
这是一条明确的上线依赖：若合法实测证明搜索不支持 ID，应取得有 ID 回显的权威接口后修改适配，不能绕过校验。
SteamID64 与 STB ID 均保持字符串；**未找到可靠的 Steam→STB 映射，不实现自动映射或同名猜测**。

## 字段归一化

`models.py` 为插件独立的 dataclass：

| 模型 | 字段 |
| --- | --- |
| `Player` | `id, name, steam_id?, elo?, source_url?, updated_at?, elo_observed_at?, analysis` |
| `Participant` | `player_id, name, team?, outcome, elo?, elo_before?, elo_delta?, kills?, losses?, destroyed_value?, lost_value?, units, units_available, damage_dealt?, damage_received?, objectives?, spawned_value?, supply?` |
| `Match` | `id, map_id?, map_name?, mode, started_at?, ended_at?, duration_seconds?, winner_team?, players, complete, source_url?, updated_at?` |
| `FetchResult[T]` | `data, fetched_at, stale=False, source_updated_at?, stale_reason?` |

- 外部 ID 只接收字符串或整数，不接收布尔、浮点或按位数猜测的身份；转换不会丢掉字符串前导零。
- 搜索 `rating` 是 ELO 档案快照；`updated_at` 保留源时间。分析 `trend.points` 中显式 `ratingAfter` 配合 `endTime`，按实际时间选最新，不依赖数组顺序。已知更新时间更新的搜索 ELO 优先。
- `elo_observed_at` 表示 ELO 观察对应的对局结束时间；它不是源记录更新时间。`source_updated_at` 不会用抓取时间或对局结束时间冒充。
- `analysis` 保留经过类型校验的来源分析数据：场数、带对局 ID 的 ELO/胜负与核心战绩记录、单位摘要、兵种占比和地图表现；逐局「评分」另由本地 ba-dragon 模型计算，缺失必要输入时保持未知。
- 只解析 Unix **秒**或带明确时区的 ISO 时间，内部为 aware UTC；不猜毫秒、不按机器本地时区解释无时区字符串。真实源若使用其他格式，须先核实其时区及单位再适配。
- `MatchInfo` 已观察到 `MapId, Type, StartTime, EndTime, TotalPlayTimeInSec, Data`。开始、结束时间分别保留；缺少开始时间时保持未知，不用结束时间和时长反推，以免改变评分中的时间判断。
- `Data` 为参与者对象字典，条目的 `Id` 为玩家规范 ID；不把对象键当 ID。重复参与者 ID、无 ID 或变更为其他集合结构均报结构错误。
- `OldRating/NewRating` 保留为赛前/赛后 ELO 与差值，**不将 ELO 涨跌转换成胜负**。单局适配缺少明确胜负字段时仍保留 `winner_team=None`、`outcome='unknown'`；展示层只在相同对局 ID 的分析记录中有明确布尔 `won` 时补充目标玩家结果。
- `Type` 枚举未得到可靠释义，故 `mode='unknown'`。缺评分或评分不变不能证明是自定义局；参考工具的“未计分”筛选也不能替代源模式。
- `Destruction/Losses` 对应单位数量，`DestructionScore/LossesScore` 对应摧毁/损失价值；两者不可混算。只在已识别的 protobuf 参与者 shape 中，对这四个省略字段和 `TeamId` 应用参考源码明确记载的零默认值；四个计数显式 null 保持未知，`TeamId` 显式 null 报结构错误。其他来源与 dataclass 默认不填零。
- `DamageDealt/DamageReceived` 为造成/承受伤害，`ObjectivesCaptured` 为占点数，`TotalSpawnedUnitScore` 为出动价值，`SupplyPointsConsumed` 为补给消耗；这些可选核心字段缺失时保持未知，不填零。
- 名字缺失为空字符串；地图表未核实，已有 `MapId` 显示 `地图#<ID>`。不下载单位表或游戏资产。
- `UnitData` 每条记录保留 ID、名称、`count=1`，以及模型需要的出动/死亡时间、退款与击杀数。`count` 仍是**出场记录数**，飞机重复出动可能多条，不是独立单位数或实际花费。`units_available` 区分缺失的 `UnitData` 与明确提供的空对象；兵种构成的基础花费估算另行标注。
- `complete` 是保守的**本地缓存完整度标记**：单局详情有结束时间、参与者且每人显式含 `UnitData` 对象才为真；不代表源端声明“数据最终完成”。列表始终为 false。结束不足五分钟的详情仍仅缓存 60 秒。
- 最近对局按稳定 ID 去重、按实际结束时间（缺少时用实际开始时间）降序，两者都缺少才放最后；相同 ID 优先保留参与者更多的记录。返回实际样本数，不冒称完整历史或完整排位样本。
- `players/matches` 必须显式返回数组。缺数组、错误对象、非 JSON/验证码响应不能当成“零搜索结果/零场对局”。

## 评分、预测与战报展示

`scoring.py` 使用 `assets/scoring-model.json`，移植 ba-dragon 固定提交的逐局评分逻辑与参数。近期战绩首列统一命名「评分」，按分数段着色，不显示称号。缺少必要数据时返回 `None` 并显示「暂无」，不补零。它与来源 ELO 分别展示，不是完整生涯评价，也不引入时间线或 Kalman 聚合算法。

`ba last` 的单局图片顶部展示基于双方赛前 ELO 的赛前预测及双队摘要；中部左侧兵种构成、右侧双方对比；底部展示队伍玩家数据。摘要区分赛前平均 ELO（`OldRating`）与赛后平均 ELO（`NewRating`）。预测是模型估算，不表示实际赛果。兵种构成按模型的单位角色与基础价格估算出动花费，缺少单位价格或记录时保留不完整说明，不冒充实际结算。

所有战报图片页脚内嵌 `assets/github-mark-black.svg` 或 `assets/github-mark-white.svg`，并包含 `timeance/astrbot_plugin_BA_stats` 与固定文字「数据来源 BATracer」。设置提示仍原样保留「数据来源 BATrace ，如有帮助请支持该平台」。

## 设置中心与诊断日志

入口为插件详情 → 可视化视图 → 设置中心，视图文件为 `views/settings/index.html`，脚本为同目录的 `settings.js`。表单与原生插件配置共用同一份设置，常用设置也可原生调整。诊断开关保存后即时生效；其他设置保存后需在插件页手动重载，页面不会自动重载或中断当前请求。

维护设置和诊断日志的内容默认折叠，诊断开关、日志状态和下载位于同一区域。排查时选择「诊断日志」并展开，开启诊断并保存后复现问题，再点击「下载诊断日志（ZIP）」。配置保存、状态查询与下载均通过宿主鉴权的插件 API 提供。

诊断记录总容量上限为 `30 * 1024 * 1024` 字节，面向用户显示为 30 MB，旧日志自动轮换。设置中心的 HTML/JS 由本插件独立编写，来源说明见 [THIRD_PARTY.md](THIRD_PARTY.md)。

## client 与 storage 契约

```python
client = BatraceClient(
    config=ClientConfig(
        enabled=False, base_url="https://bot.batrace.top",
        request_interval=1, concurrency=2, queue_size=20,
        request_timeout=20, wait_timeout=45, retries=1,
        cache_max_entries=1000, stale_hours=24,
        user_daily_limit=20, group_daily_limit=100,
    ),
    storage=storage,            # 已 open 的 Storage(data_dir)
    http_client=async_client,   # 可省略，首次在线请求时才内部创建
)
```

方法：`search(query, *, force=False) -> FetchResult[list[Player]]`、
`player(id, *, force=False) -> FetchResult[Player]`、
`recent(id, limit=5, *, force=False) -> FetchResult[list[Match]]`、
`match(id, *, force=False) -> FetchResult[Match]`、`clear_cache()`、`close()`。
`status()` 是同步安全摘要，返回 configured/closed/inflight/requests/cache_hits/stale_hits/last_error。
其中 `requests` 为本次运行的 HTTP 尝试数；每日持久用量由 `await storage.quota_usage(actor)` 提供。
`namespace` 只由数据源 `base_url` 生成，供持久绑定使用；内部缓存命名空间由 API 地址与固定客户端标识生成。更换 API 地址会隔离绑定与缓存，但不重置每日额度。

storage 为 duck type：`get_cache(key) -> (JSON payload, fetched_at, expires_at) | None`，过期但保留的记录也应返回；
`put_cache(key,payload,ttl)`、`clear_cache()`、`prune_cache(max_entries=...,max_age_seconds=...)`、`cache_count()`。
另需 `consume_request(actor=None, *, user_limit=20, group_limit=100)` 在每次 HTTP 尝试前原子检查并持久增加额度；`quota_usage(actor=None)` 只读返回 `day/global/user/group`。无 actor 时只有全局计数，user/group 为 None。
生产使用 SQLite Storage；client 不另建数据库，不触碰绑定。时间戳可用 Unix 秒或 aware datetime。
缓存存的是已校验的**原始 JSON**（player 为 search+analysis 的私有组合），读取后重新适配，不持久化 dataclass。
每次写入后按 client 上限清理，保留期限 604800 秒；创建 Storage 时也应传相同 `max_entries`，避免其内部提前按更小上限裁剪。

调度由一个插件实例共享一个 client：

- 所有真实请求统一经过全局起始间隔；并发上限和有界等待分别生效。最多接受 `concurrency + queue_size` 个不同查询任务，同一 `QuotaActor` 的同查询等待者共享同一个任务。不同用户或群不合并网络任务，已完成的缓存仍共享。
- 整体 `wait_timeout` 包括缓存读取、排队、节流、网络、退避和缓存写入；`request_timeout` 另限制单次 HTTP。未启用时不存在后台探活任务。
- `force=True` 绕过新鲜缓存。加入尚在读缓存的非 force 任务时会将共享任务提升为刷新；加入已经发送的上游任务仍只请求一次。它仍受同一队列、间隔和上游退避限制。
- 网络异常、超时、5xx 最多按配置重试，默认退避 30 秒；429 解析 `Retry-After` 秒数或 HTTP date，缺失时退避 30 秒。长于整体预算时不会突破预算；下一查询也保留已知退避窗口。
- 鉴权、404、参数错误、结构变化不重试。429 或临时服务故障可能返回旧缓存，但必须带原抓取时间和 `stale=True/stale_reason`；默认只允许抓取后 24 小时内的记录。**绑定端应拒绝 stale**。
- 清缓存有代次隔离：清理前发起、之后才完成的任务不会重新写入旧缓存；不删除绑定或额度记录。
- 一个等待者取消不影响其他合并等待者；最后一个等待者取消会取消并等待共享任务。`close()` 取消并等待所有运行中与排队任务，之后新调用抛 `ClientClosedError`。
- 内部创建的 HTTP client 由 `close()` 关闭；注入的 HTTP client 与 Storage 仍由调用者关闭。没有永久 worker/周期任务。

这些缓存与调度数值全部是**本地默认值**，上线应服从本插件实际获准的配额，不能据参考软件的使用策略推断许可或额度。

## 每日请求限额

全局 `GLOBAL_DAILY_LIMIT=100` 固定，不在 `ClientConfig`、`Settings` 或配置 Schema 中提供可调字段。用户默认 20、群默认 100，在 `limits.user_daily_limit` 和 `limits.group_daily_limit` 调整，范围 0–100，0 只取消该层限制。全部固定北京时间（UTC+8）0 点切换日期，不依赖显示时区或宿主机器时区；不提供管理员豁免。

AstrBot 适配器从事件的 `get_platform_id()/get_sender_id()/get_group_id()` 生成 `QuotaActor`，通过 `with client.request_scope(actor)` 覆盖整个业务调用；`ContextVar` 传播到子任务、后续接口及重试。身份不来自命令里的查询对象或 @ 目标。用户按平台实例跨群及私聊累计，群按平台实例和群 ID 汇总，全局覆盖本插件数据库内全部平台。

每次 `_request` 的每个 HTTP 尝试，在节流排队后、发送前调用 `Storage.consume_request`。SQLite 事务同时检查三层额度，再一起增加；不足则抛出 `QuotaExceededError`，其他层不增加。事务取得写锁后及提交前核对发送准入；退避发生变化或跨越北京时间日界时回滚，离开事务后重新排队，按最终准入日期记账。提交后直接交给 HTTP，不再进入退避等待。新鲜缓存判断后、网络退避前以及旧缓存回退前会只读检查额度，避免额度耗尽后因排队超时返回旧缓存。失败及重试计入；缓存命中、记账前取消、本地命令和图像渲染不计入。为防止断电或取消造成超额，已持久记账的尝试不退款；很小的提交与交给 HTTP 之间的中断窗口可能保守多记一次。

查询概览通常包含多个 HTTP，部分请求已发出后额度不足，会保留已用次数并中止剩余调用；绑定逻辑不会据不完整结果修改原绑定。额度不足时可读取新鲜缓存，但不会以 stale 回退隐藏该错误。网络合并仅限同一用户、同一群，防止其他调用者的强制刷新扣到原发起者。

额度表独立于数据源及缓存命名空间；重开数据库、清缓存、更换 API 地址、调整限制不清零。无 Storage 的独立适配器只有进程内全局计数，`request_scope` 拒绝该模式；生产插件始终使用持久 Storage。`ba quota` 为无需启用在线查询的本地只读查询。

## 失败语义

| 异常/code | 含义 | 可用 stale |
| --- | --- | --- |
| `NotConfiguredError / not_configured` | 在线查询未开启或 API 地址无效，未发请求 | 否 |
| `AuthenticationError / authentication` | 401/403 或明确验证码页 | 否 |
| `NoDataError / no_data` | 404 + JSON，暂无收录；不证明账号不存在 | 否 |
| `IdentityMismatchError / identity_mismatch` | 规范 ID 未回显，或近期列表包含其他玩家对局 | 否 |
| `SchemaError / schema` | 字段结构、类型、时间、数值或内容格式不符合适配契约 | 否 |
| `RateLimitError / rate_limited` | 429，可读取 retry_after | 是 |
| `RequestTimeoutError / timeout` | 单次超时或整体等待用尽 | 是 |
| `UpstreamError / upstream` | 连接故障或 5xx | 是 |
| `QueueFullError / queue_full` | 本地并发与等待名额用尽 | 否 |
| `QuotaExceededError / daily_quota` | 本地用户、群或全插件今日额度不足；scope/limit/used 表示触发范围 | 否 |
| `ClientClosedError / closed` | 关闭中或已关闭 | 否 |

搜索成功返回空数组才表示“当前查询没有候选”；其他非成功状态不改写成玩家不存在，不清绑定。
其他 JSON 4xx 抛 `ClientError / client_error` 并只保留 HTTP 状态，不输出上游任意错误正文或请求凭据。

## 测试与上线前核对

`tests/test_client.py`、`tests/test_models.py` 的 fixture 全部为**明确标识的合成数据**，HTTP 使用 `httpx.MockTransport`，SQLite 使用临时目录。
它们验证契约、缓存持久化、同请求合并、有界队列、节流/并发、超时、限流/重试、取消/关闭、force 竞争、规范身份和未知值行为；不证明 BATrace 当前真实响应吻合。

离线运行：`python -m pytest -q`。新增每日限额测试覆盖持久计数、北京时间日界、并发边界、失败/重试计数、缓存免费与身份传播；当前完整结果见 [验证记录](VALIDATION.md)。所有真实 SQLite 重开测试也使用临时目录，没有访问真实账号或 BATrace。
真实联调与实际平台验证均未执行。框架、service、render、平台与总验证详见主代理交付记录。

真实联调仍需核对 API origin、固定客户端标识的响应、允许接口与查询范围、实际并发/请求间隔/退避与缓存政策。
随后在明确授权的最小调用量内依次核对：

1. 以已知玩家名搜索，确认规范 ID、updated_at 格式/时区和截断规则。
2. 核实 `q=<规范ID>` 是否可靠回显；确认有 ID 的权威详情能力后再验收绑定 revalidation。
3. 核对分析评分时序、最新评分语义、列表实际上限，以及列表/详情的字段大小写、秒单位和 protobuf 省略默认值。
4. 获取权威模式枚举与真实胜负字段；没有证据则继续显示未知，不以 ELO 涨跌替代。
5. 在授权测试账号上检查玩家/单局页面链接和真实无收录、鉴权、限流响应；不得故意压测上游来测限流。
6. 再由主代理在明确授权的 AstrBot 测试会话验收查询与绑定；未完成前保持“真实接口联调待完成”。

## 许可与独立实现

该提交的 ba-dragon 主源码为 MIT，LICENSE 载明 AlexanderHYu 与沿用部分的 Zola 版权；其 README 另外说明可选录像使用 GPL FFmpeg。
Python 请求调度、数据模型和业务适配由本插件实现；`scoring.py` 的移植部分与 `assets/scoring-model.json` 使用 ba-dragon 的 MIT 评分逻辑和模型参数，并保留其来源与版权。未引入录像工具或 FFmpeg。
完整上游许可原样保留于 [BA_DRAGON_LICENSE.txt](BA_DRAGON_LICENSE.txt)，安装、源码与 Gemini 包均随附。本仓库根 `LICENSE` 继续为 Apache-2.0；具体第三方来源与移植范围见 [THIRD_PARTY.md](THIRD_PARTY.md)。
