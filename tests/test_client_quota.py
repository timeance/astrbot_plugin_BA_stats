"""Client quota behavior using temporary SQLite and SYNTHETIC MockTransport data.

No BATrace endpoint or real account is contacted. Storage atomicity and Beijing
midnight rollover have their own storage tests and are not duplicated here.
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime

import httpx
import pytest

import client as client_module

from client import (
    AuthenticationError, BatraceClient, ClientConfig, NoDataError,
    RateLimitError, RequestTimeoutError, SchemaError, UpstreamError,
)
from quota import QuotaActor, QuotaExceededError
from quota import quota_day
from storage import Storage


def configuration(**overrides):
    values = dict(enabled=True, base_url="https://synthetic.example.invalid",
                  client_id="synthetic-quota-client", request_interval=0,
                  retries=0, wait_timeout=5, request_timeout=1)
    values.update(overrides)
    return ClientConfig(**values)


def response():
    return httpx.Response(200, json={"players": [{"id": "001", "name": "Synthetic"}]})


@asynccontextmanager
async def running_client(tmp_path, handler, **overrides):
    storage = Storage(tmp_path / "plugin-data")
    await storage.open()
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = BatraceClient(configuration(**overrides), storage=storage, http_client=http)
            try:
                yield client, storage
            finally:
                await client.close()
    finally:
        await storage.close()


async def search(client, actor, query, *, force=False):
    with client.request_scope(actor):
        return await client.search(query, force=force)


async def counts(storage, actor):
    usage = await storage.quota_usage(actor)
    return usage["global"], usage["user"], usage["group"]


def assert_quota(error, scope, limit, used):
    assert isinstance(error, QuotaExceededError)
    assert (error.code, error.scope, error.limit, error.used) == ("daily_quota", scope, limit, used)


def expire_cache_reads(monkeypatch, storage):
    """Expose retained data as one hour older, without changing daily quota time."""
    original = storage.get_cache

    async def expired(key):
        hit = await original(key)
        if hit is None:
            return None
        payload, fetched_at, expires_at = hit
        return payload, fetched_at - 3600, expires_at - 3600

    monkeypatch.setattr(storage, "get_cache", expired)


@pytest.mark.asyncio
async def test_100th_and_101st_http_attempts_race_at_fixed_global_limit(tmp_path):
    calls = []
    def handler(request):
        calls.append(request)
        return response()

    async with running_client(tmp_path, handler) as (client, storage):
        # Real adapter attempts establish 99; each sender stays below default 20.
        for i in range(99):
            actor = QuotaActor("onebot:a", f"sender-{i // 20}", "group-A")
            await search(client, actor, f"Synthetic {i}", force=True)
        actor = QuotaActor("onebot:a", "last-sender", "group-A")
        results = await asyncio.gather(
            search(client, actor, "Boundary A", force=True),
            search(client, actor, "Boundary B", force=True),
            return_exceptions=True,
        )
        errors = [r for r in results if isinstance(r, BaseException)]
        assert len(errors) == 1
        assert_quota(errors[0], "global", 100, 100)
        assert len(calls) == client.status()["requests"] == 100
        assert await counts(storage, actor) == (100, 1, 100)
        # Even a different user/group cannot bypass the fixed plugin-wide cap.
        other = QuotaActor("onebot:b", "another", "group-B")
        with pytest.raises(QuotaExceededError) as denied:
            await search(client, other, "Other source caller", force=True)
        assert_quota(denied.value, "global", 100, 100)
        assert await counts(storage, other) == (100, 0, 0)


@pytest.mark.asyncio
async def test_default_user_20_counts_across_groups_and_private_chat(tmp_path):
    calls = []
    def handler(request):
        calls.append(request)
        return response()

    async with running_client(tmp_path, handler) as (client, storage):
        actors = [QuotaActor("onebot:a", "same-user", group) for group in ("A", "B", "")]
        for i in range(20):
            await search(client, actors[i % 3], f"Synthetic {i}", force=True)
        for actor in actors:
            with pytest.raises(QuotaExceededError) as denied:
                await search(client, actor, "Beyond personal limit", force=True)
            assert_quota(denied.value, "user", 20, 20)
        assert await counts(storage, actors[0]) == (20, 20, 7)
        assert await counts(storage, actors[1]) == (20, 20, 7)
        assert await counts(storage, actors[2]) == (20, 20, None)
        other = QuotaActor("onebot:a", "other-user", "A")
        await search(client, other, "Other user", force=True)
        assert await counts(storage, other) == (21, 1, 8)
        assert len(calls) == 21


@pytest.mark.asyncio
async def test_configured_group_limit_applies_to_all_senders_but_not_private_chat(tmp_path):
    async with running_client(tmp_path, lambda r: response(), group_daily_limit=2) as (client, storage):
        a = QuotaActor("onebot:a", "A", "limited-group")
        b = QuotaActor("onebot:a", "B", "limited-group")
        c = QuotaActor("onebot:a", "C", "limited-group")
        await search(client, a, "First", force=True)
        await search(client, b, "Second", force=True)
        with pytest.raises(QuotaExceededError) as denied:
            await search(client, c, "Third", force=True)
        assert_quota(denied.value, "group", 2, 2)
        assert await counts(storage, c) == (2, 0, 2)
        private = QuotaActor("onebot:a", "C")
        await search(client, private, "Private", force=True)
        assert await counts(storage, private) == (3, 1, None)


@pytest.mark.asyncio
async def test_fresh_cache_costs_zero_at_limit_force_and_expired_cache_are_denied(tmp_path, monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        return response()

    async with running_client(tmp_path, handler, user_daily_limit=1) as (client, storage):
        actor = QuotaActor("onebot:a", "A", "G")
        await search(client, actor, "Synthetic")
        assert not (await search(client, actor, "Synthetic")).stale
        with pytest.raises(QuotaExceededError) as denied:
            await search(client, actor, "Synthetic", force=True)
        assert_quota(denied.value, "user", 1, 1)
        expire_cache_reads(monkeypatch, storage)
        with pytest.raises(QuotaExceededError):
            await search(client, actor, "Synthetic")
        assert await counts(storage, actor) == (1, 1, 1)
        assert len(calls) == 1
        assert client.status()["stale_hits"] == 0
        assert client.status()["last_error"] == "daily_quota"


@pytest.mark.asyncio
async def test_each_submitted_failure_counts_and_is_not_refunded(tmp_path):
    sequence = ["auth", "no_data", "server", "timeout", "network", "schema", "rate"]
    errors = [AuthenticationError, NoDataError, UpstreamError, RequestTimeoutError,
              UpstreamError, SchemaError, RateLimitError]
    calls = []
    def handler(request):
        kind = sequence[len(calls)]
        calls.append(kind)
        if kind == "timeout":
            raise httpx.ReadTimeout("SYNTHETIC", request=request)
        if kind == "network":
            raise httpx.ConnectError("SYNTHETIC", request=request)
        status = {"auth": 403, "no_data": 404, "server": 503, "schema": 200, "rate": 429}[kind]
        return httpx.Response(status, json={}, headers={"Retry-After": "0"})

    async with running_client(tmp_path, handler) as (client, storage):
        actor = QuotaActor("onebot:a", "A", "G")
        for i, error in enumerate(errors):
            with pytest.raises(error):
                await search(client, actor, f"Failure {i}")
            assert await counts(storage, actor) == (i + 1, i + 1, i + 1)
        assert len(calls) == client.status()["requests"] == len(errors)
        assert await storage.cache_count() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("user_limit", [1, 2])
async def test_retry_consumes_another_attempt_or_is_blocked_before_submission(tmp_path, user_limit):
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, json={}, headers={"Retry-After": "0"})
        return response()

    async with running_client(tmp_path, handler, retries=1, user_daily_limit=user_limit) as (client, storage):
        actor = QuotaActor("onebot:a", "A", "G")
        if user_limit == 1:
            with pytest.raises(QuotaExceededError) as denied:
                await search(client, actor, "Synthetic")
            assert_quota(denied.value, "user", 1, 1)
        else:
            assert (await search(client, actor, "Synthetic")).data[0].id == "001"
        assert len(calls) == user_limit
        assert await counts(storage, actor) == (user_limit,) * 3


@pytest.mark.asyncio
@pytest.mark.parametrize("user_limit", [1, 2])
async def test_player_two_http_calls_charge_twice_or_stop_between_endpoints(tmp_path, user_limit):
    paths = []
    def handler(request):
        paths.append(request.url.path)
        if request.url.path.endswith("search"):
            return response()
        return httpx.Response(200, json={"matchCount": 0})

    async with running_client(tmp_path, handler, user_daily_limit=user_limit) as (client, storage):
        actor = QuotaActor("onebot:a", "A", "G")
        with client.request_scope(actor):
            if user_limit == 1:
                with pytest.raises(QuotaExceededError) as denied:
                    await client.player("001", force=True)
                assert_quota(denied.value, "user", 1, 1)
                assert await storage.cache_count() == 0
            else:
                assert (await client.player("001", force=True)).data.id == "001"
                assert (await client.player("001")).data.id == "001"
        assert paths == ["/api/players/search", "/api/analysis/player"][:user_limit]
        assert await counts(storage, actor) == (user_limit,) * 3


@pytest.mark.asyncio
async def test_same_actor_coalesces_different_callers_charge_separately_and_share_cache(tmp_path):
    first_entered, all_entered, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []
    async def handler(request):
        calls.append(request)
        first_entered.set()
        if len(calls) == 3:
            all_entered.set()
        await release.wait()
        return response()

    async with running_client(tmp_path, handler, concurrency=3) as (client, storage):
        a = QuotaActor("onebot:a", "A", "G1")
        a_other_group = QuotaActor("onebot:a", "A", "G2")
        b = QuotaActor("onebot:a", "B", "G1")
        tasks = [asyncio.create_task(search(client, a, "Same"))]
        try:
            await asyncio.wait_for(first_entered.wait(), 2)
            tasks.extend([
                asyncio.create_task(search(client, a, "Same", force=True)),
                asyncio.create_task(search(client, a_other_group, "Same", force=True)),
                asyncio.create_task(search(client, b, "Same", force=True)),
            ])
            await asyncio.wait_for(all_entered.wait(), 2)
            assert len(calls) == 3
            release.set()
            results = await asyncio.gather(*tasks)
            assert all(r.data[0].id == "001" for r in results)
            assert await counts(storage, a) == (3, 2, 2)
            assert await counts(storage, a_other_group) == (3, 2, 1)
            assert await counts(storage, b) == (3, 1, 2)
            c = QuotaActor("onebot:a", "C", "G1")
            assert not (await search(client, c, "Same")).stale
            assert await counts(storage, c) == (3, 0, 2)
            assert len(calls) == 3
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_other_actor_force_cannot_join_owner_or_spend_owners_quota(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def handler(request):
        calls.append(request)
        if request.url.params["q"] == "Shared":
            entered.set()
            await release.wait()
        return response()

    async with running_client(tmp_path, handler, user_daily_limit=1) as (client, storage):
        owner = QuotaActor("onebot:a", "owner", "G")
        other = QuotaActor("onebot:a", "other", "G")
        await search(client, other, "Spend own limit")
        task = asyncio.create_task(search(client, owner, "Shared"))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            with pytest.raises(QuotaExceededError) as denied:
                await search(client, other, "Shared", force=True)
            assert_quota(denied.value, "user", 1, 1)
            assert len(calls) == 2
        finally:
            release.set()
            await task
        assert await counts(storage, owner) == (2, 1, 2)
        assert await counts(storage, other) == (2, 1, 2)


@pytest.mark.asyncio
async def test_cancelling_queued_costs_zero_submitted_attempt_remains_charged(tmp_path):
    entered, stopped = asyncio.Event(), asyncio.Event()
    calls = []
    async def handler(request):
        calls.append(request)
        if len(calls) == 1:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        return response()

    async with running_client(tmp_path, handler, concurrency=1) as (client, storage):
        active_actor = QuotaActor("onebot:a", "active", "G")
        queued_actor = QuotaActor("onebot:a", "queued", "G")
        active = asyncio.create_task(search(client, active_actor, "Active"))
        await asyncio.wait_for(entered.wait(), 2)
        queued = asyncio.create_task(search(client, queued_actor, "Queued"))
        async with asyncio.timeout(2):
            while client.status()["inflight"] < 2:
                await asyncio.sleep(0)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        assert await counts(storage, queued_actor) == (1, 0, 1)
        active.cancel()
        with pytest.raises(asyncio.CancelledError):
            await active
        assert stopped.is_set()
        assert await counts(storage, active_actor) == (1, 1, 1)
        await search(client, active_actor, "After cancellation", force=True)
        assert await counts(storage, active_actor) == (2, 2, 2)
        assert len(calls) == 2


@pytest.mark.asyncio
async def test_cancellation_after_durable_reservation_conservatively_keeps_count(tmp_path, monkeypatch):
    reserved = asyncio.Event()
    calls = []
    def handler(request):
        calls.append(request)
        return response()

    async with running_client(tmp_path, handler) as (client, storage):
        consume = storage.consume_request
        async def pause_after_commit(*args, **kwargs):
            await consume(*args, **kwargs)
            reserved.set()
            await asyncio.Event().wait()
        monkeypatch.setattr(storage, "consume_request", pause_after_commit)
        actor = QuotaActor("onebot:a", "A", "G")
        task = asyncio.create_task(search(client, actor, "Synthetic"))
        await asyncio.wait_for(reserved.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await counts(storage, actor) == (1, 1, 1)
        assert calls == []
        assert client.status()["inflight"] == 0


@pytest.mark.asyncio
async def test_clear_rotation_and_reopen_never_reset_any_daily_bucket(tmp_path):
    calls = []
    def handler(request):
        calls.append(request)
        return response()

    actor = QuotaActor("onebot:a", "A", "G")
    async with running_client(tmp_path, handler, user_daily_limit=2) as (first, storage):
        await search(first, actor, "Synthetic")
        await first.clear_cache()
        assert await counts(storage, actor) == (1, 1, 1)
    async with running_client(
        tmp_path, handler, user_daily_limit=2, client_id="rotated-synthetic", api_token="rotated-token",
    ) as (second, storage):
        await search(second, actor, "Synthetic", force=True)
        assert await counts(storage, actor) == (2, 2, 2)
    async with running_client(tmp_path, handler, user_daily_limit=2) as (third, storage):
        with pytest.raises(QuotaExceededError) as denied:
            await search(third, actor, "Synthetic", force=True)
        assert_quota(denied.value, "user", 2, 2)
        assert await counts(storage, actor) == (2, 2, 2)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_exhausted_actor_during_global_backoff_cannot_receive_stale(tmp_path, monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        if request.url.params["q"] == "Trigger backoff":
            return httpx.Response(429, json={}, headers={"Retry-After": "120"})
        return response()

    async with running_client(tmp_path, handler, user_daily_limit=1, wait_timeout=0.1) as (client, storage):
        exhausted = QuotaActor("onebot:a", "A", "G")
        other = QuotaActor("onebot:a", "B", "G")
        await search(client, exhausted, "Retained")
        expire_cache_reads(monkeypatch, storage)
        with pytest.raises(RateLimitError):
            await search(client, other, "Trigger backoff")
        with pytest.raises(QuotaExceededError) as denied:
            await search(client, exhausted, "Retained")
        assert_quota(denied.value, "user", 1, 1)
        assert await counts(storage, exhausted) == (2, 1, 2)
        assert len(calls) == 2
        assert client.status()["stale_hits"] == 0


def test_identified_requests_require_persistent_storage():
    client = BatraceClient(configuration())
    with pytest.raises(RuntimeError, match="持久化"):
        with client.request_scope(QuotaActor("onebot:a", "A")):
            pytest.fail("actor scope must not silently fall back to local counters")


@pytest.mark.asyncio
async def test_cache_write_deadline_rolls_back_and_next_request_recovers(tmp_path, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return response()

    async with running_client(tmp_path, handler, wait_timeout=0.1) as (client, storage):
        actor = QuotaActor("p", "u", "g")

        async def paused_prune(*args):
            await asyncio.Event().wait()

        with monkeypatch.context() as patch:
            patch.setattr(storage, "_prune", paused_prune)
            with pytest.raises(RequestTimeoutError):
                await search(client, actor, "Interrupted cache write")
        assert not storage._connection().in_transaction
        assert await storage.cache_count() == 0
        assert await counts(storage, actor) == (1, 1, 1)
        assert (await search(client, actor, "Recovery")).data[0].id == "001"
        assert await counts(storage, actor) == (2, 2, 2)
        assert len(calls) == 2


@pytest.mark.asyncio
async def test_delayed_quota_keeps_send_spacing_without_serializing_responses(tmp_path, monkeypatch):
    interval = 0.08
    reserving, release_quota = asyncio.Event(), asyncio.Event()
    both_started, release_responses = asyncio.Event(), asyncio.Event()
    starts = []
    loop = asyncio.get_running_loop()

    async def handler(request):
        starts.append(loop.time())
        if len(starts) == 2:
            both_started.set()
        await release_responses.wait()
        return response()

    async with running_client(tmp_path, handler, request_interval=interval, concurrency=2) as (client, storage):
        consume = storage.consume_request

        async def delayed_consume(*args, **kwargs):
            reserving.set()
            await release_quota.wait()
            return await consume(*args, **kwargs)

        monkeypatch.setattr(storage, "consume_request", delayed_consume)
        actor = QuotaActor("p", "u", "g")
        tasks = [asyncio.create_task(search(client, actor, query)) for query in ("First", "Second")]
        try:
            await asyncio.wait_for(reserving.wait(), 2)
            await asyncio.sleep(interval * 2)
            release_quota.set()
            # Both HTTP calls must start while the first response is still blocked.
            await asyncio.wait_for(both_started.wait(), 2)
            assert starts[1] - starts[0] >= interval - 0.005
            assert await counts(storage, actor) == (2, 2, 2)
        finally:
            release_quota.set()
            release_responses.set()
            await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_backoff_received_during_quota_wait_is_checked_before_sending(tmp_path, monkeypatch):
    first_started, reply_first = asyncio.Event(), asyncio.Event()
    second_reserving, release_quota = asyncio.Event(), asyncio.Event()
    starts = []
    loop = asyncio.get_running_loop()
    backoff_started = None

    async def handler(request):
        nonlocal backoff_started
        starts.append(loop.time())
        if request.url.params["q"] == "First":
            first_started.set()
            await reply_first.wait()
            backoff_started = loop.time()
            return httpx.Response(429, json={}, headers={"Retry-After": "0.15"})
        return response()

    async with running_client(tmp_path, handler, concurrency=2) as (client, storage):
        consume = storage.consume_request
        reservations = 0

        async def delayed_second(*args, **kwargs):
            nonlocal reservations
            reservations += 1
            if reservations == 2:
                second_reserving.set()
                await release_quota.wait()
            return await consume(*args, **kwargs)

        monkeypatch.setattr(storage, "consume_request", delayed_second)
        actor = QuotaActor("p", "u", "g")
        tasks = [asyncio.create_task(search(client, actor, "First"))]
        try:
            await asyncio.wait_for(first_started.wait(), 2)
            tasks.append(asyncio.create_task(search(client, actor, "Second")))
            await asyncio.wait_for(second_reserving.wait(), 2)
            reply_first.set()
            with pytest.raises(RateLimitError):
                await tasks[0]
            release_quota.set()
            await asyncio.wait_for(tasks[1], 2)
            assert len(starts) == 2
            assert starts[1] - backoff_started >= 0.145
            assert await counts(storage, actor) == (2, 2, 2)
        finally:
            reply_first.set()
            release_quota.set()
            await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_admission_backoff_crossing_midnight_cannot_send_101_new_day_attempts(tmp_path, monkeypatch):
    clock = [datetime.fromisoformat("2026-11-17T15:59:59+00:00").timestamp()]
    first_started, reply_first = asyncio.Event(), asyncio.Event()
    second_reserved, release_reservation = asyncio.Event(), asyncio.Event()
    days = []

    async def handler(request):
        days.append(quota_day(clock[0]))
        if request.url.params["q"] == "First":
            first_started.set()
            await reply_first.wait()
            return httpx.Response(429, json={}, headers={"Retry-After": "0.1"})
        return response()

    async with running_client(tmp_path, handler, user_daily_limit=0, group_daily_limit=0) as (client, storage):
        monkeypatch.setattr(storage, "_clock", lambda: clock[0])
        monkeypatch.setattr(client_module, "quota_day", lambda _: quota_day(clock[0]))
        consume = storage.consume_request
        reservations = 0

        async def pause_second_before_commit(*args, **kwargs):
            nonlocal reservations
            reservations += 1
            if reservations == 2:
                second_reserved.set()
                await release_reservation.wait()
            return await consume(*args, **kwargs)

        monkeypatch.setattr(storage, "consume_request", pause_second_before_commit)
        actor = QuotaActor("p", "u", "g")
        tasks = [asyncio.create_task(search(client, actor, "First", force=True))]
        try:
            await asyncio.wait_for(first_started.wait(), 2)
            tasks.append(asyncio.create_task(search(client, actor, "Second", force=True)))
            await asyncio.wait_for(second_reserved.wait(), 2)
            assert await counts(storage, actor) == (1, 1, 1)
            reply_first.set()
            with pytest.raises(RateLimitError):
                await tasks[0]
            release_reservation.set()
            await asyncio.sleep(0.02)
            clock[0] += 1  # Midnight while the new Retry-After is still active.
            await asyncio.wait_for(tasks[1], 2)
            assert await counts(storage, actor) == (1, 1, 1)
            clock[0] -= 1
            assert await counts(storage, actor) == (1, 1, 1)
            clock[0] += 1
            for index in range(99):
                await search(client, actor, f"New day {index}", force=True)
            with pytest.raises(QuotaExceededError) as denied:
                await search(client, actor, "New day 101st", force=True)
            assert_quota(denied.value, "global", 100, 100)
            assert days.count("2026-11-18") == 100
            assert await counts(storage, actor) == (100, 100, 100)
        finally:
            reply_first.set()
            release_reservation.set()
            await asyncio.gather(*tasks, return_exceptions=True)
