"""AstrBot v4.29.4 接口适配；命令只经宿主的唤醒及指令过滤器进入。"""
from __future__ import annotations

import asyncio
from dataclasses import fields, replace
import hashlib
from pathlib import Path
import re
import time

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Plain
from astrbot.api.star import Context, Star, StarTools
from astrbot.api.web import error_response, json_response, request, stream_response

from .client import BatraceClient, ClientConfig, ClientError
from .commands import mention_token, parse_command
from .diagnostics import DiagnosticLog, image_error_metadata
from .image_transport import (
    IMAGE_SUFFIXES, MAX_IMAGE_BYTES, ImageArtifact, ImageTransportError, download_image, is_image_url, validate_image,
)
from .quota import GLOBAL_DAILY_LIMIT, QuotaActor
from .render import Renderer
from .service import Caller, StatsService
from .settings import Settings
from .storage import Storage
from .web_settings import SettingsError, WebSettings


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
        match = re.match(r"^" + re.escape(prefix) + r"\s*ba(?:\s+|$)", raw, re.IGNORECASE)
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
        self.diagnostics = DiagnosticLog(self.data_dir / "diagnostics", enabled=self.settings.diagnostics_enabled)
        self.storage = Storage(self.data_dir, max_entries=self.settings.cache_max_entries)
        values = {f.name: getattr(self.settings, f.name) for f in fields(ClientConfig)}
        self.client = BatraceClient(ClientConfig(**values), storage=self.storage)
        self.client.diagnostics = self.diagnostics.record
        self.service = StatsService(self.client, self.storage, self.settings)
        self.renderer = Renderer(self.settings.theme, self.settings.timezone)
        self._images = self.data_dir / "images"
        self._image_lock = asyncio.Lock()
        self._tasks = set()
        self._closed = False
        self.web_settings = WebSettings(config, self.settings, self.diagnostics)
        # v4.29.4 exposes these through authenticated /api/v1/plugins/extensions/.
        self._web_routes = {
            "/astrbot_plugin_ba_stats/diagnostics/status": (self._diagnostics_status, ["GET"]),
            "/astrbot_plugin_ba_stats/diagnostics/download": (self._download_diagnostics, ["GET"]),
            "/astrbot_plugin_ba_stats/settings/config": (self._settings_config, ["GET", "POST"]),
        }
        for route, (handler, methods) in self._web_routes.items():
            context.register_web_api(route, handler, methods, "断箭战报设置中心")

    async def _settings_config(self):
        if not request.username:
            return error_response("请先登录 AstrBot 管理面板。", status_code=401,
                                  headers={"Cache-Control": "no-store"})
        if self._closed:
            return error_response("插件已关闭，请重载插件后重试。", status_code=503,
                                  headers={"Cache-Control": "no-store"})
        try:
            payload = await request.json() if request.method == "POST" else None
        except (ValueError, TypeError):
            return error_response("请求不是有效的 JSON 对象。", status_code=400,
                                  headers={"Cache-Control": "no-store"})
        if self._closed:
            return error_response("插件已关闭，请刷新后重试。", status_code=503,
                                  headers={"Cache-Control": "no-store"})
        try:
            data = self.web_settings.save(payload) if request.method == "POST" else self.web_settings.snapshot()
        except SettingsError as exc:
            return error_response(str(exc), status_code=exc.status_code,
                                  headers={"Cache-Control": "no-store"})
        if request.method == "POST":
            self.settings = replace(self.settings, diagnostics_enabled=self.diagnostics.enabled)
        return json_response(data, headers={"Cache-Control": "no-store"})

    async def _diagnostics_status(self):
        if not request.username:
            return error_response("请先登录 AstrBot 管理面板。", status_code=401,
                                  headers={"Cache-Control": "no-store"})
        if self._closed:
            return error_response("插件已关闭，请重载插件后重试。", status_code=503,
                                  headers={"Cache-Control": "no-store"})
        return json_response(self.diagnostics.stats(), headers={"Cache-Control": "no-store"})

    async def _download_diagnostics(self):
        if not request.username:
            return error_response("请先登录 AstrBot 管理面板。", status_code=401,
                                  headers={"Cache-Control": "no-store"})
        if self._closed:
            return error_response("插件已关闭，请重载插件后重试。", status_code=503,
                                  headers={"Cache-Control": "no-store"})
        try:
            data = await asyncio.to_thread(self.diagnostics.export_zip)
        except OSError:
            return error_response("读取诊断日志失败，请检查日志目录后重试。", status_code=500,
                                  headers={"Cache-Control": "no-store"})
        return stream_response(
            iter([data]), content_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="ba-stats-diagnostics.zip"',
                     "Cache-Control": "no-store"},
        )

    async def initialize(self):
        await self.storage.open()
        self._images.mkdir(parents=True, exist_ok=True)
        self._prune_images()
        self.diagnostics.record("plugin_start", status="0.1.4")

    async def _run(self, awaitable, *, timed=True):
        if self._closed:
            awaitable.close()
            raise asyncio.CancelledError
        task = asyncio.create_task(awaitable)
        self._tasks.add(task)
        try:
            return await asyncio.wait_for(task, self.settings.wait_timeout) if timed else await task
        finally:
            self._tasks.discard(task)

    def _image_files(self):
        return [p for p in self._images.iterdir()
                if p.is_file() and p.suffix.lower() in {*IMAGE_SUFFIXES.values(), ".jpeg"}]

    def _prune_images(self):
        files = sorted(self._image_files(), key=lambda p: p.stat().st_mtime, reverse=True)
        for index, path in enumerate(files):
            if index >= 48 or time.time() - path.stat().st_mtime > 86400:
                path.unlink(missing_ok=True)

    async def _image(self, view, umo):
        mode = self.settings.image_send_mode
        progress = {"stage": "generate"}
        try:
            # Keep the timeout inside the stage-aware operation so a download
            # timeout is not mislabeled as a renderer failure.
            async with asyncio.timeout(self.settings.wait_timeout):
                return await self._prepare_image(view, umo, mode, progress)
        except ImageTransportError:
            raise
        except Exception as exc:
            reason = {"generate": "generation_failed", "download": "download_failed",
                      "validate": "validation_failed", "cache": "cache_failed"}[progress["stage"]]
            raise ImageTransportError(progress["stage"], "timeout" if isinstance(exc, TimeoutError) else reason) from exc

    async def _prepare_image(self, view, umo, mode, progress):
        html = self.renderer.template(view)
        digest = hashlib.sha256((umo + html).encode()).hexdigest()
        async with self._image_lock:
            progress["stage"] = "cache"
            for cached in self._image_files():
                if cached.stem != digest:
                    continue
                try:
                    if cached.stat().st_size > MAX_IMAGE_BYTES:
                        raise ImageTransportError("cache", "invalid_image_size")
                    data = cached.read_bytes()
                    image_type, width, height = await asyncio.to_thread(validate_image, data)
                except (ImageTransportError, OSError) as exc:
                    exc.image_bytes = cached.stat().st_size if cached.exists() else None
                    self._log_image_failure(exc, mode, "cache")
                    cached.unlink(missing_ok=True)
                    continue
                # Old releases could label PNG bytes as .jpg. Normalize the
                # artifact to its decoded format without changing its bytes.
                correct_path = cached.with_suffix(IMAGE_SUFFIXES[image_type])
                if cached != correct_path:
                    cached.replace(correct_path)
                    cached = correct_path
                # URL mode always regenerates: signed URLs are not persisted.
                if mode != "url" and time.time() - cached.stat().st_mtime <= 86400:
                    image = ImageArtifact(cached, image_type, width, height, len(data))
                    self.diagnostics.record("image_cache", mode=mode, stage="cache", **image.metadata)
                    return image
            # Final HTML is DATA, never a second Jinja template. This preserves
            # literal {{...}} in nicknames after the first HTML escaping pass.
            progress["stage"] = "generate"
            source = await self.html_render(
                "{{ body | safe }}", {"body": html}, return_url=True,
                options={"type": "jpeg", "quality": 85, "full_page": True,
                         "viewport_width": 900 if view.get("kind") == "match" else 390, "device_scale_factor_level": "ultra"}, umo=umo)
            url = source if is_image_url(source) else None
            content_type = ""
            if url:
                progress["stage"] = "download"
                try:
                    data, content_type = await download_image(url, self.settings.wait_timeout)
                except ImageTransportError:
                    raise
                except Exception as exc:
                    raise ImageTransportError("download", "download_failed") from exc
            else:
                if mode == "url":
                    raise ImageTransportError("generate", "url_unavailable")
                if not isinstance(source, (str, Path)) or not Path(source).is_file():
                    raise ImageTransportError("generate", "local_path_unavailable")
                if Path(source).stat().st_size > MAX_IMAGE_BYTES:
                    raise ImageTransportError("validate", "invalid_image_size")
                data = Path(source).read_bytes()
            progress["stage"] = "validate"
            try:
                image_type, width, height = await asyncio.to_thread(validate_image, data)
            except ImageTransportError as exc:
                exc.image_bytes, exc.content_type = len(data), content_type
                raise
            progress["stage"] = "cache"
            destination = self._images / f"{digest}{IMAGE_SUFFIXES[image_type]}"
            temporary = destination.with_suffix(".part")
            try:
                temporary.write_bytes(data)
                temporary.replace(destination)
            finally:
                temporary.unlink(missing_ok=True)
            for previous in self._image_files():
                if previous.stem == digest and previous != destination:
                    previous.unlink(missing_ok=True)
            image = ImageArtifact(destination, image_type, width, height, len(data), url)
            self.diagnostics.record("image_validated", mode=mode, stage="validate", content_type=content_type,
                                    **image.metadata)
            self._prune_images()
            return image

    async def _send_image(self, event, image):
        mode = self.settings.image_send_mode
        if mode == "base64":
            await event.send(event.image_result(str(image.path)))
            return
        if mode not in {"file", "url"}:
            raise ImageTransportError("send", "unsupported_mode")
        if (event.get_platform_name() != "aiocqhttp"
                or not callable(getattr(getattr(event, "bot", None), "call_action", None))):
            raise ImageTransportError("send", "onebot_required")
        if mode == "url" and not is_image_url(image.url):
            raise ImageTransportError("send", "url_unavailable")
        file = image.path.resolve().as_uri() if mode == "file" else image.url
        group = event.get_group_id()
        target = group or event.get_sender_id()
        if not str(target).isascii() or not str(target).isdecimal() or int(target) <= 0:
            raise ImageTransportError("send", "invalid_target")
        payload = {"group_id" if group else "user_id": int(target), "self_id": event.get_self_id(),
                   "message": [{"type": "image", "data": {"file": file}}]}
        result = await event.bot.call_action("send_group_msg" if group else "send_private_msg", **payload)
        # aiocqhttp normally raises ActionFailed itself; also handle adapters
        # that return an unwrapped OneBot failure envelope.
        if isinstance(result, dict) and (result.get("retcode", 0) != 0 or result.get("status") == "failed"):
            error = ImageTransportError("send", "onebot_rejected")
            error.result = result
            raise error

    async def _status(self):
        status = self.client.status()
        usage = await self.storage.quota_usage()
        labels = {"authentication": "鉴权失败", "rate_limited": "限流", "timeout": "超时",
                  "schema": "响应结构变化", "identity_mismatch": "身份校验失败",
                  "no_data": "暂无收录", "upstream": "上游暂不可用", "queue_full": "队列已满",
                  "not_configured": "接入未配置", "closed": "客户端已关闭", "daily_quota": "每日额度已用完"}
        return (f"断箭战报 v0.1.4\n在线查询：{'已启用' if status['configured'] else '未启用或 API 地址无效'}\n"
                f"今日总请求：{usage['global']}/{GLOBAL_DAILY_LIMIT}（固定上限）；北京时间 0 点重置\n"
                f"本次运行请求：{status['requests']}；进行中：{status['inflight']}\n"
                f"API 缓存：{await self.storage.cache_count()} 条；命中：{status['cache_hits']}；旧缓存：{status['stale_hits']}\n"
                f"图片缓存：{len(self._image_files())} 张\n"
                f"最近错误：{labels.get(status['last_error'], '无' if not status['last_error'] else '请求失败')}\n"
                f"诊断模式：{'已开启' if self.settings.diagnostics_enabled else '已关闭'}")

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

    def _response_result(self, event, response, config):
        parts = [Plain(response.text)] if response.text else []
        private_prefix = ""
        if not event.get_group_id() and config.get("platform_settings", {}).get("friend_message_needs_wake_prefix", False):
            prefixes = config.get("wake_prefix", [])
            private_prefix = str(prefixes[0]) if prefixes else ""
        for label, command in response.instructions:
            parts.append(Plain("\n" + label))
            if event.get_group_id():
                # QQ resolves this real mention to the bot's current group card/name.
                parts.extend([At(qq=event.get_self_id()), Plain(" " + command)])
            else:
                parts.append(Plain((private_prefix + " " if private_prefix else "") + command))
        # Instructions must remain copyable, and At components must stay interactive.
        return event.chain_result(parts).use_t2i(False)

    def _log_image_failure(self, exc, mode, stage, image=None):
        details = image_error_metadata(exc, mode, stage)
        metadata = image.metadata if image is not None else {}
        details.update(metadata)
        self.logger.error(
            "断箭战报图片失败 mode=%s stage=%s error=%s retcode=%s reason=%s message=%s http_status=%s type=%s bytes=%s",
            details.get("mode", "unknown"), details.get("stage", stage), details.get("error", "unknown"),
            details.get("retcode", "unknown"), details.get("reason", "unknown"), details.get("message", "details_omitted"),
            details.get("status_code", "unknown"), details.get("image_type", "unknown"), details.get("image_bytes", "unknown"))
        if stage == "send":
            self.diagnostics.record("image_send_error", **details)
        elif stage == "cache":
            self.diagnostics.record("image_cache_invalid", **details)
        else:
            self.diagnostics.record("image_render_error", **details)

    @filter.command("ba", alias={"BA", "Ba", "bA"})
    async def ba(self, event: AstrMessageEvent):
        if self._closed:
            return
        event.stop_event()
        config = self.context.get_config(event.unified_msg_origin) or {}
        prefixes = config.get("wake_prefix", ["/"])
        started = time.monotonic()
        try:
            text, mentions = extract_arguments(event, prefixes)
            request = parse_command(text, mentions, self.settings.default_limit,
                                    self.settings.max_limit, self.settings.chinese_aliases)
            self.diagnostics.record("command_start", action=request.action)
            caller = Caller(str(event.get_platform_id()), event.unified_msg_origin,
                            str(event.get_sender_id()))
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
                        for path in self._image_files():
                            path.unlink(missing_ok=True)
                    yield event.plain_result("已清理 API 缓存与图片缓存；账号绑定及今日已用额度不受影响。")
                return
            with self.client.request_scope(quota_actor(event)):
                response = await self._run(self.service.execute(request, caller))
            self.diagnostics.record("query_complete", action=request.action,
                                    kind=response.view.get("kind") if response.view else "text",
                                    duration_ms=round((time.monotonic() - started) * 1000))
            if response.text:
                yield self._response_result(event, response, config)
            if response.view is not None:
                send_images = self.settings.image_enabled
                for view in self.renderer.views(response.view):
                    send_uncertain = False
                    if send_images:
                        stage_started = time.monotonic()
                        try:
                            image = await self._run(self._image(view, event.unified_msg_origin), timed=False)
                        except Exception as exc:
                            self._log_image_failure(exc, self.settings.image_send_mode, "generate")
                        else:
                            self.diagnostics.record("image_render_complete", kind=view.get("kind"), mode=self.settings.image_send_mode,
                                                    **image.metadata,
                                                    duration_ms=round((time.monotonic() - stage_started) * 1000))
                            stage_started = time.monotonic()
                            self.diagnostics.record("image_send_start", mode=self.settings.image_send_mode, stage="send", **image.metadata)
                            try:
                                # Await the transport here: errors from yielded results
                                # are otherwise swallowed later by AstrBot's pipeline.
                                await self._run(self._send_image(event, image))
                            except Exception as exc:
                                self._log_image_failure(exc, self.settings.image_send_mode, "send", image)
                                send_uncertain = True
                            else:
                                self.diagnostics.record("image_send_complete", mode=self.settings.image_send_mode, stage="send", **image.metadata,
                                                        duration_ms=round((time.monotonic() - stage_started) * 1000))
                                continue
                        send_images = False
                    text = self.renderer.text(view)
                    self.diagnostics.record("text_report", kind=view.get("kind"), page=view.get("page", 1))
                    if send_uncertain:
                        text = "图片发送未获成功确认，以下为文字战报：\n" + text
                    # Prevent the host's global T2I setting from turning fallback
                    # text into another image sent through the same failing path.
                    yield event.plain_result(text).use_t2i(False)
        except (ClientError, ValueError) as exc:
            self.diagnostics.record("command_error", error=type(exc).__name__, status=getattr(exc, "code", "invalid_input"))
            yield event.plain_result(str(exc))
        except asyncio.TimeoutError:
            self.diagnostics.record("command_error", error="timeout")
            yield event.plain_result("查询等待超时，请稍后重试。")
        except Exception as exc:
            self.diagnostics.record("command_error", error=type(exc).__name__)
            # Neither response bodies nor credential-bearing URLs enter logs.
            self.logger.error("断箭战报请求失败（%s）。", type(exc).__name__)
            yield event.plain_result("本次查询未完成，请稍后重试；如持续出现，请管理员使用 ba status 检查插件状态。")

    async def terminate(self):
        self._closed = True
        # Remove only this instance's handlers. A newer instance may already
        # have replaced the same routes during plugin reload.
        apis = self.context.registered_web_apis
        apis[:] = [api for api in apis if not (
            api[0] in self._web_routes
            and (api[1], api[2]) == self._web_routes[api[0]]
        )]
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self.service.close()
        await self.client.close()
        await self.storage.close()
        self.diagnostics.record("plugin_stop")
        self.diagnostics.close()
