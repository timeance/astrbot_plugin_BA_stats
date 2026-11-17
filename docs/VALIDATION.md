# 验证记录

验证日期：2026-11-17（Asia/Hong_Kong）。版本：v0.1.2。

## 本次结果

| 项目 | 结果 |
| --- | --- |
| 完整离线测试 | **635 passed，25.90 秒** |
| 评分与固定上游 TypeScript 对照 | **320 个合成样本一致**；中间值误差小于 1e-12 |
| 三类战报本地渲染 | 18 组 HTML/文字、36 张 PNG；无横向溢出或外部请求 |
| 设置中心本地交互 | 深色 390px、浅色 1100px 两组通过；4 张预览 |
| 依赖兼容性 | `uv pip check`：21 个已安装包兼容 |
| 完整 AstrBot 服务、真实渲染服务 | 未启动；按 v4.29.4 固定源码核对 API，使用事件与桥接桩验证 |
| BATrace / QQ 实际联调 | 开发环境未调用；用户已提供候选结果和图片超时日志 |
| 远端 PR / CI | 以对应 PR 的实时结果为准；本记录不表示已合并或部署 |

## 战报、评分与数据口径

- 玩家概览、近期列表、单局战报共用展示数据和格式；精简重复说明，保留样本范围、未知值和旧缓存标识。
- 分值 K/D 使用摧毁分与损失分；击杀/阵亡是单位数量，伤害/承伤是另一组数据。出兵花费、补给消耗保留有效小数，不被整数格式误判为缺失。
- 分析接口只以明确布尔 `won` 及相同对局 ID 提供目标玩家胜负；不将 ELO 涨跌当成展示的权威胜负。单局页保留已知目标胜负。
- 分析样本与近期样本分别标明；重复和相互矛盾的分析结果不重复计入分母。地图、单位按来源 ID 关联。
- 「评分」移植固定 ba-dragon 模型的逐局 career 计算，按分数着色，不展示称号。保留 MIT 来源与完整许可。
- Oracle 包含原始 160 个样本，以及逐个删除 `StartTime` 的 160 个边界样本。开始/结束时间分别保留；不以结束时间减时长反推开始时间。近期列表按真实结束时间、缺少时按真实开始时间排序，两者都缺少才放最后。
- 赛前预测使用 A/B 队全部必要赛前 ELO；兵种构成按已收录单位基础价格估算。缺少成员单位数据或存在未知价格时，不把已识别部分重新归一化为完整构成。
- 单局图片按 900 CSS 像素渲染：顶部预测，中部左右对比/构成，底部玩家。其他卡片为 390 CSS 像素；运行仍使用 JPEG 85、ultra 像素比。
- 所有图片页脚为本地内嵌 GitHub 头像、`timeance/astrbot_plugin_BA_stats` 和「数据来源 BATracer」。不在运行时请求头像。

## 设置中心与诊断

- 独立视图 `views/settings/`，与原生配置共用 AstrBotConfig；常用、额度、维护、诊断分组。跟随宿主深浅主题，维护和诊断内容默认折叠。
- 按 schema 描述与白名单校验字段，API origin 与客户端复用同一 HTTPS 校验规则；不返回或接受旧客户端标识、令牌、全局额度字段。
- 保存检查磁盘配置版本，冲突返回 409 并保留页面草稿。普通设置等待手动重载，页面显示待生效项；诊断开关保存后即时应用，保存不重启业务对象或中断在途请求。
- API 使用 AstrBot 登录鉴权，插件额外检查用户名。未登录、已关闭、无效 JSON、非法类型/范围、保存失败及旧实例卸载有回归测试。下载不通过公开静态目录。
- 诊断仅记录允许的事件与字段：请求耗时/状态、渲染及图片发送阶段、文件类型/大小、受控错误分类等。日志上限为 `30 * 1024 * 1024` 字节，旧段自动淘汰；关闭后可下载既有日志。
- 记录器覆盖轮换、并发导出、关闭、磁盘失败及脱敏；不记录令牌、请求头、完整响应、聊天正文或图片内容。导出为 ZIP，不清空原日志。
- 本地浏览器验证保存开关、下载路由、409 草稿保留、重新读取后保留草稿、待重载提示及固定总额度。合成桥接不替代真实宿主收发；常用设置全页预览将悬浮保存栏放到文档末尾，交互测试使用原样 CSS。

## 图片超时的已知边界

用户环境为 AstrBot 4.29.4、NapCat.Core 4.18.18；其他插件能发图片。用户日志显示 OneBot `retcode=1200`、QQ `sendMsg` 确认超时。

v0.1.1 已改为等待图片发送结果，并在未获成功确认时使用同一次查询回复文字、关闭文字自动转图。v0.1.2 增加受控诊断信息，**未证明 QQ 超时根因已修复**。本地卡片可渲染不等于实际 QQ 可接收；需在用户实例打开诊断并复现后检查日志。

## 执行与证据

```powershell
& ../.venv/Scripts/python.exe -X utf8 -m pytest -q --basetemp=../tmp/pytest-v012-final --junitxml=../validation/pytest-v0.1.2.xml
# 635 passed in 25.90s

& ../.venv/Scripts/python.exe -X utf8 -B tools/render_previews.py --screenshots
# 18 HTML/text pairs; 36 screenshots; 0 layout/network failures

& ../.venv/Scripts/python.exe -X utf8 -B tools/check_settings_view.py
# 2 local browser scenarios passed; 4 screenshots; no external requests

uv --cache-dir ../tmp/uv-cache pip check --python ../.venv/Scripts/python.exe
# All installed packages are compatible
```

主代理目视检查了近期浅色手机卡、玩家概览、单局深色宽图、常用设置浅色桌面页和诊断深色手机页。卡片矩阵含正常、长昵称、缺失数据三组，深浅两色，900/390px；预览均标明合成样本。浏览器拦截外部请求，只使用本地生成内容，没有使用外部账号或登录态。

会话目录：`H:/Codex/01a1e923-f766-7562-b804-35854a994fc4`。Python 3.11.4；Playwright 1.65.2，本地 Chromium 159.0.8154.6。主测试使用 httpx.MockTransport、临时 SQLite 和 AstrBot 桩；没有访问真实战绩、QQ 账号、数据库或运行配置。

## 固定来源与交付

- AstrBot v4.29.4：`61335414433db721b3f7ecc93f90deafe0a59d78`。
- ba-dragon：`072dc9bc2d73ffc87000c1d3db4c0f9b4dcab549`。
- AstrBot T2I：`bcc08e47274c69e5ace19bbf738b95260ce17ca7`。
- 设置中心仅参考 `anka-afk/astrbot_plugin_meme_manager` 的分组、折叠与共享配置交互，代码独立实现。

`tools/build_delivery.py --output ../delivery/v0.1.2` 生成安装 ZIP、Gemini 文案优化 ZIP 及总交付 ZIP。安装包包含原生入口、元数据、配置、模板、设置视图、标题翻译、模型和许可；不含测试环境、个人数据库、缓存、真实凭据或参考项目源码。文案包覆盖提示、配置、HTML/JS、README、CHANGELOG，保留占位符、固定文字与许可约束。各 ZIP 用 `testzip()` 校验并附 SHA-256。

原 Logo 保持不变：

- `logo.png`：`7b93b4513cc84535155e4e4edf63b7a36b17321823e05ce0b494369d03b6e616`
- `assets/readme-logo.png`：`2acb2855440c8ec609370789866857ca80e9c4c73a1cd1379f9b23d5c15e65a7`

v0.1.1 的命令正文/真实 At、超过 3 人改用 ID 绑定、发送失败文字回退，以及持久额度、北京时间日界、并发扣额、缓存与绑定隔离回归均包含在本次完整测试中。历史文案导入及早期验证见 Git 历史。
