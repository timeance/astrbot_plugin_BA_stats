"""Synthetic integration scenarios against the real service and SQLite store.

Network completions use explicit events, never timing-dependent sleeps. The
isolated package permits relative imports without importing AstrBot globally.
"""

import asyncio
import importlib
from pathlib import Path
import re
import sys
from types import ModuleType, SimpleNamespace
from datetime import datetime, timezone
from unittest.mock import AsyncMock, call

import httpx
import pytest


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 17, tzinfo=timezone.utc)


@pytest.fixture
def modules(monkeypatch):
    name = "_ba_stats_service_contract"
    package = ModuleType(name)
    package.__path__ = [str(ROOT)]
    monkeypatch.setitem(sys.modules, name, package)
    try:
        yield SimpleNamespace(**{
            part: importlib.import_module(f"{name}.{part}")
            for part in ("service", "models", "commands", "storage", "settings", "client")
        })
    finally:
        for key in tuple(sys.modules):
            if key.startswith(name + "."):
                sys.modules.pop(key, None)


@pytest.fixture
async def h(modules, tmp_path):
    m = modules.models

    def result(data, **kwargs):
        return m.FetchResult(data, NOW, **kwargs)

    player = m.Player("001", "Synthetic Player", source_url="https://example.invalid/players/001")
    client = SimpleNamespace(
        namespace="batrace:synthetic", player=AsyncMock(return_value=result(player)),
        search=AsyncMock(return_value=result([player])), recent=AsyncMock(return_value=result([])),
        match=AsyncMock(),
    )
    storage = modules.storage.Storage(tmp_path / "service-data")
    await storage.open()
    settings = modules.settings.Settings(cooldown_seconds=0)
    service = modules.service.StatsService(client, storage, settings)
    caller = modules.service.Caller("onebot:test", "group:one", "10001")
    tasks = []

    async def run(text, who=None):
        return await service.execute(modules.commands.parse_command(text), who or caller)

    def spawn(coro):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    try:
        yield SimpleNamespace(
            m=m, modules=modules, client=client, storage=storage, service=service,
            caller=caller, run=run, result=result, player=player, spawn=spawn,
            Binding=modules.storage.Binding,
        )
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        service.close()
        await storage.close()


def select_command(response):
    match = re.search(r"/ba select ([^\s]+) ([0-9]+)", response.text)
    assert match, response.text
    return f"select {match[1]} {match[2]}"


async def seed(h, player_id="old", namespace=None):
    binding = h.Binding(namespace or h.client.namespace, player_id, "Original", "https://example.invalid/original")
    await h.storage.set_binding(h.caller.platform, h.caller.user, binding)
    return binding


async def binding(h):
    return await h.storage.get_binding(h.caller.platform, h.caller.user)


async def test_help_binding_unbind_work_with_real_unconfigured_client(h):
    def forbidden_network(request):
        raise AssertionError("Local commands must not contact the upstream")

    c = h.modules.client
    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden_network)) as http:
        real_client = c.BatraceClient(c.ClientConfig(), storage=h.storage, http_client=http)
        h.service.client = real_client
        try:
            await seed(h, namespace=real_client.namespace)
            assert "/ba help" in (await h.run("help")).text
            assert "old" in (await h.run("binding")).text
            assert "解除" in (await h.run("unbind")).text
            assert await binding(h) is None
            with pytest.raises(c.NotConfiguredError, match="未配置"):
                await h.run("search Synthetic")
            assert real_client.status()["requests"] == 0
        finally:
            await real_client.close()


async def test_single_bind_requires_one_choice_and_forces_identity_revalidation(h):
    old = await seed(h)
    renamed = h.m.Player("001", "Renamed", source_url=h.player.source_url)
    h.client.player.side_effect = [h.result(h.player), h.result(renamed)]
    offered = await h.run("bind stb:001")
    assert "选择后绑定" in offered.text
    assert h.player.source_url in offered.text
    assert await binding(h) == old
    command = select_command(offered)
    response = await h.run(command)
    assert "已绑定默认账号：Renamed" in response.text
    assert (await binding(h)).player_id == "001"
    assert (await binding(h)).display_name == "Renamed"
    assert (await binding(h)).namespace == h.client.namespace
    assert h.client.player.await_args_list == [call("001"), call("001", force=True)]
    with pytest.raises(ValueError, match="过期"):
        await h.run(command)
    assert h.client.player.await_count == 2


@pytest.mark.parametrize("failure", ["wrong_id", "stale", "upstream"])
async def test_failed_final_identity_check_preserves_original_binding(h, failure):
    old = await seed(h)
    offered = await h.run("bind stb:001")
    if failure == "upstream":
        h.client.player.side_effect = h.modules.client.AuthenticationError("鉴权失败")
        expected = h.modules.client.AuthenticationError
    else:
        player = h.m.Player("wrong" if failure == "wrong_id" else "001", "Synthetic")
        h.client.player.return_value = h.result(player, stale=failure == "stale")
        expected = ValueError
    with pytest.raises(expected):
        await h.run(select_command(offered))
    assert await binding(h) == old
    with pytest.raises(ValueError, match="过期"):
        await h.run(select_command(offered))


async def test_read_only_search_and_select_never_change_binding(h, monkeypatch):
    old = await seed(h)
    write = AsyncMock(wraps=h.storage.set_binding)
    monkeypatch.setattr(h.storage, "set_binding", write)
    offered = await h.run("search Synthetic Player")
    assert "请选择要查询" in offered.text
    assert await binding(h) == old
    response = await h.run(select_command(offered))
    assert response.view["kind"] == "overview"
    assert response.view["player"]["id"] == "001"
    assert await binding(h) == old
    write.assert_not_awaited()
    assert h.client.player.await_args_list == [call("001")]
    h.client.recent.assert_awaited_once_with("001", h.service.settings.max_limit)


async def test_late_search_cannot_publish_over_newer_candidates(h):
    started, release = asyncio.Event(), asyncio.Event()
    newer = h.m.Player("002", "New", source_url="https://example.invalid/players/002")

    async def search(query):
        if query == "Old":
            started.set()
            await release.wait()
            return h.result([h.player])
        return h.result([newer])

    h.client.search.side_effect = search
    old_task = h.spawn(h.run("search Old"))
    await asyncio.wait_for(started.wait(), 2)
    current = await h.run("search New")
    release.set()
    assert "覆盖" in (await old_task).text
    h.client.player.return_value = h.result(newer)
    response = await h.run(select_command(current))
    assert response.view["player"]["id"] == "002"


async def test_new_operation_during_selected_revalidation_prevents_binding(h):
    old = await seed(h)
    offered = await h.run("bind stb:001")
    started, release = asyncio.Event(), asyncio.Event()

    async def revalidate(player_id, *, force=False):
        assert force
        started.set()
        await release.wait()
        return h.result(h.player)

    h.client.player.side_effect = revalidate
    task = h.spawn(h.run(select_command(offered)))
    await asyncio.wait_for(started.wait(), 2)
    await h.run("help")
    release.set()
    assert "覆盖" in (await task).text
    assert await binding(h) == old


async def test_cross_group_unbind_wins_over_older_selected_revalidation(h):
    await seed(h)
    offered = await h.run("bind stb:001")
    started, release = asyncio.Event(), asyncio.Event()

    async def revalidate(player_id, *, force=False):
        started.set()
        await release.wait()
        return h.result(h.player)

    h.client.player.side_effect = revalidate
    task = h.spawn(h.run(select_command(offered)))
    await asyncio.wait_for(started.wait(), 2)
    other_group = h.modules.service.Caller(h.caller.platform, "group:two", h.caller.user)
    assert "解除" in (await h.run("unbind", other_group)).text
    release.set()
    assert "覆盖" in (await task).text
    assert await binding(h) is None


async def test_cross_group_unbind_serializes_with_already_started_binding_write(h, monkeypatch):
    await seed(h)
    offered = await h.run("bind stb:001")
    entered, release = asyncio.Event(), asyncio.Event()
    original_set = h.storage.set_binding

    async def slow_set(*args):
        entered.set()
        await release.wait()
        await original_set(*args)

    monkeypatch.setattr(h.storage, "set_binding", slow_set)
    selecting = h.spawn(h.run(select_command(offered)))
    await asyncio.wait_for(entered.wait(), 2)
    other_group = h.modules.service.Caller(h.caller.platform, "group:two", h.caller.user)
    unbinding = h.spawn(h.run("unbind", other_group))
    await asyncio.sleep(0)  # let the unbind enter the service's mutation lock
    assert not unbinding.done()
    release.set()
    await asyncio.gather(selecting, unbinding)
    assert await binding(h) is None


async def test_newer_cross_group_bind_cannot_be_overwritten_by_older_selection(h):
    first = await h.run("bind stb:001")
    second_player = h.m.Player("002", "Second", source_url="https://example.invalid/players/002")
    h.client.player.return_value = h.result(second_player)
    other_group = h.modules.service.Caller(h.caller.platform, "group:two", h.caller.user)
    second = await h.run("bind stb:002", other_group)
    started, release = asyncio.Event(), asyncio.Event()

    async def revalidate(player_id, *, force=False):
        if player_id == "001":
            started.set()
            await release.wait()
            return h.result(h.player)
        return h.result(second_player)

    h.client.player.side_effect = revalidate
    older = h.spawn(h.run(select_command(first)))
    await asyncio.wait_for(started.wait(), 2)
    assert "已绑定" in (await h.run(select_command(second), other_group)).text
    release.set()
    assert "覆盖" in (await older).text
    assert (await binding(h)).player_id == "002"


@pytest.mark.parametrize("target", ["me", "mention"])
async def test_other_namespace_binding_is_rejected_before_upstream_query(h, target):
    old = await seed(h, namespace="batrace:other-source")
    request = h.modules.commands.Request("stats", h.modules.commands.Target(target, h.caller.user))
    with pytest.raises(ValueError, match="数据源.*不一致"):
        await h.service.execute(request, h.caller)
    h.client.player.assert_not_awaited()
    h.client.recent.assert_not_awaited()
    assert await binding(h) == old


async def test_last_skips_unrecorded_details_and_returns_next_available_match(h):
    person = h.m.Participant("001", "Synthetic")
    newest = h.m.Match("new", players=[person])
    available = h.m.Match("old", players=[person], complete=True)
    h.client.recent.return_value = h.result([newest, available])
    h.client.match.side_effect = [h.modules.client.NoDataError("暂无收录"), h.result(available)]
    response = await h.run("last stb:001")
    assert response.view["match"]["id"] == "old"
    assert h.client.match.await_args_list == [call("new"), call("old")]
    assert response.view["player"]["id"] == "001"


async def test_last_does_not_describe_authentication_failure_as_missing_match(h):
    h.client.recent.return_value = h.result([
        h.m.Match("new", players=[h.m.Participant("001", "Synthetic")])
    ])
    h.client.match.side_effect = h.modules.client.AuthenticationError("鉴权失败")
    with pytest.raises(h.modules.client.AuthenticationError, match="鉴权失败"):
        await h.run("last stb:001")
    h.client.match.assert_awaited_once_with("new")


@pytest.mark.parametrize("action", ["stats", "recent", "last"])
async def test_player_reports_reject_nonempty_foreign_rosters(h, action):
    h.client.recent.return_value = h.result([
        h.m.Match("wrong", players=[h.m.Participant("999", "Other")]),
        h.m.Match("valid", players=[h.m.Participant("001", "Synthetic")]),
    ])
    with pytest.raises(h.modules.client.IdentityMismatchError, match="未参与"):
        await h.run(f"{action} stb:001")
    h.client.match.assert_not_awaited()


@pytest.mark.parametrize("detail_player_id", ["999", None, "001"])
async def test_last_validates_real_client_detail_without_skipping_identity_errors(h, monkeypatch, detail_player_id):
    c = h.modules.client
    seen = []

    def handler(request):
        seen.append((request.url.path, dict(request.url.params)))
        if request.url.path == "/api/players/matches":
            return httpx.Response(200, json={"matches": [
                {"matchId": mid, "data": {"StartTime": start, "Data": {"a": {"Id": "001"}}}}
                for mid, start in [("new", 1700000100), ("old", 1700000000)]
            ]})
        assert request.url.path == "/api/match"
        return httpx.Response(200, json={"matchInfo": {
            "Data": {} if detail_player_id is None else {"a": {"Id": detail_player_id}}
        }})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = c.BatraceClient(c.ClientConfig(
            enabled=True, client_id="synthetic-test-client", base_url="https://example.invalid",
            request_interval=0, retries=0,
        ), storage=h.storage, http_client=http)
        monkeypatch.setattr(client, "player", AsyncMock(return_value=h.result(h.player)))
        h.service.client = client
        try:
            if detail_player_id == "999":
                with pytest.raises(c.IdentityMismatchError, match="未参与") as error:
                    await h.run("last stb:001")
                assert error.value.code == "identity_mismatch"
            else:
                view = (await h.run("last stb:001")).view
                assert view["match"]["id"] == "new"
                assert view["player"]["id"] == "001"
                if detail_player_id is None:
                    assert view["teams"] == []
                    assert view["match"]["winner"] is None
            assert seen == [
                ("/api/players/matches", {"stbid": "001", "limit": "20"}),
                ("/api/match", {"matchid": "new"}),
            ]
        finally:
            await client.close()


async def test_empty_recent_rosters_remain_unavailable_without_identity_errors(h):
    h.client.recent.return_value = h.result([h.m.Match("empty")])
    assert (await h.run("recent stb:001")).view["rows"] == []
    assert "暂无可用单局战报" in (await h.run("last stb:001")).text
    h.client.match.assert_not_awaited()


@pytest.mark.parametrize("theme", ["dark", "light"])
async def test_units_use_stable_ids_and_distinct_labels_in_image_and_text(h, theme):
    from html import escape
    from render import Renderer

    records = [
        [{"Id": "u1"}, {"Id": "u2", "Name": "Tank <A>"}, {"Id": "u3"}],
        [{"Id": "u1", "Name": "Tank <A>"}],
        [{"Id": "u1", "Name": "Old Name"}],
    ]
    h.client.recent.return_value = h.result(h.m.parse_recent({"matches": [
        {"matchId": str(index), "data": {"StartTime": 1700000200 - index * 100, "Data": {
            "a": {"Id": "001", "UnitData": {str(i): unit for i, unit in enumerate(units)}},
            "b": {"Id": "999", "UnitData": {"x": {"Id": "other", "Name": "Other player's unit"}}},
        }}}
        for index, units in enumerate(records)
    ]}))
    view = (await h.run("stats stb:001")).view
    assert view["common_units"] == [
        {"id": "u1", "name": "Tank <A>（ID：u1）", "count": 3},
        {"id": "u2", "name": "Tank <A>（ID：u2）", "count": 1},
        {"id": "u3", "name": "单位#u3", "count": 1},
    ]
    renderer = Renderer(theme=theme)
    html, text = renderer.template(view), renderer.text(view)
    for unit in view["common_units"]:
        assert f"{unit['name']}：{unit['count']} 个" in text
        assert (f'<span class="unit-name">{escape(unit["name"])}</span>'
                f'<span class="unit-count">{unit["count"]} 个</span>') in html
    assert "Old Name" not in html + text
    assert "Other player's unit" not in html + text


async def test_recent_view_distinguishes_zero_unknown_and_lossless_ratios(h):
    people = [
        h.m.Participant("001", "S", kills=0, losses=2, destroyed_value=0, lost_value=100),
        h.m.Participant("001", "S", kills=2, losses=0, destroyed_value=500, lost_value=0),
        h.m.Participant("001", "S", kills=None, losses=2, destroyed_value=None, lost_value=0),
        h.m.Participant("001", "S", kills=0, losses=0, destroyed_value=0, lost_value=0),
        h.m.Participant("001", "S", kills=2, losses=1, destroyed_value=1000, lost_value=100),
    ]
    h.client.recent.return_value = h.result([
        h.m.Match(str(index), players=[person]) for index, person in enumerate(people)
    ])
    rows = (await h.run("recent stb:001 --limit 5")).view["rows"]
    assert [row["unit_exchange"] for row in rows] == [0, "无损失", None, None, 2]
    assert [row["value_exchange"] for row in rows] == [0, "无损失", None, None, 10]
    assert rows[2]["kills"] is None
    assert all(row["outcome"] == "unknown" for row in rows)


async def test_overview_uses_actual_sample_and_known_outcomes_only(h):
    h.client.recent.return_value = h.result([
        h.m.Match(str(index), players=[h.m.Participant("001", "S", outcome=outcome)])
        for index, outcome in enumerate(["win", "loss", "unknown"])
    ], stale=True)
    view = (await h.run("stats stb:001")).view
    metrics = {item["label"]: item for item in view["metrics"]}
    assert metrics["样本场数"]["value"] == 3
    assert metrics["胜 / 负 / 未知"]["value"] == "1 / 1 / 1"
    assert metrics["已知结果胜率"]["value"] == 50
    assert "2 场" in metrics["已知结果胜率"]["note"]
    assert "实际 3 场" in view["scope"]
    assert "非完整历史" in view["scope"]
    assert view["stale"] is True


async def test_service_real_client_pipeline_sorts_and_deduplicates_recent_samples(h, monkeypatch):
    c = h.modules.client
    client = c.BatraceClient(c.ClientConfig(
        enabled=True, client_id="synthetic-test-client", base_url="https://example.invalid",
        request_interval=0, retries=0,
    ), storage=h.storage)

    def raw(start):
        return {"StartTime": start, "EndTime": start + 60, "TotalPlayTimeInSec": 60,
                "Data": {"a": {"Id": "001", "Name": "Synthetic", "UnitData": {}}}}

    payload = {"matches": [
        {"matchId": "old", "data": raw(1700000000)},
        {"matchId": "new", "data": raw(1700000100)},
        {"matchId": "new", "data": raw(1700000100)},
    ]}
    request = AsyncMock(return_value=payload)
    monkeypatch.setattr(client, "_request", request)
    monkeypatch.setattr(client, "player", AsyncMock(return_value=h.result(h.player)))
    h.service.client = client
    try:
        view = (await h.run("recent stb:001 --limit 5")).view
        assert [row["id"] for row in view["rows"]] == ["new", "old"]
        assert "实际 2 场" in view["scope"]
        request.assert_awaited_once_with("/api/players/matches", {"stbid": "001", "limit": 5})
    finally:
        await client.close()
