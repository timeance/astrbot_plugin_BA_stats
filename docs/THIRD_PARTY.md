# 来源与许可

本项目既有许可证为 Apache-2.0，原始仓库基线为 `c4272e87413734ec317fe937faa868bbbf70eb08`。新增代码按该许可证提供；第三方服务数据、产品名称和交接图像不因本项目代码许可证而获得额外授权。

| 来源 | 本次用途 | 固定版本 |
| --- | --- | --- |
| [AstrBot](https://github.com/astrbotdevs/astrbot) | 原生插件 API、ZIP 格式与配置 Schema 核对 | v4.29.4，`61335414433db721b3f7ecc93f90deafe0a59d78` |
| [AstrBot T2I](https://github.com/AstrBotDevs/astrbot-t2i-service) | 核对 `viewport_width`、像素比及 JPEG 质量参数 | `bcc08e47274c69e5ace19bbf738b95260ce17ca7` |
| [ba-dragon](https://github.com/AlexanderHYu/ba-dragon) | BATrace 请求形态、ID 与字段语义研究 | 见 API_NOTES.md |
| [wows-stats-bot](https://github.com/benx1n/wows-stats-bot) | README 中绑定与 @ 查询的交互研究，未移植 HoshinoBot 实现 | master，`80c86970685887578054e61e1a76d1589ea1dc31` |
| [BATrace](https://dash.batrace.top/) | 预期战绩数据来源 | 自身接入许可、配额及真实字段待确认 |
| [astrbot_plugin_openai_native](https://github.com/timeance/astrbot_plugin_openai_native) | 参考用户跨群累计、群共享额度、配置提示与额度查询；按本任务要求统一私聊用户计数和北京时间日界 | main，`98bd76784bb2a35df0aea968138f87fd4d9ee207`；核对 README、配置 Schema、事件身份提取 |

研究不等于获得服务调用许可。插件不内置参考项目的获准标识或密钥。测试负载与卡片预览为明确标识的合成样本。

未复制参考项目的非琐碎源码、图片、字体或样式到插件。依赖由宿主环境安装，安装 ZIP 不附带第三方库；各依赖许可证以其发布包为准。预览工具使用本地 Playwright/Chromium，仅用于开发验收，运行插件不需要安装它们。

`logo.png` 与 `assets/readme-logo.png` 原样来自用户交接包，交付时核对 SHA-256 一致。它们的授权与来源由交接提供方负责说明，本项目没有代替权利人作版权声明。
