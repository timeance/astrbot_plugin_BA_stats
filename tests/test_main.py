"""AstrBot adapter tests with test-local, fully restored module stubs.

These exercise registered handler behavior, not a live QQ/AstrBot deployment.
Plain/At and event methods follow the adapter's actual framework interfaces.
"""

import asyncio
from contextlib import nullcontext
from dataclasses import dataclass
from html.parser import HTMLParser
import importlib
import inspect
from io import BytesIO
import json
import logging
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from datetime import datetime, timezone
from unittest.mock import AsyncMock
from zipfile import ZipFile

import pytest
import httpx
from PIL import Image as PILImage


ROOT = Path(__file__).resolve().parents[1]


@dataclass
class Plain:
    text: str


@dataclass
class At:
    qq: str | int


class Result(tuple):
    def __new__(cls, kind, value, chain=()):
        result = super().__new__(cls, (kind, value))
        result.chain = list(chain)
        result.use_t2i_ = None
        return result

    def use_t2i(self, value):
        self.use_t2i_ = value
        return self


class Event:
    def __init__(self, messages, *, admin=False, normalized=None, session="onebot:test:group:1",
                 user="10001", group="1", platform="onebot:test"):
        self.message_obj = SimpleNamespace(message=messages, self_id="999", sender=SimpleNamespace(user_id="10001"))
        self.unified_msg_origin = session
        self.admin = admin
        self.stopped = False
        self.normalized = normalized
        self.message_obj.sender.user_id = user
        self.group, self.platform = group, platform
        self.sent = []

    def get_messages(self):
        return self.message_obj.message

    def get_message_str(self):
        if self.normalized is not None:
            return self.normalized
        text = "".join(part.text if isinstance(part, Plain) else f"@{part.qq}" for part in self.get_messages())
        return " ".join(text.split())

    def get_self_id(self):
        return self.message_obj.self_id

    def get_sender_id(self):
        return self.message_obj.sender.user_id

    def get_platform_id(self):
        return self.platform

    def get_platform_name(self):
        return "aiocqhttp"

    def get_group_id(self):
        return self.group

    def is_admin(self):
        return self.admin

    def stop_event(self):
        self.stopped = True

    def plain_result(self, text):
        return Result("plain", text, [Plain(text)])

    def image_result(self, path):
        return Result("image", path)

    def chain_result(self, chain):
        text = "".join(p.text if isinstance(p, Plain) else f"@{p.qq}" for p in chain)
        return Result("plain", text, chain)

    async def send(self, result):
        self.sent.append(result)


@pytest.fixture
def app_modules(monkeypatch, tmp_path):
    registered = []

    class Filters:
        def command(self, command, **options):
            def decorate(function):
                registered.append((command, options, function))
                return function
            return decorate

        def __getattr__(self, name):
            raise AssertionError(f"Unexpected message-wide or extra filter registration: {name}")

    class Star:
        def __init__(self, context):
            self.context = context
            self.logger = logging.getLogger("ba-stats-test")

        async def html_render(self, *args, **kwargs):
            raise AssertionError("A real renderer must never be contacted by these tests")

    class StarTools:
        @staticmethod
        def get_data_dir(plugin_name):
            assert plugin_name == "astrbot_plugin_ba_stats"
            return tmp_path / "plugin-data"

    class Context:
        def __init__(self, prefixes=None):
            self.prefixes = prefixes or ["/"]
            self.registered_web_apis = []

        def get_config(self, umo):
            return {"wake_prefix": self.prefixes}

        def register_web_api(self, route, view_handler, methods, desc):
            # AstrBot v4.29.4 core/star/context.py:774: same route/method
            # replaces its handler; registered_web_apis is the live list.
            for index, api in enumerate(self.registered_web_apis):
                if api[0] == route and api[2] == methods:
                    self.registered_web_apis[index] = (route, view_handler, methods, desc)
                    return
            self.registered_web_apis.append((route, view_handler, methods, desc))

    # Signatures/envelopes follow v4.29.4 astrbot/api/web.py:343-439.
    # The test environment has no AstrBot/FastAPI/Starlette installation.
    def json_response(data=None, *, status_code=200, headers=None):
        return SimpleNamespace(data={} if data is None else data, status_code=status_code,
                               headers=headers or {}, media_type="application/json")

    def error_response(message, *, status_code=400, data=None, headers=None):
        return json_response({"status": "error", "message": message, "data": data},
                             status_code=status_code, headers=headers)

    def stream_response(content, *, content_type="text/event-stream", status_code=200, headers=None):
        return SimpleNamespace(content=content, media_type=content_type,
                               status_code=status_code, headers=headers or {})

    stubs = {key: ModuleType(key) for key in (
        "astrbot", "astrbot.api", "astrbot.api.event", "astrbot.api.message_components", "astrbot.api.star",
        "astrbot.api.web",
    )}
    stubs["astrbot"].__path__ = []
    stubs["astrbot.api"].__path__ = []
    stubs["astrbot"].api = stubs["astrbot.api"]
    stubs["astrbot.api"].AstrBotConfig = dict
    stubs["astrbot.api.event"].AstrMessageEvent = Event
    stubs["astrbot.api.event"].filter = Filters()
    stubs["astrbot.api.message_components"].Plain = Plain
    stubs["astrbot.api.message_components"].At = At
    stubs["astrbot.api.star"].Context = Context
    stubs["astrbot.api.star"].Star = Star
    stubs["astrbot.api.star"].StarTools = StarTools
    stubs["astrbot.api.web"].request = SimpleNamespace(username="synthetic-admin", method="GET", json=AsyncMock(return_value=None))
    stubs["astrbot.api.web"].json_response = json_response
    stubs["astrbot.api.web"].error_response = error_response
    stubs["astrbot.api.web"].stream_response = stream_response
    for key, module in stubs.items():
        monkeypatch.setitem(sys.modules, key, module)
    name = "_ba_stats_main_contract"
    package = ModuleType(name)
    package.__path__ = [str(ROOT)]
    monkeypatch.setitem(sys.modules, name, package)
    try:
        main = importlib.import_module(name + ".main")
        yield SimpleNamespace(
            main=main, registered=registered,
            models=importlib.import_module(name + ".models"),
            storage=importlib.import_module(name + ".storage"),
            web=stubs["astrbot.api.web"], context_class=Context,
            package=name,
        )
    finally:
        for key in tuple(sys.modules):
            if key.startswith(name + "."):
                sys.modules.pop(key, None)


@pytest.fixture
async def app(app_modules):
    plugins = []

    async def make(*, images=False, config=None, prefixes=None, context=None):
        values = {"query": {"cooldown_seconds": 0}, "output": {"image_enabled": images}}
        if config:
            values.update(config)
        context = context or app_modules.context_class(prefixes)
        class Config(dict):
            schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))

            def save_config(self, replace_config=None):
                if replace_config:
                    self.update(replace_config)
                self.config_path.write_text(json.dumps(self), encoding="utf-8")

        stored = Config(values)
        stored.config_path = app_modules.main.StarTools.get_data_dir("astrbot_plugin_ba_stats").parent / f"config-{len(plugins)}.json"
        stored.save_config()
        plugin = app_modules.main.BAStatsPlugin(context, stored)
        plugins.append(plugin)
        await plugin.initialize()
        return plugin

    try:
        yield SimpleNamespace(make=make, **vars(app_modules))
    finally:
        for plugin in plugins:
            await plugin.terminate()


async def collect(plugin, event):
    return [item async for item in plugin.ba(event)]


def image_bytes(image_type="JPEG"):
    output = BytesIO()
    PILImage.new("RGB", (12, 8), "#285080").save(output, format=image_type)
    return output.getvalue()


def artifact(app, path, *, url=None):
    return app.main.ImageArtifact(Path(path), "JPEG", 12, 8, len(image_bytes()), url)


@pytest.mark.parametrize("mode", ["base64", "file", "url"])
async def test_twenty_byte_render_body_never_enters_cache_or_send(app, monkeypatch, mode, caplog):
    plugin = await app.make(config={"output": {"image_enabled": True, "image_send_mode": mode}})
    monkeypatch.setattr(plugin.renderer, "template", lambda view: "synthetic html")
    monkeypatch.setattr(plugin.service, "execute", AsyncMock(return_value=SimpleNamespace(text="", view={"kind": "overview"}, instructions=())))
    render = AsyncMock(return_value="https://render.invalid/result?signature=NEVER-LOG")
    download = AsyncMock(return_value=(b"Internal Server Err ", "image/jpeg"))
    assert len(download.return_value[0]) == 20
    monkeypatch.setattr(plugin, "html_render", render)
    monkeypatch.setattr(app.main, "download_image", download)
    event = Event([Plain("/ba stats stb:001")])
    event.bot = SimpleNamespace(call_action=AsyncMock())
    replies = await collect(plugin, event)
    assert len(replies) == 1 and replies[0].use_t2i_ is False
    assert not event.sent
    event.bot.call_action.assert_not_awaited()
    render.assert_awaited_once()
    download.assert_awaited_once()
    assert list(plugin._images.iterdir()) == []
    assert f"mode={mode}" in caplog.text and "stage=validate" in caplog.text
    assert "bytes=20" in caplog.text and "invalid_image" in caplog.text
    assert "NEVER-LOG" not in caplog.text and "render.invalid" not in caplog.text
    assert caplog.records[-1].levelno == logging.ERROR
    assert not plugin.diagnostics.enabled


async def test_bad_cache_deleted_and_regenerated_once_without_touching_other_data(app, monkeypatch, tmp_path):
    plugin = await app.make(images=True)
    monkeypatch.setattr(plugin.renderer, "template", lambda view: "cache regression")
    digest = app.main.hashlib.sha256(b"sessioncache regression").hexdigest()
    bad = plugin._images / f"{digest}.jpg"
    bad.write_bytes(b"Internal Server Err ")
    source = tmp_path / "real.png"
    source.write_bytes(image_bytes("PNG"))
    render = AsyncMock(return_value=str(source))
    monkeypatch.setattr(plugin, "html_render", render)
    marker = plugin.data_dir / "keep.json"
    marker.write_text("keep")
    await plugin.storage.put_cache("keep-api", {"ok": True}, 60)
    first = await plugin._image({}, "session")
    assert not bad.exists() and first.path.suffix == ".png"
    assert first.path.read_bytes() == source.read_bytes()
    assert (first.width, first.height, first.image_type) == (12, 8, "PNG")
    assert await plugin._image({}, "session") == first
    render.assert_awaited_once()
    assert marker.read_text() == "keep" and await plugin.storage.cache_count() == 1


async def test_valid_old_mislabeled_cache_uses_true_extension_without_rerender(app, monkeypatch):
    plugin = await app.make(images=True)
    monkeypatch.setattr(plugin.renderer, "template", lambda view: "old image")
    digest = app.main.hashlib.sha256(b"sessionold image").hexdigest()
    old = plugin._images / f"{digest}.jpg"
    old.write_bytes(image_bytes("PNG"))
    image = await plugin._image({}, "session")
    assert image.path.suffix == ".png" and not old.exists()
    assert image.path.read_bytes() == image_bytes("PNG")


async def test_bad_cache_and_bad_regeneration_leave_no_reusable_artifact(app, monkeypatch, tmp_path):
    plugin = await app.make(images=True)
    monkeypatch.setattr(plugin.renderer, "template", lambda view: "bad cache")
    digest = app.main.hashlib.sha256(b"sessionbad cache").hexdigest()
    (plugin._images / f"{digest}.jpg").write_bytes(b"Internal Server Err ")
    source = tmp_path / "bad.jpg"
    source.write_bytes(b"Internal Server Err ")
    render = AsyncMock(return_value=str(source))
    monkeypatch.setattr(plugin, "html_render", render)
    with pytest.raises(app.main.ImageTransportError, match="invalid_image"):
        await plugin._image({}, "session")
    assert not plugin._image_files()
    render.assert_awaited_once()


async def test_url_mode_regenerates_url_each_time_but_shares_validated_disk_cache(app, monkeypatch):
    plugin = await app.make(config={"output": {"image_enabled": True, "image_send_mode": "url"}})
    monkeypatch.setattr(plugin.renderer, "template", lambda view: "url cache")
    urls = ["https://render.invalid/a?signature=one", "https://render.invalid/b?signature=two"]
    render = AsyncMock(side_effect=urls)
    download = AsyncMock(return_value=(image_bytes("PNG"), "image/png"))
    monkeypatch.setattr(plugin, "html_render", render)
    monkeypatch.setattr(app.main, "download_image", download)
    first = await plugin._image({}, "session")
    second = await plugin._image({}, "session")
    assert [first.url, second.url] == urls and first.path == second.path
    assert render.await_count == download.await_count == 2
    assert all(call.kwargs["return_url"] is True for call in render.await_args_list)
    assert len(plugin._image_files()) == 1
    assert "signature" not in repr(second)
    plugin.settings = app.main.replace(plugin.settings, image_send_mode="base64")
    cached = await plugin._image({}, "session")
    assert cached.path == second.path and cached.url is None
    assert render.await_count == 2


async def test_url_mode_with_local_path_reports_error_and_never_sends(app, monkeypatch, tmp_path, caplog):
    plugin = await app.make(config={"output": {"image_enabled": True, "image_send_mode": "url"}})
    source = tmp_path / "local.jpg"
    source.write_bytes(image_bytes())
    render = AsyncMock(return_value=str(source))
    monkeypatch.setattr(plugin, "html_render", render)
    monkeypatch.setattr(plugin.service, "execute", AsyncMock(return_value=SimpleNamespace(text="", view={"kind": "overview"}, instructions=())))
    event = Event([Plain("/ba stats stb:001")])
    event.bot = SimpleNamespace(call_action=AsyncMock())
    replies = await collect(plugin, event)
    assert len(replies) == 1 and not event.sent
    event.bot.call_action.assert_not_awaited()
    render.assert_awaited_once()
    assert "mode=url" in caplog.text and "url_unavailable" in caplog.text
    assert caplog.records[-1].levelno == logging.ERROR


@pytest.mark.parametrize("mode", ["file", "url"])
@pytest.mark.parametrize("group", ["123", ""])
async def test_native_onebot_payload_and_no_standard_component_conversion(app, monkeypatch, tmp_path, mode, group):
    plugin = await app.make(config={"output": {"image_enabled": True, "image_send_mode": mode}})
    picture = tmp_path / "空 格 #.jpg"
    picture.write_bytes(image_bytes())
    url = "https://render.invalid/result?signature=do%2Fnot%2Bchange"
    image = artifact(app, picture, url=url)
    event = Event([], group=group, user="456")
    event.bot = SimpleNamespace(call_action=AsyncMock(return_value={"message_id": 1}))
    event.image_result = lambda path: pytest.fail("native modes must not use a standard Image component")
    event.send = AsyncMock(side_effect=AssertionError("native modes must not call event.send"))
    await plugin._send_image(event, image)
    event.bot.call_action.assert_awaited_once_with(
        "send_group_msg" if group else "send_private_msg",
        **{"group_id" if group else "user_id": int(group or "456"), "self_id": "999",
           "message": [{"type": "image", "data": {"file": picture.resolve().as_uri() if mode == "file" else url}}]})
    event.send.assert_not_awaited()


@pytest.mark.parametrize("mode", ["base64", "file", "url"])
async def test_failed_send_uses_error_and_text_without_retrying_or_switching_mode(app, monkeypatch, tmp_path, caplog, mode):
    plugin = await app.make(config={"output": {"image_enabled": True, "image_send_mode": mode}})
    picture = tmp_path / "report.jpg"
    picture.write_bytes(image_bytes())
    render = AsyncMock(return_value=artifact(app, picture, url="https://render.invalid/image?secret=123"))
    monkeypatch.setattr(plugin, "_image", render)
    monkeypatch.setattr(plugin.service, "execute", AsyncMock(return_value=SimpleNamespace(text="", view={"kind": "overview"}, instructions=())))
    error = RuntimeError("base64://raw-body https://secret.invalid/?token=leak private-chat")
    error.result = {"retcode": 1200, "message": "Timeout: sendMsg base64://raw-body secret-token private-chat"}
    event = Event([Plain("/ba stats stb:001")])
    event.send = AsyncMock(side_effect=error)
    event.bot = SimpleNamespace(call_action=AsyncMock(side_effect=error))
    replies = await collect(plugin, event)
    assert len(replies) == 1 and replies[0].use_t2i_ is False
    assert event.send.await_count == (mode == "base64")
    assert event.bot.call_action.await_count == (mode != "base64")
    render.assert_awaited_once()
    assert f"mode={mode}" in caplog.text and "stage=send" in caplog.text and "retcode=1200" in caplog.text
    assert caplog.records[-1].levelno == logging.ERROR
    assert not any(secret in caplog.text for secret in ("raw-body", "secret.invalid", "secret-token", "private-chat", "?secret="))


@pytest.mark.parametrize("mode", ["file", "url"])
async def test_native_modes_reject_non_onebot_platform_with_text_fallback(app, monkeypatch, caplog, mode):
    plugin = await app.make(config={"output": {"image_enabled": True, "image_send_mode": mode}})
    monkeypatch.setattr(plugin, "_image", AsyncMock(return_value=artifact(app, "report.jpg", url="https://render.invalid/image")))
    monkeypatch.setattr(plugin.service, "execute", AsyncMock(return_value=SimpleNamespace(text="", view={"kind": "overview"}, instructions=())))
    event = Event([Plain("/ba stats stb:001")])
    event.get_platform_name = lambda: "discord"
    event.bot = SimpleNamespace(call_action=AsyncMock())
    replies = await collect(plugin, event)
    assert len(replies) == 1 and not event.sent
    event.bot.call_action.assert_not_awaited()
    assert "onebot_required" in caplog.text and caplog.records[-1].levelno == logging.ERROR


@pytest.mark.parametrize("mode", ["base64", "file", "url"])
@pytest.mark.parametrize("stage", ["generate", "download"])
async def test_generation_and_download_failures_log_correct_stage_without_secrets(app, monkeypatch, caplog, mode, stage):
    plugin = await app.make(config={"output": {"image_enabled": True, "image_send_mode": mode}})
    monkeypatch.setattr(plugin.service, "execute", AsyncMock(return_value=SimpleNamespace(text="", view={"kind": "overview"}, instructions=())))
    error = RuntimeError("https://render.invalid/?signature=private-token base64://private-data private-chat")
    render = AsyncMock(side_effect=error) if stage == "generate" else AsyncMock(return_value="https://render.invalid/?signature=private-token")
    monkeypatch.setattr(plugin, "html_render", render)
    download = AsyncMock(side_effect=error)
    monkeypatch.setattr(app.main, "download_image", download)
    event = Event([Plain("/ba stats stb:001")])
    replies = await collect(plugin, event)
    assert len(replies) == 1 and not event.sent
    assert f"mode={mode}" in caplog.text and f"stage={stage}" in caplog.text
    assert "error=RuntimeError" in caplog.text and "retcode=unknown" in caplog.text
    assert caplog.records[-1].levelno == logging.ERROR
    assert not any(secret in caplog.text for secret in ("render.invalid", "private-token", "private-data", "private-chat"))
    assert download.await_count == (stage == "download")
    assert not plugin.diagnostics.enabled


@pytest.mark.parametrize("mode", ["file", "url"])
async def test_native_returned_failure_envelope_is_not_reported_as_success(app, monkeypatch, caplog, mode):
    plugin = await app.make(config={"output": {"image_enabled": True, "image_send_mode": mode}})
    monkeypatch.setattr(plugin, "_image", AsyncMock(return_value=artifact(app, "report.jpg", url="https://render.invalid/image")))
    monkeypatch.setattr(plugin.service, "execute", AsyncMock(return_value=SimpleNamespace(text="", view={"kind": "overview"}, instructions=())))
    event = Event([Plain("/ba stats stb:001")])
    event.bot = SimpleNamespace(call_action=AsyncMock(return_value={"status": "failed", "retcode": 1200,
                                                                  "message": "private-data Timeout: sendMsg"}))
    replies = await collect(plugin, event)
    assert len(replies) == 1 and not event.sent
    event.bot.call_action.assert_awaited_once()
    assert "retcode=1200" in caplog.text and "onebot_rejected" in caplog.text
    assert "private-data" not in caplog.text


def test_only_framework_ba_command_is_registered(app_modules):
    assert len(app_modules.registered) == 1
    command, options, function = app_modules.registered[0]
    assert command == "ba"
    assert options["alias"] == {"BA", "Ba", "bA"}
    assert function is app_modules.main.BAStatsPlugin.ba


@pytest.mark.parametrize("messages", [
    [Plain('/ba recent name:"a  b" --limit 10')],
    [Plain('/ba recent name:"a '), Plain(' b" --limit 10')],
    [At("999"), Plain(' /ba recent name:"a  b" --limit 10')],
    [Plain('!!BA recent name:"a  b" --limit 10')],
])
def test_real_style_plain_components_preserve_quotes_and_repeated_spaces(app_modules, messages):
    event = Event(messages, normalized='ba recent name:"a b" --limit 10')
    text, mentions = app_modules.main.extract_arguments(event, ["/", "!!"])
    assert 'name:"a  b"' in text
    request = app_modules.main.parse_command(text, mentions)
    assert request.target.kind == "name"
    assert request.target.value == "a  b"
    assert request.limit == 10


def test_at_component_is_used_as_account_id_and_never_group_card(app_modules):
    event = Event([At("999"), Plain(" /ba stats "), At(12345)])
    text, mentions = app_modules.main.extract_arguments(event, ["/"])
    assert mentions == ["12345"]
    request = app_modules.main.parse_command(text, mentions)
    assert (request.target.kind, request.target.value) == ("mention", "12345")


@pytest.mark.parametrize("messages", [
    [Plain("/ba stats @user:12345")],
    [Plain("/ba stats @user:12345 "), At("12345")],
    [Plain("/ba stats "), At("all")],
])
def test_typed_internal_mentions_and_at_all_are_rejected(app_modules, messages):
    with pytest.raises(ValueError, match="提及|全体"):
        text, mentions = app_modules.main.extract_arguments(Event(messages), ["/"])
        app_modules.main.parse_command(text, mentions)


def test_plain_text_cannot_forge_component_position(app_modules):
    with pytest.raises(ValueError, match="控制字符"):
        app_modules.main.extract_arguments(Event([
            Plain("/ba stats \x00at:0\x00 "), At("12345")
        ]), ["/"])


@pytest.mark.parametrize("command", [
    "stats name:@user:12345", "bind name:@user:12345",
    "recent name:@user:12345 --limit 2", "last name:@user:12345",
    "search @user:12345",
])
async def test_handler_preserves_marker_text_for_nickname_targets(app, monkeypatch, command):
    plugin = await app.make()
    execute = AsyncMock(return_value=SimpleNamespace(text="ok", view=None, instructions=()))
    monkeypatch.setattr(plugin.service, "execute", execute)
    assert await collect(plugin, Event([Plain("/ba " + command)])) == [("plain", "ok")]
    request, _ = execute.await_args.args
    assert (request.target.kind, request.target.value) == ("name", "@user:12345")


@pytest.mark.parametrize("command", ["stats", "bind", "recent", "last"])
def test_component_provenance_is_preserved_for_each_target_action(app_modules, command):
    text, mentions = app_modules.main.extract_arguments(
        Event([Plain(f"/ba {command} "), At("12345")]), ["/"])
    target = app_modules.main.parse_command(text, mentions).target
    assert (target.kind, target.value) == ("mention", "12345")


async def test_unconfigured_plugin_help_works_and_online_command_explains_missing_access(app):
    plugin = await app.make()
    help_event = Event([Plain("/ba help")])
    replies = await collect(plugin, help_event)
    assert help_event.stopped
    assert replies[0][0] == "plain"
    assert "首次使用" in replies[0][1]
    replies = await collect(plugin, Event([Plain("/ba search Synthetic")]))
    assert "接入未配置" in replies[0][1]
    assert plugin.client.status()["requests"] == 0


async def test_handler_passes_component_preserved_name_to_service(app, monkeypatch):
    plugin = await app.make(prefixes=["!!"])
    execute = AsyncMock(return_value=SimpleNamespace(text="ok", view=None, instructions=()))
    monkeypatch.setattr(plugin.service, "execute", execute)
    replies = await collect(plugin, Event(
        [Plain('!!ba stats name:"a '), Plain(' b"')], normalized='ba stats name:"a b"'
    ))
    assert replies == [("plain", "ok")]
    request, caller = execute.await_args.args
    assert request.target.value == "a  b"
    assert caller.user == "10001"


@pytest.mark.parametrize("command", ["status", "状态", "cache clear", "缓存 清理"])
async def test_admin_check_cannot_be_bypassed_by_english_or_chinese_commands(app, monkeypatch, command):
    plugin = await app.make()
    status = AsyncMock(wraps=plugin._status)
    clear = AsyncMock(wraps=plugin.client.clear_cache)
    execute = AsyncMock(wraps=plugin.service.execute)
    monkeypatch.setattr(plugin, "_status", status)
    monkeypatch.setattr(plugin.client, "clear_cache", clear)
    monkeypatch.setattr(plugin.service, "execute", execute)
    replies = await collect(plugin, Event([Plain("/ba " + command)], admin=False))
    assert "仅供 AstrBot 管理员" in replies[0][1]
    status.assert_not_awaited()
    clear.assert_not_awaited()
    execute.assert_not_awaited()


async def test_admin_cache_clear_removes_api_and_images_but_preserves_binding(app):
    plugin = await app.make()
    old = app.storage.Binding(plugin.client.namespace, "001", "Synthetic")
    await plugin.storage.set_binding("onebot:test", "10001", old)
    await plugin.storage.put_cache("synthetic", {}, 60)
    await plugin.storage.consume_request()
    (plugin._images / "synthetic.jpg").write_bytes(b"synthetic image cache")
    replies = await collect(plugin, Event([Plain("/ba cache clear")], admin=True))
    assert "已清理" in replies[0][1]
    assert await plugin.storage.cache_count() == 0
    assert not list(plugin._images.glob("*.jpg"))
    assert await plugin.storage.get_binding("onebot:test", "10001") == old
    assert (await plugin.storage.quota_usage())["global"] == 1


async def test_admin_status_does_not_expose_configured_credentials(app):
    secret = "synthetic-token-never-display"
    identity = "synthetic-private-client"
    plugin = await app.make(config={"access": {
        "enabled": True, "client_id": identity, "api_token": secret,
        "base_url": "https://example.invalid",
    }})
    replies = await collect(plugin, Event([Plain("/ba status")], admin=True))
    text = replies[0][1]
    assert "在线查询：已启用" in text
    assert secret not in text and identity not in text and "example.invalid" not in text
    assert plugin.client.status()["requests"] == 0
    assert "今日总请求：0/100（固定上限）" in text


async def test_image_failure_uses_same_result_as_text_without_second_fetch(app, monkeypatch):
    plugin = await app.make(images=True)
    m = app.models
    at = datetime(2026, 11, 17, tzinfo=timezone.utc)
    player = m.Player("001", "Synthetic a  b", source_url="https://example.invalid/players/001")
    client = SimpleNamespace(
        namespace="batrace:synthetic", player=AsyncMock(return_value=m.FetchResult(player, at)),
        recent=AsyncMock(return_value=m.FetchResult([], at)), close=AsyncMock(),
        request_scope=lambda actor: nullcontext(),
    )
    await plugin.client.close()
    plugin.client = plugin.service.client = client
    image = AsyncMock(side_effect=RuntimeError("synthetic render failure"))
    monkeypatch.setattr(plugin, "_image", image)
    original_execute = plugin.service.execute
    responses = []

    async def execute(*args):
        response = await original_execute(*args)
        responses.append(response)
        return response

    monkeypatch.setattr(plugin.service, "execute", execute)
    replies = await collect(plugin, Event([Plain("/ba stats stb:001")]))
    assert len(responses) == 1
    expected = plugin.renderer.text(plugin.renderer.views(responses[0].view)[0])
    assert replies == [("plain", expected)]
    assert replies[0].use_t2i_ is False
    assert "001" in expected and "Synthetic a  b" in expected
    image.assert_awaited_once()
    client.player.assert_awaited_once_with("001")
    client.recent.assert_awaited_once_with("001", plugin.settings.max_limit)


@pytest.mark.parametrize("wake", ["zar", "zariba", "/", "!!"])
async def test_help_uses_bare_ba_and_real_self_mention(app, wake):
    plugin = await app.make(prefixes=[wake])
    result, = await collect(plugin, Event([At("999"), Plain(" ba help")]))
    assert "• ba me：" in result[1]
    assert wake + "ba" not in result[1]
    assert "发送示例：@999 ba me" in result[1]
    assert [part.qq for part in result.chain if isinstance(part, At)] == ["999"]
    assert result.use_t2i_ is False


@pytest.mark.parametrize("choice", ["select", "cancel"])
async def test_binding_hints_round_trip_real_mentions_and_do_not_rewrite_names(app, monkeypatch, choice):
    plugin = await app.make(prefixes=["zar"])
    player = app.models.Player("152117", "zarba select stays literal", source_url="https://dash.batrace.top/player/152117")
    result = app.models.FetchResult(player, datetime.now(timezone.utc))
    monkeypatch.setattr(plugin.client, "player", AsyncMock(return_value=result))
    offered, = await collect(plugin, Event([At("999"), Plain(" ba bind stb:152117")]))
    assert player.name in offered[1]
    assert [p.qq for p in offered.chain if isinstance(p, At)] == ["999", "999"]
    commands = [p.text.strip() for p in offered.chain if isinstance(p, Plain) and p.text.startswith(" ba ")]
    select, cancel = commands
    assert select.startswith("ba select ") and cancel.startswith("ba cancel ")
    assert select.split()[2] == cancel.split()[2]
    confirmed, = await collect(plugin, Event([At("999"), Plain(" " + (select if choice == "select" else cancel))]))
    if choice == "select":
        assert "已绑定默认账号" in confirmed[1] and "@999 ba me" in confirmed[1]
        assert (await plugin.storage.get_binding("onebot:test", "10001")).player_id == "152117"
    else:
        assert "已取消" in confirmed[1]
        assert await plugin.storage.get_binding("onebot:test", "10001") is None


async def test_unbound_self_uses_same_structured_binding_hint(app):
    plugin = await app.make(prefixes=["zar"])
    result, = await collect(plugin, Event([At("999"), Plain(" ba me")]))
    assert "@999 ba bind name:昵称" in result[1]
    assert [p.qq for p in result.chain if isinstance(p, At)] == ["999"]


@pytest.mark.parametrize("needs_prefix", [False, True])
async def test_private_hint_uses_host_wake_requirement_without_at(app, needs_prefix):
    plugin = await app.make()
    plugin.context.get_config = lambda umo: {"wake_prefix": ["zar"], "platform_settings": {
        "friend_message_needs_wake_prefix": needs_prefix}}
    result, = await collect(plugin, Event([Plain("zar ba help")], group=""))
    assert not any(isinstance(p, At) for p in result.chain)
    expected = "zar ba me" if needs_prefix else "ba me"
    assert result[1].endswith("发送示例：" + expected)
    args, _ = app.main.extract_arguments(Event([Plain(expected)], group=""), ["zar"])
    assert app.main.parse_command(args).action == "stats"


@pytest.mark.parametrize("failed_page", [None, 0, 1])
async def test_report_delivery_failure_switches_remaining_pages_to_plain_text(app, monkeypatch, failed_page):
    plugin = await app.make(images=True)
    execute = AsyncMock(return_value=SimpleNamespace(text="", view={}, instructions=()))
    monkeypatch.setattr(plugin.service, "execute", execute)
    monkeypatch.setattr(plugin.renderer, "views", lambda view: [{"kind": "recent", "page": i} for i in range(3)])
    monkeypatch.setattr(plugin.renderer, "text", lambda view: f"same data page {view['page']}")
    render = AsyncMock(side_effect=lambda view, umo: artifact(app, f"page-{view['page']}.jpg"))
    monkeypatch.setattr(plugin, "_image", render)
    event = Event([Plain("/ba stats stb:152117")])
    attempted = []
    async def send(result):
        attempted.append(result)
        if failed_page is not None and len(attempted) - 1 == failed_page:
            raise RuntimeError("ActionFailed retcode=1200 synthetic transport timeout")
    event.send = send
    replies = await collect(plugin, event)
    execute.assert_awaited_once()
    if failed_page is None:
        assert replies == [] and len(attempted) == render.await_count == 3
    else:
        assert len(attempted) == render.await_count == failed_page + 1
        assert len(replies) == 3 - failed_page
        assert "图片发送未获成功确认" in replies[0][1]
        for index, reply in enumerate(replies, failed_page):
            assert reply[1].endswith(f"same data page {index}")
            assert reply.use_t2i_ is False


async def test_cancelled_image_send_is_not_converted_to_fallback(app, monkeypatch):
    plugin = await app.make(images=True)
    monkeypatch.setattr(plugin.service, "execute", AsyncMock(return_value=SimpleNamespace(text="", view={}, instructions=())))
    monkeypatch.setattr(plugin.renderer, "views", lambda view: [view])
    monkeypatch.setattr(plugin, "_image", AsyncMock(return_value=artifact(app, "synthetic.jpg")))
    event = Event([Plain("/ba stats stb:001")])
    event.send = AsyncMock(side_effect=asyncio.CancelledError)
    with pytest.raises(asyncio.CancelledError):
        await collect(plugin, event)
    event.send.assert_awaited_once()


@pytest.mark.parametrize(("kind", "width"), [("overview", 640), ("recent", 800), ("match", 900), (None, 640)])
async def test_image_render_uses_card_width_and_fixed_outer_template(app, monkeypatch, tmp_path, kind, width):
    plugin = await app.make(images=True)
    source = tmp_path / "synthetic-render.jpg"
    source.write_bytes(image_bytes())
    render = AsyncMock(return_value=str(source))
    monkeypatch.setattr(plugin, "html_render", render)
    view = {
        "player": {"id": "001", "name": "Synthetic {{ 7 * 7 }} a  b"},
        "scope": "合成测试样本", "fetched_at": datetime(2026, 11, 17, tzinfo=timezone.utc),
    }
    if kind is not None:
        view["kind"] = kind
    if kind == "match":
        view["teams"] = [{"name": "A 队", "players": [view["player"]]}]
    first = await plugin._image(view, "onebot:test:group:one")
    assert first.path.suffix == ".jpg"
    assert first.path.read_bytes() == source.read_bytes()
    assert await plugin._image(view, "onebot:test:group:one") == first
    render.assert_awaited_once()
    positional, options = render.await_args
    assert positional[0] == "{{ body | safe }}"
    assert "{{ 7 * 7 }} a  b" in positional[1]["body"]
    assert options["return_url"] is True
    assert options["umo"] == "onebot:test:group:one"
    assert options["options"] == {
        "type": "jpeg", "quality": 85, "full_page": True,
        "viewport_width": width, "device_scale_factor_level": "ultra",
    }
    second = await plugin._image(view, "onebot:test:group:two")
    assert second != first
    assert render.await_count == 2


async def test_run_after_terminate_closes_coroutine_and_creates_no_task(app):
    plugin = await app.make()
    await plugin.terminate()
    entered = False

    async def should_not_run():
        nonlocal entered
        entered = True

    coroutine = should_not_run()
    with pytest.raises(asyncio.CancelledError):
        await plugin._run(coroutine)
    assert inspect.getcoroutinestate(coroutine) == inspect.CORO_CLOSED
    assert not entered
    assert not plugin._tasks


async def test_terminate_cancels_and_awaits_active_work_then_closes_resources(app, monkeypatch):
    plugin = await app.make()
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def blocked(*args):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(plugin.service, "execute", blocked)
    close_client = AsyncMock(wraps=plugin.client.close)
    close_storage = AsyncMock(wraps=plugin.storage.close)
    monkeypatch.setattr(plugin.client, "close", close_client)
    monkeypatch.setattr(plugin.storage, "close", close_storage)
    scope = ("p", "s", "u")
    generation = plugin.service.pending.begin(scope)
    plugin.service.pending.publish(scope, generation, object(), [object()])
    task = asyncio.create_task(collect(plugin, Event([Plain("/ba search Synthetic")])))
    try:
        await asyncio.wait_for(started.wait(), 2)
        await plugin.terminate()
        results = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(results[0], asyncio.CancelledError)
        assert cancelled.is_set()
        assert not plugin._tasks
        assert not plugin.service.pending.is_current(scope, generation)
        close_client.assert_awaited_once()
        close_storage.assert_awaited_once()
        with pytest.raises(RuntimeError, match="尚未打开"):
            await plugin.storage.get_binding("p", "u")
        assert await collect(plugin, Event([Plain("/ba help")])) == []
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("command,group", [("quota", "1"), ("额度", "")])
async def test_quota_is_local_and_available_without_access_or_admin(app, command, group):
    plugin = await app.make()
    replies = await collect(plugin, Event([Plain("/ba " + command)], group=group))
    text = replies[0][1]
    assert "个人：已用 0/20 次" in text
    assert "全插件：已用 0/100 次" in text
    assert ("本群：已用 0/100 次" in text) == bool(group)
    assert "北京时间每天 0 点重置" in text
    assert plugin.client.status()["requests"] == 0


async def test_handler_enforces_user_group_quota_and_preserves_cached_access(app):
    plugin = await app.make(config={
        "access": {"enabled": True, "base_url": "https://synthetic.invalid"},
        "query": {"cooldown_seconds": 0},
        "maintenance": {"request_interval": 0.1, "retries": 0},
        "limits": {"user_daily_limit": 2, "group_daily_limit": 3},
    })
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"players": [{"id": "001", "name": "Synthetic"}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        plugin.client._http, plugin.client._owns_http = http, False
        async def search(query, user, group, admin=False):
            return await collect(plugin, Event([Plain("/ba search " + query)], user=user,
                                               group=group, admin=admin))
        await search("one", "u1", "g1")
        await search("two", "u1", "g2")
        blocked = await search("private", "u1", "")
        assert "20" not in blocked[0][1]  # Runtime override, not a hardcoded default.
        assert "额度" in blocked[0][1] and "2" in blocked[0][1]
        await search("three", "u2", "g1")
        await search("four", "u3", "g1")
        assert "额度" in (await search("five", "u4", "g1"))[0][1]
        assert "额度" in (await search("six", "u1", "g2", admin=True))[0][1]
        assert len(calls) == 4
        # A fresh cached response consumes no upstream request, even at the cap.
        cached = await search("one", "u1", "g1")
        assert "请选择账号" in cached[0][1]
        assert len(calls) == 4
        actor = app.main.QuotaActor("onebot:test", "u1", "g1")
        usage = await plugin.storage.quota_usage(actor)
        assert (usage["global"], usage["user"], usage["group"]) == (4, 2, 3)
        before = dict(usage)
        for text in ["help", "binding", "unbind", "quota"]:
            await collect(plugin, Event([Plain("/ba " + text)], user="u1", group="g1"))
        assert await plugin.storage.quota_usage(actor) == before


async def test_mentions_charge_request_sender_not_queried_user(app):
    plugin = await app.make(config={
        "access": {"enabled": True, "base_url": "https://synthetic.invalid"},
        "query": {"cooldown_seconds": 0}, "maintenance": {"request_interval": 0.1, "retries": 0},
    })
    await plugin.storage.set_binding("onebot:test", "target", app.storage.Binding(
        plugin.client.namespace, "001", "Synthetic"))
    def handler(request):
        if request.url.path.endswith("search"):
            payload = {"players": [{"id": "001", "name": "Synthetic"}]}
        elif request.url.path.endswith("player"):
            payload = {"trend": {"points": []}}
        else:
            payload = {"matches": []}
        return httpx.Response(200, json=payload)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        plugin.client._http, plugin.client._owns_http = http, False
        replies = await collect(plugin, Event([Plain("/ba stats "), At("target")], user="sender", group="g"))
        assert "Synthetic" in replies[0][1]
    sender = await plugin.storage.quota_usage(app.main.QuotaActor("onebot:test", "sender", "g"))
    target = await plugin.storage.quota_usage(app.main.QuotaActor("onebot:test", "target", "g"))
    assert (sender["global"], sender["user"], sender["group"], target["user"]) == (3, 3, 3, 0)


def test_quota_defaults_schema_and_fixed_total_are_consistent(app_modules):
    settings = app_modules.main.Settings
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    limits = schema["limits"]["items"]
    assert set(limits) == {"user_daily_limit", "group_daily_limit"}
    defaults = settings.from_config({})
    assert defaults.user_daily_limit == limits["user_daily_limit"]["default"] == 20
    assert defaults.group_daily_limit == limits["group_daily_limit"]["default"] == 100
    configured = settings.from_config({"limits": {
        "user_daily_limit": 0, "group_daily_limit": 1, "global_daily_limit": 10000, "timezone": "UTC",
    }, "output": {"timezone": "UTC"}})
    assert configured.user_daily_limit == 0 and configured.group_daily_limit == 1
    assert not hasattr(configured, "global_daily_limit")
    assert app_modules.main.GLOBAL_DAILY_LIMIT == 100
    for key in limits:
        for invalid in [-1, 101, True, 2.5, "20", None]:
            with pytest.raises(ValueError, match="0–100"):
                settings.from_config({"limits": {key: invalid}})


async def test_image_failure_logs_onebot_details_and_image_metadata(app, tmp_path, caplog):
    plugin = await app.make(config={"diagnostics": {"enabled": True}})
    picture = tmp_path / "report.jpg"
    picture.write_bytes(image_bytes())
    error = RuntimeError("not used")
    error.result = {"retcode": 1200, "message": "Timeout: sendMsg base64://private-image https://secret.invalid/?token=secret",
                    "data": "private-response", "echo": {"token": "private-echo"}}
    with caplog.at_level(logging.ERROR):
        plugin._log_image_failure(error, "base64", "send", artifact(app, picture))
    assert all(record.levelno == logging.ERROR for record in caplog.records)
    assert "retcode=1200" in caplog.text and "Timeout: sendMsg" in caplog.text
    assert "JPEG" in caplog.text and str(picture.stat().st_size) in caplog.text
    assert not any(secret in caplog.text for secret in ("private-image", "secret.invalid", "private-response", "private-echo"))
    import io, zipfile
    with zipfile.ZipFile(io.BytesIO(plugin.diagnostics.export_zip())) as archive:
        logs = "".join(archive.read(name).decode() for name in archive.namelist() if name.endswith(".jsonl"))
    assert "image_send_error" in logs and "1200" in logs
    assert "private-response" not in logs and "private-image" not in logs


async def test_legacy_access_keys_are_ignored_and_diagnostics_default_off(app):
    plugin = await app.make(config={"access": {"enabled": True, "client_id": "old", "api_token": "legacy"}})
    assert not hasattr(plugin.settings, "client_id") and not hasattr(plugin.client.config, "api_token")
    assert not plugin.settings.diagnostics_enabled
    assert not (plugin.data_dir / "diagnostics").exists()


async def test_settings_registers_only_config_status_and_download_and_reports_empty_logs(app):
    plugin = await app.make()
    routes = {route: (handler, methods) for route, handler, methods, _ in plugin.context.registered_web_apis}
    assert set(routes) == {
        "/astrbot_plugin_ba_stats/diagnostics/status",
        "/astrbot_plugin_ba_stats/diagnostics/download",
        "/astrbot_plugin_ba_stats/settings/config",
    }
    assert routes["/astrbot_plugin_ba_stats/settings/config"][1] == ["GET", "POST"]
    assert all(methods == ["GET"] for route, (_, methods) in routes.items() if "/diagnostics/" in route)
    status = await routes["/astrbot_plugin_ba_stats/diagnostics/status"][0]()
    assert status.status_code == 200
    assert status.headers["Cache-Control"] == "no-store"
    assert status.data == {"enabled": False, "bytes": 0, "limit_bytes": 30 * 1024 * 1024, "files": 0}
    download = await routes["/astrbot_plugin_ba_stats/diagnostics/download"][0]()
    assert download.media_type == "application/zip"
    assert download.headers["Cache-Control"] == "no-store"
    assert download.headers["Content-Disposition"] == 'attachment; filename="ba-stats-diagnostics.zip"'
    with ZipFile(BytesIO(b"".join(download.content))) as archive:
        assert archive.namelist() == []
    assert not (plugin.data_dir / "diagnostics").exists()


async def test_diagnostics_download_retains_logs_when_recording_is_disabled(app):
    plugin = await app.make()
    plugin.diagnostics.set_enabled(True)
    assert plugin.diagnostics.record("http_response", route="/api/match", status=200, duration_ms=20)
    plugin.diagnostics.set_enabled(False)
    before = plugin.diagnostics.stats()
    response = await plugin._download_diagnostics()
    assert response.status_code == 200
    with ZipFile(BytesIO(b"".join(response.content))) as archive:
        assert archive.testzip() is None
        records = [json.loads(line) for name in archive.namelist()
                   for line in archive.read(name).splitlines()]
    assert records[0]["event"] == "http_response" and records[0]["status"] == 200
    assert not before["enabled"] and before["bytes"] > 0
    assert (await plugin._diagnostics_status()).data == before == plugin.diagnostics.stats()


async def test_diagnostics_handlers_require_host_authenticated_identity(app, monkeypatch):
    plugin = await app.make()
    monkeypatch.setattr(app.web.request, "username", None)
    export = AsyncMock(side_effect=AssertionError("unauthenticated export must not run"))
    monkeypatch.setattr(plugin.diagnostics, "export_zip", export)
    for handler in (plugin._diagnostics_status, plugin._download_diagnostics, plugin._settings_config):
        response = await handler()
        assert response.status_code == 401
        assert response.data["status"] == "error"
        assert response.headers["Cache-Control"] == "no-store"
    export.assert_not_called()


async def test_diagnostics_download_read_failure_returns_safe_error(app, monkeypatch):
    plugin = await app.make()

    def fail():
        raise OSError("PRIVATE/path/token=SECRET")

    monkeypatch.setattr(plugin.diagnostics, "export_zip", fail)
    response = await plugin._download_diagnostics()
    assert response.status_code == 500
    assert response.data["status"] == "error"
    assert "SECRET" not in str(response.data) and "PRIVATE" not in str(response.data)


async def test_terminate_removes_owned_web_routes_and_closes_stale_handlers(app):
    plugin = await app.make()

    async def unrelated():
        return "other plugin"

    plugin.context.register_web_api("/other-plugin/ping", unrelated, ["GET"], "unrelated")
    await plugin.terminate()
    assert plugin.context.registered_web_apis == [("/other-plugin/ping", unrelated, ["GET"], "unrelated")]
    for handler in (plugin._diagnostics_status, plugin._download_diagnostics, plugin._settings_config):
        response = await handler()
        assert response.status_code == 503
        assert response.data["status"] == "error"
    assert not plugin.diagnostics.enabled


async def test_terminate_preserves_a_newer_instances_replacement_route(app):
    plugin = await app.make()

    async def replacement():
        return "new instance"

    route = "/astrbot_plugin_ba_stats/diagnostics/download"
    plugin.context.register_web_api(route, replacement, ["GET"], "new instance")
    await plugin.terminate()
    assert plugin.context.registered_web_apis == [(route, replacement, ["GET"], "new instance")]


def test_diagnostics_schema_defaults_and_view_download_entry(app_modules):
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    options = schema["diagnostics"]["items"]
    assert options["show_options"]["type"] == "bool" and options["show_options"]["default"] is False
    assert options["enabled"]["default"] is False
    assert options["enabled"]["condition"] == {"show_options": True}
    # Hiding options must not silently turn off an already enabled recorder.
    assert app_modules.main.Settings.from_config({
        "diagnostics": {"show_options": False, "enabled": True},
    }).diagnostics_enabled

    class ViewParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.details = 0
            self.download_in_details = False

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "details":
                assert "open" not in attrs
                self.details += 1
            if tag == "button" and attrs.get("id") == "download":
                self.download_in_details = self.details > 0

        def handle_endtag(self, tag):
            if tag == "details":
                self.details -= 1

    html = (ROOT / "views/settings/index.html").read_text(encoding="utf-8")
    parser = ViewParser()
    parser.feed(html)
    assert parser.download_in_details and parser.details == 0
    assert not (ROOT / "views/diagnostics/index.html").exists()


async def test_settings_handler_reads_and_saves_diagnostics_without_restarting_business(app, monkeypatch):
    plugin = await app.make()
    response = await plugin._settings_config()
    assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
    original_client = plugin.client
    original_service = plugin.service
    monkeypatch.setattr(app.web.request, "method", "POST")
    app.web.request.json.return_value = {"revision": response.data["revision"], "changes": {"diagnostics.enabled": True}}
    saved = await plugin._settings_config()
    assert saved.status_code == 200 and saved.data["diagnostics_applied"]
    assert plugin.diagnostics.enabled and plugin.settings.diagnostics_enabled
    assert plugin.client is original_client and plugin.service is original_service
    assert not plugin._closed and not saved.data["reload_required"]
    assert plugin.web_settings.config["diagnostics"]["enabled"] is True


@pytest.mark.parametrize("body", [None, [], "bad", {"changes": {"diagnostics.enabled": True}}])
async def test_settings_malformed_json_is_400_and_never_changes_config(app, monkeypatch, body):
    plugin = await app.make()
    before = plugin.web_settings.config.config_path.read_bytes()
    monkeypatch.setattr(app.web.request, "method", "POST")
    app.web.request.json.return_value = body
    response = await plugin._settings_config()
    assert response.status_code == 400
    assert plugin.web_settings.config.config_path.read_bytes() == before
    assert not plugin.diagnostics.enabled


async def test_settings_json_decoder_error_and_close_during_body_are_rejected(app, monkeypatch):
    plugin = await app.make()
    monkeypatch.setattr(app.web.request, "method", "POST")
    app.web.request.json.side_effect = ValueError("private raw request")
    response = await plugin._settings_config()
    assert response.status_code == 400 and "private" not in str(response.data)

    async def closed_while_reading():
        await plugin.terminate()
        return {"revision": "stale", "changes": {"diagnostics.enabled": True}}

    app.web.request.json.side_effect = closed_while_reading
    response = await plugin._settings_config()
    assert response.status_code == 503 and not plugin.diagnostics.enabled


async def test_settings_conflict_and_business_changes_report_manual_reload(app, monkeypatch):
    plugin = await app.make()
    before = (await plugin._settings_config()).data
    monkeypatch.setattr(app.web.request, "method", "POST")
    app.web.request.json.return_value = {"revision": before["revision"], "changes": {"query.default_limit": 6}}
    response = await plugin._settings_config()
    assert response.status_code == 200 and response.data["reload_required"]
    assert response.data["pending_restart"] == ["query.default_limit"]
    assert plugin.settings.default_limit == 5 and not plugin._closed
    response = await plugin._settings_config()
    assert response.status_code == 409
