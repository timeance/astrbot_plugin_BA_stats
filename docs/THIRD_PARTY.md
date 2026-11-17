# 来源与许可

本项目既有许可证为 Apache-2.0，原始仓库基线为 `c4272e87413734ec317fe937faa868bbbf70eb08`。项目自有代码按该许可证提供；ba-dragon 移植部分及模型保留上游 MIT 许可。第三方服务数据、产品名称和交接图像不因本项目代码许可证而获得额外授权。

| 来源 | 本次用途 | 固定版本 |
| --- | --- | --- |
| [AstrBot](https://github.com/astrbotdevs/astrbot) | 原生插件 API、ZIP 格式与配置 Schema 核对 | v4.29.4，`61335414433db721b3f7ecc93f90deafe0a59d78` |
| [AstrBot T2I](https://github.com/AstrBotDevs/astrbot-t2i-service) | 核对 `viewport_width`、像素比及 JPEG 质量参数 | `bcc08e47274c69e5ace19bbf738b95260ce17ca7` |
| [ba-dragon](https://github.com/AlexanderHYu/ba-dragon) | BATrace 接口参考；逐局评分、玩家综合评分、赛前 ELO 预测及兵种构成估算的 MIT 移植 | `072dc9bc2d73ffc87000c1d3db4c0f9b4dcab549`，详见下文及 API_NOTES.md |
| [wows-stats-bot](https://github.com/benx1n/wows-stats-bot) | README 中绑定与 @ 查询的交互研究，未移植 HoshinoBot 实现 | master，`80c86970685887578054e61e1a76d1589ea1dc31` |
| [BATrace](https://dash.batrace.top/) | 预期战绩数据来源 | 自身接入许可、配额及真实字段待确认 |
| [astrbot_plugin_openai_native](https://github.com/timeance/astrbot_plugin_openai_native) | 参考用户跨群累计、群共享额度、配置提示与额度查询；按本任务要求统一私聊用户计数和北京时间日界 | main，`98bd76784bb2a35df0aea968138f87fd4d9ee207`；核对 README、配置 Schema、事件身份提取 |

研究不等于获得服务调用许可。插件不内置参考项目的获准标识或密钥。测试负载与卡片预览为明确标识的合成样本。

## ba-dragon 评分与模型

`scoring.py` 将固定提交中的 `src/shared/dragon/score.ts`、`roles.ts`、`model.ts` 相关逻辑移植为 Python；`assets/scoring-model.json` 从同一提交的 `src/shared/dragon/model.json` 提取评分参数，单位表仅保留角色和基础价格。模型内的 `source` 记录仓库、提交、来源文件和完整 MIT 文本。

玩家概览使用 `computeDragonScore` 的 Kalman 聚合、玩家分位表、分档和四个表现分项；分档称号取自 `src/renderer/src/features/players/PlayerDetail.tsx`。第五项「实际胜率」由插件服务根据明确的 `won` 计算并传入评分模块。近期战绩保持逐局评分。未移植单局奖项、时间线或实力 ELO；这些模型分数不代表官方评价。

[BA_DRAGON_LICENSE.txt](BA_DRAGON_LICENSE.txt) 和模型的 `source.licenseText` 均原样保留固定提交的 `LICENSE`，包括 `Copyright (c) 2026 AlexanderHYu` 及 `Copyright (c) 2025-2026 Zola (Zawinzala)` 与原文件的沿用部分说明。分发这些移植代码或模型时，安装包、源码和 Gemini 文案包均应保留完整上游版权及 MIT 许可；文案润色不修改该文本。

未移植参考项目的录像工具或 FFmpeg。其 README 提到的可选 GPL FFmpeg 不随本插件分发。

## 依赖与图像

`views/settings/` 中的 HTML/JS 由本插件独立编写，设置中心仅参考 `meme_manager` 的交互组织，未复制其源码。

依赖由宿主环境安装，安装 ZIP 不附带第三方库；各依赖许可证以其发布包为准。预览工具使用本地 Playwright/Chromium，仅用于开发验收，运行插件不需要安装它们。

`assets/github-mark-black.svg` 与 `assets/github-mark-white.svg` 来自 GitHub 官方 [Logo 素材包](https://brand.github.com/GitHub_Logos.zip)，用于图片页脚的项目链接。按主题内嵌对应黑白版本，与 `timeance/astrbot_plugin_BA_stats` 及「数据来源 BATracer」一同显示。

`logo.png` 与 `assets/readme-logo.png` 原样来自用户交接包，交付时核对 SHA-256 一致。它们的授权与来源由交接提供方负责说明，本项目没有代替权利人作版权声明。
