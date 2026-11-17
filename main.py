"""AstrBot v4.29.4 接口适配；命令只经宿主的唤醒及指令过滤器进入。"""
from __future__ import annotations

import asyncio
from dataclasses import fields
import hashlib
from pathlib import Path
import re
import shutil
import time

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Plain
from astrbot.api.star import Context, Star, StarTools

from .client import BatraceClient, ClientConfig, ClientError
from .commands import mention_token, parse_command
from .quota import GLOBAL_DAILY_LIMIT, QuotaActor
from .render import Renderer
from .service import Caller, StatsService
from .settings import Settings
from .storage import Storage


def extract_arguments(event, prefixes):
    """Rebuild Plain/At components; keep quoted nickname spacing and true IDs."""
    parts, mentions = [], []
    for component in event.get_messages():
        if isinstance(component, Plain):
            # NUL separates trusted components; ordinary @user: names stay literal.
            if "\x00" in component.text:
                raise ValueError("命令文本包含不支持的控制字符。")
            parts.append(component.text)
        elif isinstance(component, At):
            user = str(component.qq)
            if user == str(event.get_self_id()) and not "".join(parts).strip():
                continue
            if user == "all":
                raise ValueError("不能使用 @全体成员 查询玩家。")
            parts.append(f" {mention_token(len(mentions))} ")
            mentions.append(user)
    raw = "".join(parts).strip()
    choices = sorted({str(p) for p in prefixes if p}, key=len, reverse=True)
    for prefix in choices + [""]:
        match = re.match(r"^" + re.escape(prefix) + r"ba(?:\s+|$)", raw, re.IGNORECASE)
        if match:
            return raw[match.end():], mentions
    # Some adapters expose normalized text with command prefixes already removed.
    if not mentions:
        match = re.match(r"^ba(?:\s+|$)", event.get_message_str().strip(), re.IGNORECASE)
        if match:
            return event.get_message_str().strip()[match.end():], []
    raise ValueError("未能识别命令内容，请确认使用了当前配置的 AstrBot 命令前缀后重试。")


def quota_actor(event):
    """Account to the actual sender and group, never the queried/@ player."""
    def identifier(value, required=True):
        if value is None and not required:
            return ""
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise ValueError("无法确认消息的用户或群身份。")
        result = str(value).strip()
        if required and not result:
            raise ValueError("无法确认消息的用户或群身份。")
        return result
    return QuotaActor(identifier(event.get_platform_id()), identifier(event.get_sender_id()),
                      identifier(event.get_group_id(), required=False))


class BAStatsPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.settings = Settings.from_config(config)
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_ba_stats")
        self.storage = Storage(self.data_dir, max_entries=self.settings.cache_max_entries)
        values = {f.name: getattr(self.settings, f.name) for f in fields(ClientConfig)}
        self.client = BatraceClient(ClientConfig(**values), storage=self.storage)
        self.service = StatsService(self.client, self.storage, self.settings)
        self.renderer = Renderer(self.settings.theme, self.settings.timezone)
        self._images = self.data_dir / "images"
        self._image_lock = asyncio.Lock()
        self._tasks = set()
        self._closed = False

    async def initialize(self):
        await self.storage.open()
        self._images.mkdir(parents=True, exist_ok=True)
        self._prune_images()

    async def _run(self, awaitable):
        if self._closed:
            awaitable.close()
            raise asyncio.CancelledError
        task = asyncio.create_task(awaitable)
        self._tasks.add(task)
        try:
            return await asyncio.wait_for(task, self.settings.wait_timeout)
        finally:
            self._tasks.discard(task)

    def _prune_images(self):
        files = sorted(self._images.glob("*.jpg"), key=lambda p: p.stat().st_mtime, reverse=True)
        for index, path in enumerate(files):
            if index >= 48 or time.time() - path.stat().st_mtime > 86400:
                path.unlink(missing_ok=True)

    async def _image(self, view, umo):
        html = self.renderer.template(view)
        # Scope renderer endpoint/session too: different AstrBot sessions can use
        # different render services. Content itself remains the same result.
        digest = hashlib.sha256((umo + html).encode()).hexdigest()
        destination = self._images / f"{digest}.jpg"
        async with self._image_lock:
            if destination.exists() and time.time() - destination.stat().st_mtime <= 86400:
                return str(destination)
            # Final HTML is DATA, never a second Jinja template. This preserves
            # literal {{...}} in nicknames after the first HTML escaping pass.
            source = await self.html_render(
                "{{ body | safe }}", {"body": html}, return_url=False,
                options={"type": "jpeg", "quality": 85, "full_page": True,
                         "viewport_width": 390, "device_scale_factor_level": "ultra"}, umo=umo)
            if not Path(source).is_file():
                raise ValueError("渲染服务未返回本地图片。")
            shutil.copyfile(source, destination)
            self._prune_images()
            return str(destination)

    async def _status(self):
        status = self.client.status()
        usage = await self.storage.quota_usage()
        labels = {"authentication": "鉴权失败", "rate_limited": "限流", "timeout": "超时",
                  "schema": "响应结构变化", "identity_mismatch": "身份校验失败",
                  "no_data": "暂无收录", "upstream": "上游暂不可用", "queue_full": "队列已满",
                  "not_configured": "接入未配置", "closed": "客户端已关闭", "daily_quota": "每日额度已用完"}
        return (f"断箭战报 v0.1.0\n接入配置：{'已填写（不代表联调通过）' if status['configured'] else '未配置'}\n"
                f"今日总请求：{usage['global']}/{GLOBAL_DAILY_LIMIT}（固定上限）；北京时间 0 点重置\n"
                f"本次运行请求：{status['requests']}；进行中：{status['inflight']}\n"
                f"API 缓存：{await self.storage.cache_count()} 条；命中：{status['cache_hits']}；旧缓存：{status['stale_hits']}\n"
                f"图片缓存：{len(list(self._images.glob('*.jpg')))} 张\n"
                f"最近错误：{labels.get(status['last_error'], '无' if not status['last_error'] else '请求失败')}\n"
                "真实接口与 QQ 平台验收待完成。")

    async def _quota(self, actor):
        usage = await self.storage.quota_usage(actor)
        def line(label, used, limit):
            if limit == 0:
                return f"{label}：已用 {used} 次，不单独限制"
            return f"{label}：已用 {used}/{limit} 次，剩余 {max(0, limit - used)} 次"
        lines = [f"今日请求额度 · {usage['day']}（北京时间）",
                 line("个人", usage['user'], self.settings.user_daily_limit)]
        if actor.group:
            lines.append(line("本群", usage['group'], self.settings.group_daily_limit))
        lines.extend([line("全插件", usage['global'], GLOBAL_DAILY_LIMIT),
                      "北京时间每天 0 点重置；个人额度跨群及私聊累计。",
                      "按实际发出的上游 HTTP 请求计数（含重试与失败）；命中缓存、本地命令及图片渲染不计入。"])
        return "\n".join(lines)

    @filter.command("ba", alias={"BA", "Ba", "bA"})
    async def ba(self, event: AstrMessageEvent):
        if self._closed:
            return
        event.stop_event()
        config = self.context.get_config(event.unified_msg_origin)
        prefixes = config.get("wake_prefix", ["/"]) if config is not None else ["/"]
        prefix = str(prefixes[0]) if prefixes else ""
        try:
            text, mentions = extract_arguments(event, prefixes)
            request = parse_command(text, mentions, self.settings.default_limit,
                                    self.settings.max_limit, self.settings.chinese_aliases)
            caller = Caller(str(event.get_platform_id()), event.unified_msg_origin,
                            str(event.get_sender_id()), prefix + "ba")
            if request.action == "quota":
                yield event.plain_result(await self._quota(quota_actor(event)))
                return
            if request.action in {"status", "cache_clear"}:
                self.service.pending.begin(caller.scope)
                if not event.is_admin():
                    yield event.plain_result("此命令仅供 AstrBot 管理员使用。")
                    return
                if request.action == "status":
                    yield event.plain_result(await self._status())
                else:
                    await self.client.clear_cache()
                    async with self._image_lock:
                        for path in self._images.glob("*.jpg"):
                            path.unlink(missing_ok=True)
                    yield event.plain_result("已清理 API 缓存与图片缓存；账号绑定及今日已用额度不受影响。")
                return
            with self.client.request_scope(quota_actor(event)):
                response = await self._run(self.service.execute(request, caller))
            if response.text:
                yield event.plain_result(response.text)
            if response.view is not None:
                for view in self.renderer.views(response.view):
                    if self.settings.image_enabled:
                        try:
                            path = await self._run(self._image(view, event.unified_msg_origin))
                        except (Exception,):
                            self.logger.warning("断箭战报图片渲染失败，已使用相同查询结果回复文字。")
                        else:
                            yield event.image_result(path)
                            continue
                    yield event.plain_result(self.renderer.text(view))
        except (ClientError, ValueError) as exc:
            yield event.plain_result(str(exc))
        except asyncio.TimeoutError:
            yield event.plain_result("查询等待超时，请稍后重试。")
        except Exception as exc:
            # Neither response bodies nor credential-bearing URLs enter logs.
            self.logger.error("断箭战报请求失败（%s）。", type(exc).__name__)
            yield event.plain_result("本次查询未完成，请稍后重试；如持续出现，请管理员使用 ba status 检查插件状态。")

    async def terminate(self):
        self._closed = True
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self.service.close()
        await self.client.close()
        await self.storage.close()
