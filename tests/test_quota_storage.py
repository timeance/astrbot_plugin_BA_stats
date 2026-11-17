"""Persistent quota accounting with real temporary SQLite connections.

No API requests: all dates, contention, cancellation and failures are local.
"""

import asyncio
from dataclasses import FrozenInstanceError
from datetime import datetime
import sqlite3

import pytest

from quota import GLOBAL_DAILY_LIMIT, QuotaActor, QuotaExceededError, quota_day
from storage import Binding, Storage


class Clock:
    def __init__(self, value="2026-11-17T15:59:59+00:00"):
        self.now = datetime.fromisoformat(value).timestamp()

    def __call__(self):
        return self.now


@pytest.fixture
async def stores(tmp_path):
    connections, tasks = [], []
    clock = Clock()

    async def connect():
        store = Storage(tmp_path / "plugin-data", clock=clock)
        await store.open()
        connections.append(store)
        return store

    def spawn(coroutine):
        task = asyncio.create_task(coroutine)
        tasks.append(task)
        return task

    try:
        yield connect, clock, spawn
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for store in connections:
            await store.close()


def assert_exceeded(error, scope, limit, used):
    assert isinstance(error, ValueError)
    assert error.code == "daily_quota"
    assert (error.scope, error.limit, error.used) == (scope, limit, used)
    assert "北京时间 0 点重置" in str(error)
    assert str(limit) in str(error) and str(used) in str(error)


def test_actor_is_immutable_and_preserves_identity_strings():
    actor = QuotaActor("onebot:001", "00012345678901234567890")
    assert actor.group == ""
    assert actor.user == "00012345678901234567890"
    with pytest.raises(FrozenInstanceError):
        actor.user = "another"


@pytest.mark.parametrize("args", [
    ("", "u"), ("p", ""), (" ", "u"), ("p", " \t"),
    (None, "u"), ("p", 123), ("p", "u", None), ("p", "u", " "),
])
def test_actor_rejects_missing_platform_or_user_and_invalid_group(args):
    with pytest.raises(ValueError):
        QuotaActor(*args)


@pytest.mark.parametrize(("iso", "expected"), [
    ("2026-11-17T15:59:59.999999+00:00", "2026-11-17"),
    ("2026-11-17T16:00:00+00:00", "2026-11-18"),
    ("2026-12-31T16:00:00+00:00", "2027-01-01"),
])
def test_quota_day_uses_beijing_calendar_boundary(iso, expected):
    assert quota_day(datetime.fromisoformat(iso).timestamp()) == expected


async def test_usage_is_read_only_and_actor_none_counts_global_only(stores):
    connect, _, _ = stores
    store = await connect()
    actor = QuotaActor("p", "u", "g")
    before_changes = store._connection().total_changes
    assert await store.quota_usage() == {"day": "2026-11-17", "global": 0, "user": None, "group": None}
    assert await store.quota_usage(actor) == {"day": "2026-11-17", "global": 0, "user": 0, "group": 0}
    assert store._connection().total_changes == before_changes
    assert await store.consume_request() == {"day": "2026-11-17", "global": 1, "user": None, "group": None}
    assert await store.quota_usage(actor) == {"day": "2026-11-17", "global": 1, "user": 0, "group": 0}


async def test_default_user_twenty_boundary_does_not_charge_other_buckets(stores):
    connect, _, _ = stores
    store = await connect()
    actor = QuotaActor("p", "u", "group-one")
    for count in range(1, 21):
        assert (await store.consume_request(actor))["user"] == count
    another_group = QuotaActor("p", "u", "group-two")
    with pytest.raises(QuotaExceededError) as error:
        await store.consume_request(another_group)
    assert_exceeded(error.value, "user", 20, 20)
    assert await store.quota_usage(another_group) == {
        "day": "2026-11-17", "global": 20, "user": 20, "group": 0,
    }
    assert (await store.quota_usage(actor))["group"] == 20
    assert (await store.consume_request(QuotaActor("p", "other", "group-two")))["global"] == 21


async def test_group_accumulates_across_users_and_rejection_does_not_charge_new_user(stores):
    connect, _, _ = stores
    store = await connect()
    for index in range(3):
        result = await store.consume_request(QuotaActor("p", str(index), "g"), group_limit=3)
        assert result["user"] == 1
        assert result["group"] == index + 1
    actor = QuotaActor("p", "new-user", "g")
    with pytest.raises(QuotaExceededError) as error:
        await store.consume_request(actor, group_limit=3)
    assert_exceeded(error.value, "group", 3, 3)
    assert await store.quota_usage(actor) == {"day": "2026-11-17", "global": 3, "user": 0, "group": 3}
    assert await store.consume_request(QuotaActor("p", actor.user, "other"), group_limit=3) == {
        "day": "2026-11-17", "global": 4, "user": 1, "group": 1,
    }


async def test_user_shared_across_groups_and_private_but_platforms_are_isolated(stores):
    connect, _, _ = stores
    store = await connect()
    for group in ("g1", "g2", ""):
        await store.consume_request(QuotaActor("p1", "u", group), user_limit=3)
    private = QuotaActor("p1", "u")
    with pytest.raises(QuotaExceededError) as error:
        await store.consume_request(private, user_limit=3)
    assert_exceeded(error.value, "user", 3, 3)
    assert await store.quota_usage(private) == {"day": "2026-11-17", "global": 3, "user": 3, "group": None}
    assert (await store.quota_usage(QuotaActor("p1", "u", "g1")))["group"] == 1
    assert await store.consume_request(QuotaActor("p2", "u", "g1"), user_limit=3) == {
        "day": "2026-11-17", "global": 4, "user": 1, "group": 1,
    }


@pytest.mark.parametrize("unlimited", [False, True])
async def test_fixed_global_hundred_and_default_group_hundred_boundaries(stores, unlimited):
    connect, _, _ = stores
    store = await connect()
    assert GLOBAL_DAILY_LIMIT == 100
    for index in range(100):
        actor = QuotaActor("p", "one-user" if unlimited else str(index // 20), "g")
        result = await store.consume_request(actor, user_limit=0 if unlimited else 20,
                                             group_limit=0 if unlimited else 100)
    assert result["global"] == 100 and result["group"] == 100
    assert result["user"] == (100 if unlimited else 20)
    rejected = QuotaActor("new-platform", "new-user", "new-group")
    with pytest.raises(QuotaExceededError) as error:
        await store.consume_request(rejected, user_limit=0, group_limit=0)
    assert_exceeded(error.value, "global", 100, 100)
    assert await store.quota_usage(rejected) == {"day": "2026-11-17", "global": 100, "user": 0, "group": 0}
    with pytest.raises(QuotaExceededError) as error:
        await store.consume_request()
    assert_exceeded(error.value, "global", 100, 100)


@pytest.mark.parametrize("scope", ["user", "group"])
async def test_lowered_limits_apply_to_already_recorded_unlimited_usage(stores, scope):
    connect, _, _ = stores
    store = await connect()
    actor = QuotaActor("p", "u", "g")
    for _ in range(4):
        await store.consume_request(actor, user_limit=0, group_limit=0)
    limits = {"user_limit": 0, "group_limit": 0, f"{scope}_limit": 3}
    with pytest.raises(QuotaExceededError) as error:
        await store.consume_request(actor, **limits)
    assert_exceeded(error.value, scope, 3, 4)
    assert await store.quota_usage(actor) == {"day": "2026-11-17", "global": 4, "user": 4, "group": 4}
    limits[f"{scope}_limit"] = 5
    assert await store.consume_request(actor, **limits) == {
        "day": "2026-11-17", "global": 5, "user": 5, "group": 5,
    }


@pytest.mark.parametrize(("scope", "limit", "kwargs"), [
    ("global", 100, {"user_limit": 0, "group_limit": 0}),
    ("user", 20, {"user_limit": 20, "group_limit": 0}),
    ("group", 30, {"user_limit": 0, "group_limit": 30}),
])
async def test_multiple_connections_cannot_overdraw_any_bucket(stores, scope, limit, kwargs):
    connect, _, spawn = stores
    connections = [await connect() for _ in range(4)]
    actor = QuotaActor("p", "u", "g")
    tasks = [spawn(connections[index % 4].consume_request(actor, **kwargs))
             for index in range(limit + 20)]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    successes = [result for result in results if isinstance(result, dict)]
    failures = [result for result in results if isinstance(result, BaseException)]
    assert len(successes) == limit
    assert len(failures) == 20
    assert sorted(result["global"] for result in successes) == list(range(1, limit + 1))
    for error in failures:
        assert isinstance(error, QuotaExceededError)
        assert_exceeded(error, scope, limit, limit)
    for store in connections:
        assert await store.quota_usage(actor) == {
            "day": "2026-11-17", "global": limit, "user": limit, "group": limit,
        }


async def test_beijing_midnight_resets_all_daily_views_without_erasing_prior_day(stores):
    connect, clock, _ = stores
    store = await connect()
    actor = QuotaActor("p", "u", "g")
    await store.consume_request(actor, user_limit=1)
    with pytest.raises(QuotaExceededError):
        await store.consume_request(actor, user_limit=1)
    clock.now += 1  # Beijing 23:59:59 -> 00:00:00
    assert await store.quota_usage(actor) == {"day": "2026-11-18", "global": 0, "user": 0, "group": 0}
    assert await store.consume_request(actor, user_limit=1) == {
        "day": "2026-11-18", "global": 1, "user": 1, "group": 1,
    }
    clock.now -= 1
    assert await store.quota_usage(actor) == {"day": "2026-11-17", "global": 1, "user": 1, "group": 1}


async def test_date_is_evaluated_after_obtaining_cross_connection_write_lock(stores):
    connect, clock, spawn = stores
    locker, contender = await connect(), await connect()
    actor = QuotaActor("p", "u", "g")
    loop = asyncio.get_running_loop()
    begin_started = asyncio.Event()

    def trace(sql):
        if sql == "BEGIN IMMEDIATE":
            loop.call_soon_threadsafe(begin_started.set)

    await contender._connection().set_trace_callback(trace)
    async with locker._connection().execute("BEGIN IMMEDIATE"):
        pass
    try:
        task = spawn(contender.consume_request(actor))
        await asyncio.wait_for(begin_started.wait(), 2)
        clock.now += 1
    finally:
        await locker._connection().rollback()
    assert (await asyncio.wait_for(task, 2))["day"] == "2026-11-18"
    clock.now -= 1
    assert (await locker.quota_usage())["global"] == 0


async def test_reload_cache_clear_prune_and_binding_namespace_changes_keep_usage(stores):
    connect, _, _ = stores
    store = await connect()
    actor = QuotaActor("p", "u", "g")
    expected = await store.consume_request(actor)
    original = Binding("source:old-credentials", "001", "Synthetic")
    await store.set_binding("p", "u", original)
    await store.put_cache("source:old-credentials", {}, 10)
    await store.clear_cache()
    await store.prune_cache(max_entries=0, max_age_seconds=0)
    assert await store.quota_usage(actor) == expected
    assert await store.get_binding("p", "u") == original
    await store.close()
    reopened = await connect()
    assert await reopened.quota_usage(actor) == expected
    await reopened.set_binding("p", "u", Binding("source:new-credentials", "002", "Other"))
    await reopened.put_cache("source:new-credentials", [], 10)
    assert await reopened.quota_usage(actor) == expected
    assert (await reopened.consume_request(actor))["global"] == 2


async def test_sql_error_after_partial_bucket_updates_rolls_back_everything(stores):
    connect, _, _ = stores
    store, observer = await connect(), await connect()
    actor = QuotaActor("p", "u", "g")
    db = store._connection()
    async with db.execute(
        "CREATE TEMP TRIGGER reject_group BEFORE INSERT ON request_quotas "
        "WHEN NEW.scope = 'group' BEGIN SELECT RAISE(ABORT, 'synthetic quota failure'); END"
    ):
        pass
    with pytest.raises(sqlite3.IntegrityError, match="synthetic quota failure"):
        await store.consume_request(actor)
    assert not db.in_transaction
    for connection in (store, observer):
        assert await connection.quota_usage(actor) == {
            "day": "2026-11-17", "global": 0, "user": 0, "group": 0,
        }
    async with db.execute("DROP TRIGGER reject_group"):
        pass
    assert (await store.consume_request(actor))["global"] == 1


async def test_cancellation_before_commit_rolls_back_and_finishes_cleanup(stores, monkeypatch):
    connect, _, spawn = stores
    store, observer = await connect(), await connect()
    actor = QuotaActor("p", "u", "g")
    db = store._connection()
    commit_started, rollback_started, allow_rollback = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_rollback = db.rollback

    async def paused_commit():
        commit_started.set()
        await asyncio.Event().wait()

    async def paused_rollback():
        rollback_started.set()
        await allow_rollback.wait()
        await original_rollback()

    with monkeypatch.context() as patch:
        patch.setattr(db, "commit", paused_commit)
        patch.setattr(db, "rollback", paused_rollback)
        task = spawn(store.consume_request(actor))
        try:
            await asyncio.wait_for(commit_started.wait(), 2)
            task.cancel()
            await asyncio.wait_for(rollback_started.wait(), 2)
            task.cancel()  # repeated cancellation must not abandon rollback
        finally:
            allow_rollback.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
    assert not db.in_transaction
    assert await observer.quota_usage(actor) == {"day": "2026-11-17", "global": 0, "user": 0, "group": 0}
    assert (await store.consume_request(actor))["global"] == 1


async def test_cancelled_queued_begin_cannot_leave_an_open_transaction(stores):
    connect, _, spawn = stores
    locker, contender = await connect(), await connect()
    loop = asyncio.get_running_loop()
    begin_started = asyncio.Event()

    def trace(sql):
        if sql == "BEGIN IMMEDIATE":
            loop.call_soon_threadsafe(begin_started.set)

    await contender._connection().set_trace_callback(trace)
    async with locker._connection().execute("BEGIN IMMEDIATE"):
        pass
    try:
        task = spawn(contender.consume_request(QuotaActor("p", "u", "g")))
        await asyncio.wait_for(begin_started.wait(), 2)
        task.cancel()
    finally:
        await locker._connection().rollback()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    assert not contender._connection().in_transaction
    assert (await locker.quota_usage())["global"] == 0
    assert (await contender.consume_request())["global"] == 1


@pytest.mark.parametrize("invalid", [-1, 1.5, True, "20"])
async def test_invalid_limits_are_rejected_without_charging(stores, invalid):
    connect, _, _ = stores
    store = await connect()
    for field in ("user_limit", "group_limit"):
        with pytest.raises(ValueError):
            await store.consume_request(QuotaActor("p", "u", "g"), **{field: invalid})
    assert (await store.quota_usage())["global"] == 0


@pytest.mark.parametrize("reject_at", [1, 2])
async def test_changed_admission_rolls_back_all_buckets(stores, reject_at):
    from quota import AdmissionDeferred
    connect, _, _ = stores
    store, observer = await connect(), await connect()
    actor = QuotaActor("p", "u", "g")
    calls = 0
    def ready():
        nonlocal calls
        calls += 1
        return calls != reject_at
    with pytest.raises(AdmissionDeferred):
        await store.consume_request(actor, ready=ready)
    assert not store._connection().in_transaction
    assert (await observer.quota_usage(actor))["global"] == 0
    assert (await store.consume_request(actor))["global"] == 1


async def test_midnight_during_reservation_rolls_back_before_retry(stores):
    from quota import AdmissionDeferred
    connect, clock, _ = stores
    store = await connect()
    actor = QuotaActor("p", "u", "g")
    original = store._quota_usage
    async def cross_midnight(*args):
        usage = await original(*args)
        clock.now += 1
        return usage
    store._quota_usage = cross_midnight
    with pytest.raises(AdmissionDeferred):
        await store.consume_request(actor)
    store._quota_usage = original
    assert (await store.consume_request(actor))["day"] == "2026-11-18"
    clock.now -= 1
    assert (await store.quota_usage(actor))["global"] == 0
