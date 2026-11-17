"""Use temporary SQLite databases, never production player data."""

import asyncio

import pytest

from storage import Binding, Storage


@pytest.mark.parametrize("operation", ["set_binding", "unbind", "put_cache", "clear_cache", "prune_cache"])
@pytest.mark.parametrize("failure", ["error", "cancel"])
async def test_failed_writes_roll_back_before_connection_reuse(tmp_path, monkeypatch, operation, failure):
    store, observer = Storage(tmp_path), Storage(tmp_path)
    await store.open()
    await observer.open()
    original = Binding("source", "001", "Original")
    try:
        await store.set_binding("p", "u", original)
        await store.put_cache("existing", {"version": 1}, 60)
        mutations = {
            "set_binding": lambda: store.set_binding("p", "u", Binding("source", "002", "Replacement")),
            "unbind": lambda: store.unbind("p", "u"),
            "put_cache": lambda: store.put_cache("existing", {"version": 2}, 60),
            "clear_cache": store.clear_cache,
            "prune_cache": lambda: store.prune_cache(max_entries=0),
        }
        committing = asyncio.Event()

        async def fail_commit():
            committing.set()
            if failure == "cancel":
                await asyncio.Event().wait()
            raise RuntimeError("synthetic commit failure")

        with monkeypatch.context() as patch:
            patch.setattr(store._connection(), "commit", fail_commit)
            task = asyncio.create_task(mutations[operation]())
            try:
                await asyncio.wait_for(committing.wait(), 2)
                if failure == "cancel":
                    task.cancel()
                expected = asyncio.CancelledError if failure == "cancel" else RuntimeError
                with pytest.raises(expected):
                    await asyncio.wait_for(task, 2)
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        assert not store._connection().in_transaction
        # A later successful write must neither commit the failed mutation nor
        # encounter a transaction left open by it.
        assert (await store.consume_request())["global"] == 1
        await store.put_cache("recovery", {}, 60)
        for connection in (store, observer):
            assert await connection.get_binding("p", "u") == original
            assert (await connection.get_cache("existing"))[0] == {"version": 1}
            assert await connection.cache_count() == 2
    finally:
        await observer.close()
        await store.close()


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now


def test_bindings_survive_restart_and_isolate_platform_and_user(tmp_path):
    async def run():
        first = Binding("batrace", "000765611980123456789012345", "Player 2", "https://example.invalid/player", 10.5)
        other = Binding("batrace", "2", "Other", updated_at=11.0)
        db = Storage(tmp_path / "plugin-data")
        await db.open()
        await db.open()
        try:
            await db.set_binding("onebot:a", "001", first)
            await db.set_binding("onebot:b", "001", other)
            await db.set_binding("onebot:a", "002", other)
            assert await db.get_binding("onebot:a", "missing") is None
        finally:
            await db.close()
        await db.close()
        restarted = Storage(tmp_path / "plugin-data")
        await restarted.open()
        try:
            assert await restarted.get_binding("onebot:a", "001") == first
            assert isinstance((await restarted.get_binding("onebot:a", "001")).player_id, str)
            assert await restarted.get_binding("onebot:b", "001") == other
            assert await restarted.get_binding("onebot:a", "002") == other
            renamed = Binding(first.namespace, first.player_id, "Renamed", first.source_url, 20.0)
            await restarted.set_binding("onebot:a", "001", renamed)
            assert await restarted.get_binding("onebot:a", "001") == renamed
            assert await restarted.unbind("onebot:a", "001")
            assert not await restarted.unbind("onebot:a", "001")
            assert await restarted.get_binding("onebot:a", "001") is None
            assert await restarted.get_binding("onebot:b", "001") == other
        finally:
            await restarted.close()
        await restarted.open()
        try:
            assert await restarted.get_binding("onebot:a", "001") is None
        finally:
            await restarted.close()
    asyncio.run(run())


def test_json_cache_timing_expiry_stale_read_and_restart(tmp_path):
    async def run():
        clock = Clock()
        db = Storage(tmp_path, clock=clock)
        await db.open()
        payload = {"id": "000765611980123456789012345", "name": "玩家", "unknown": None,
                   "nested": [{"win": False, "elo": 1234.5}]}
        try:
            assert await db.get_cache("missing") is None
            await db.put_cache("batrace:player:001:recent:5", payload, 10)
            clock.now = 1010.0
            assert await db.get_cache("batrace:player:001:recent:5") == (payload, 1000.0, 1010.0)
            await db.put_cache("batrace:player:001:recent:10", [], 0)
            assert await db.cache_count() == 2
        finally:
            await db.close()
        await db.open()
        try:
            assert await db.get_cache("batrace:player:001:recent:5") == (payload, 1000.0, 1010.0)
            await db.put_cache("batrace:player:001:recent:5", [{"updated": True}], 60)
            assert await db.get_cache("batrace:player:001:recent:5") == ([{"updated": True}], 1010.0, 1070.0)
            assert await db.cache_count() == 2
        finally:
            await db.close()
    asyncio.run(run())


def test_cache_clear_and_age_capacity_pruning_preserve_binding(tmp_path):
    async def run():
        clock = Clock()
        db = Storage(tmp_path, clock=clock)
        await db.open()
        binding = Binding("batrace", "001", "Player", updated_at=1.0)
        try:
            await db.set_binding("p", "u", binding)
            for index in range(4):
                clock.now = 1000.0 + index * 10
                await db.put_cache(str(index), {"i": index}, 1)
            assert await db.prune_cache(max_entries=2) == 2
            assert await db.get_cache("0") is None
            assert await db.get_cache("1") is None
            assert await db.get_cache("2") is not None
            clock.now = 1040
            assert await db.prune_cache(max_age_seconds=15) == 1
            assert await db.get_cache("2") is None
            assert await db.get_cache("3") is not None
            assert await db.get_binding("p", "u") == binding
            await db.clear_cache()
            assert await db.cache_count() == 0
            assert await db.get_binding("p", "u") == binding
            await db.put_cache("new", [], 30)
            assert await db.prune_cache(max_entries=0) == 1
            assert await db.get_binding("p", "u") == binding
        finally:
            await db.close()
    asyncio.run(run())


def test_default_retention_prunes_old_stale_entries_on_write(tmp_path):
    async def run():
        clock = Clock()
        db = Storage(tmp_path, clock=clock)
        await db.open()
        try:
            await db.put_cache("old", {}, 10)
            clock.now += 604800
            await db.put_cache("new", [], 10)
            assert await db.get_cache("old") is None
            assert await db.cache_count() == 1
        finally:
            await db.close()
    asyncio.run(run())


def test_configured_cache_bounds_apply_on_write_and_manual_prune(tmp_path):
    async def run():
        clock = Clock()
        db = Storage(tmp_path, max_entries=2, max_age_seconds=10, clock=clock)
        await db.open()
        try:
            for index in range(3):
                clock.now += 1
                await db.put_cache(str(index), {}, 1)
            assert await db.get_cache("0") is None
            assert await db.cache_count() == 2
            assert await db.prune_cache() == 0
            clock.now += 10
            assert await db.prune_cache() == 2
        finally:
            await db.close()
    asyncio.run(run())


def test_configured_cache_capacity_can_exceed_one_thousand(tmp_path):
    async def run():
        db = Storage(tmp_path, max_entries=1002)
        await db.open()
        try:
            for index in range(1001):
                await db.put_cache(str(index), [], 60)
            assert await db.cache_count() == 1001
            assert await db.prune_cache() == 0
            assert await db.cache_count() == 1001
        finally:
            await db.close()
    asyncio.run(run())


def test_concurrent_writes_and_cache_maintenance_do_not_affect_bindings(tmp_path):
    async def run():
        db = Storage(tmp_path)
        await db.open()
        try:
            await asyncio.gather(*(
                db.set_binding("p", str(index), Binding("ns", str(index), str(index)))
                for index in range(20)
            ), *(db.put_cache(str(index), {"i": index}, 30) for index in range(20)))
            await asyncio.gather(db.clear_cache(), db.set_binding("p", "new", Binding("ns", "001", "New")))
            assert await db.cache_count() == 0
            for index in range(20):
                assert (await db.get_binding("p", str(index))).player_id == str(index)
            assert (await db.get_binding("p", "new")).player_id == "001"
        finally:
            await db.close()
    asyncio.run(run())


@pytest.mark.parametrize("player_id", [123, 123.5, None, ""])
def test_bindings_never_convert_numeric_ids_to_float_or_integer(player_id):
    with pytest.raises(ValueError):
        Binding("batrace", player_id, "Player")


def test_invalid_cache_parameters_and_closed_lifecycle(tmp_path):
    async def run():
        db = Storage(tmp_path)
        with pytest.raises(RuntimeError, match="尚未打开"):
            await db.get_binding("p", "u")
        await db.open()
        try:
            for ttl in [-1, float("nan"), float("inf")]:
                with pytest.raises(ValueError):
                    await db.put_cache("bad", {}, ttl)
            with pytest.raises(ValueError):
                await db.put_cache("bad", "text", 1)
            for kwargs in [{"max_entries": -1}, {"max_entries": 1.5},
                           {"max_age_seconds": -1}, {"max_age_seconds": float("nan")}]:
                with pytest.raises(ValueError):
                    await db.prune_cache(**kwargs)
            assert await db.cache_count() == 0
        finally:
            await db.close()
        with pytest.raises(RuntimeError):
            await db.cache_count()
    asyncio.run(run())
