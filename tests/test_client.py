"""SYNTHETIC fixtures and httpx.MockTransport only; no live BATrace requests."""

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
import time

import httpx
import pytest

import client as client_module
from client import (
    AuthenticationError, BatraceClient, ClientClosedError, ClientConfig,
    IdentityMismatchError, NoDataError, NotConfiguredError, QueueFullError,
    RateLimitError, RequestTimeoutError, SchemaError, UpstreamError,
)
from storage import Storage
from quota import GLOBAL_DAILY_LIMIT, QuotaActor, QuotaExceededError


def config(**kwargs):
    defaults = dict(enabled=True, base_url="https://synthetic.example.invalid",
                    request_interval=0, retries=0,
                    request_timeout=0.2, wait_timeout=1)
    defaults.update(kwargs)
    return ClientConfig(**defaults)


def search_payload(id="001", name="Synthetic Player"):
    return {"players": [{"id": id, "name": name}]}


def match_payload(*, complete=True, ended=None):
    ended = time.time() - 1000 if ended is None else ended
    player = {"Id": "001", "Name": "Synthetic", "OldRating": 1000, "NewRating": 1010}
    if complete:
        player["UnitData"] = {}
    return {"matchInfo": {"StartTime": ended - 600, "EndTime": ended,
                          "TotalPlayTimeInSec": 600, "Data": {"a": player}}}


class MemoryStorage:
    """Small test double implementing the real retained-cache contract."""
    def __init__(self):
        self.entries = {}
        self.ttls = []
        self.requests = 0

    async def consume_request(self, actor=None, *, user_limit=20, group_limit=100, ready=None):
        assert actor is None  # Identified quota tests use real temporary SQLite.
        if ready is not None and not ready():
            from quota import AdmissionDeferred
            raise AdmissionDeferred
        if self.requests >= GLOBAL_DAILY_LIMIT:
            raise QuotaExceededError("global", GLOBAL_DAILY_LIMIT, self.requests)
        self.requests += 1

    async def quota_usage(self, actor=None):
        assert actor is None
        return {"global": self.requests, "user": None, "group": None}

    async def get_cache(self, key):
        return self.entries.get(key)

    async def put_cache(self, key, payload, ttl):
        now = time.time()
        self.entries[key] = (payload, now, now + ttl)
        self.ttls.append(ttl)

    async def clear_cache(self):
        self.entries.clear()

    async def prune_cache(self, max_entries=1000, max_age_seconds=604800):
        keys = sorted(self.entries, key=lambda k: self.entries[k][1], reverse=True)
        for key in keys[max_entries:]:
            self.entries.pop(key)
        for key, (_, fetched, _) in list(self.entries.items()):
            if time.time() - fetched >= max_age_seconds:
                self.entries.pop(key)

    async def cache_count(self):
        return len(self.entries)

    def expire(self, age=700):
        now = time.time()
        for key, (payload, _, _) in list(self.entries.items()):
            self.entries[key] = (payload, now - age, now - 1)


@pytest.mark.asyncio
async def test_disabled_access_never_touches_http_or_cache():
    calls = []
    def handler(request):
        calls.append(request)
        raise AssertionError("disabled path reached transport")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = BatraceClient(http_client=http)
        for operation in (lambda: client.search("Synthetic"), lambda: client.player("001"),
                          lambda: client.recent("001"), lambda: client.match("42")):
            with pytest.raises(NotConfiguredError):
                await operation()
        await client.close()
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"base_url": "http://synthetic.invalid"}, {"base_url": "https://name:secret@synthetic.invalid"},
    {"base_url": "https://synthetic.invalid/?secret=1"}, {"base_url": "https://synthetic.invalid/#fragment"},
    {"base_url": "https://synthetic.invalid/api"}, {"base_url": "https://synthetic.invalid:bad"},
    {"base_url": "https://synthetic.invalid:0"}, {"base_url": "https://synthetic.invalid:65536"},
    {"base_url": "https://synthetic.in\tvalid"}, {"base_url": "https://synthetic.invalid\\other"},
    {"base_url": "https://@synthetic.invalid"},
    {"base_url": ""},
])
async def test_invalid_access_fails_closed(changes):
    client = BatraceClient(config(**changes))
    with pytest.raises(NotConfiguredError):
        await client.search("Synthetic")
    assert client.status()["requests"] == 0
    await client.close()


def test_binding_and_cache_namespaces_track_source():
    a = BatraceClient(config())
    b = BatraceClient(config(base_url="https://synthetic.example.invalid/"))
    c = BatraceClient(config(base_url="https://different.example.invalid"))
    assert a.namespace == b.namespace != c.namespace
    assert a._namespace == b._namespace != c._namespace
    status = str(a.status())
    assert "synthetic.example" not in status


@pytest.mark.asyncio
@pytest.mark.parametrize("base_url", ["https://synthetic.example.invalid", "https://different.example.invalid"])
async def test_paths_query_encoding_and_plugin_identity(base_url):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=search_payload())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = BatraceClient(config(base_url=base_url), http_client=http)
        result = await client.search('Player 2 & "中文"')
        assert result.data[0].id == "001"
        assert result.fetched_at.tzinfo == timezone.utc
        assert not result.stale
        request = requests[0]
        assert request.url.path == "/api/players/search"
        assert dict(request.url.params) == {"q": 'Player 2 & "中文"', "limit": "20"}
        assert request.headers["X-Client"] == "Astrbot_BA_Stats"
        assert request.headers["User-Agent"] == "astrbot-plugin-ba-stats"
        assert "Authorization" not in request.headers
        assert request.extensions["timeout"]["read"] == 0.2
        await client.close()
        assert not http.is_closed


@pytest.mark.asyncio
async def test_player_echo_verification_and_latest_elo():
    requests = []
    pid = "00098765432109876543210987654321"
    def handler(request):
        requests.append(request)
        if request.url.path.endswith("search"):
            return httpx.Response(200, json=search_payload(pid))
        assert request.url.path == "/api/analysis/player"
        assert request.url.params["stbid"] == pid
        return httpx.Response(200, json={"trend": {"points": [
            {"ratingAfter": 1100, "endTime": 1700000600},
            {"ratingAfter": 900, "endTime": 1700000000},
        ]}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = BatraceClient(config(), http_client=http)
        result = await client.player(pid)
        assert result.data.id == pid and result.data.elo == 1100
        assert result.data.steam_id is None
        assert len(requests) == 2
        await client.close()


@pytest.mark.asyncio
async def test_identity_mismatch_is_not_a_fallback_or_a_new_binding():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=search_payload("somebody-else"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = BatraceClient(config(), http_client=http)
        with pytest.raises(IdentityMismatchError):
            await client.player("001")
        assert len(calls) == 1  # never continue analysis with the wrong identity
        await client.close()


@pytest.mark.asyncio
async def test_recent_identity_limit_and_actual_request_parameters():
    seen = []
    def handler(request):
        seen.append(dict(request.url.params))
        return httpx.Response(200, json={"matches": [
            {"matchId": "42", "data": match_payload()["matchInfo"]},
        ]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = BatraceClient(config(), http_client=http)
        assert (await client.recent("001", 7)).data[0].id == "42"
        assert seen[0] == {"stbid": "001", "limit": "7"}
        with pytest.raises(IdentityMismatchError):
            await client.recent("002", 7)
        for limit in (0, 21, True, 1.5):
            with pytest.raises(ValueError):
                await client.recent("001", limit)
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("player_id", [None, "999"])
async def test_recent_empty_roster_is_unknown_but_foreign_roster_cannot_use_old_cache(player_id):
    store = MemoryStorage()
    payload = {"matches": [{"matchId": "42", "data": match_payload()["matchInfo"]}]}
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=payload)
    )) as http:
        client = BatraceClient(config(), storage=store, http_client=http)
        try:
            await client.recent("001")
            store.expire()
            payload = {"matches": [{"matchId": "42", "data": {
                "Data": {} if player_id is None else {"a": {"Id": player_id}}
            }}]}
            if player_id is None:
                result = await client.recent("001")
                assert result.data[0].players == []
                assert not result.stale
            else:
                with pytest.raises(IdentityMismatchError, match="未参与") as error:
                    await client.recent("001")
                assert error.value.code == "identity_mismatch"
                assert client.status()["stale_hits"] == 0
        finally:
            await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status,error", [
    (401, AuthenticationError), (403, AuthenticationError), (404, NoDataError),
    (429, RateLimitError), (500, UpstreamError), (503, UpstreamError),
])
async def test_http_failures_remain_distinct_without_unlimited_retries(status, error):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"error": "SYNTHETIC secret, never display"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = BatraceClient(config(), http_client=http)
        with pytest.raises(error) as caught:
            await client.search("Synthetic")
        assert "secret" not in str(caught.value)
        assert len(calls) == 1
        assert client.status()["inflight"] == 0
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status,body,content_type,error", [
    (200, "{}", "application/json", SchemaError),
    (200, "not json", "application/json", SchemaError),
    (404, "<h1>missing page</h1>", "text/html", SchemaError),
    (200, "Security Verification", "text/html", AuthenticationError),
    (503, "EO-Bot-Captcha-Token", "text/html", AuthenticationError),
    (200, "<h1>maintenance</h1>", "text/html", SchemaError),
])
async def test_schema_changes_and_captcha_never_become_empty_results(status, body, content_type, error):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, text=body, headers={"Content-Type": content_type})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = BatraceClient(config(retries=1), http_client=http)
        with pytest.raises(error):
            await client.search("Synthetic")
        assert len(calls) == 1
        await client.close()


@pytest.mark.asyncio
async def test_retry_after_zero_retries_once_and_recovers():
    calls = 0
    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, json={}, headers={"Retry-After": "0"})
        return httpx.Response(200, json=search_payload())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = BatraceClient(config(retries=1), http_client=http)
        assert (await client.search("Synthetic")).data[0].id == "001"
        assert calls == 2
        await client.close()


@pytest.mark.asyncio
async def test_exhausted_retries_stop_after_configured_attempt_count():
    calls = 0
    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(429, json={}, headers={"Retry-After": "0"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = BatraceClient(config(retries=1), http_client=http)
        with pytest.raises(RateLimitError):
            await client.search("Synthetic")
        assert calls == 2
        await client.close()


@pytest.mark.asyncio
async def test_long_retry_after_is_preserved_across_next_query():
    calls = 0
    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(429, json={}, headers={"Retry-After": "120"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = BatraceClient(config(retries=1, wait_timeout=0.03), http_client=http)
        with pytest.raises(RateLimitError) as caught:
            await client.search("Synthetic A")
        assert caught.value.retry_after == 120
        with pytest.raises(RequestTimeoutError):
            await client.search("Synthetic B")
        assert calls == 1
        await client.close()


@pytest.mark.asyncio
async def test_retry_wait_is_part_of_overall_deadline():
    calls = 0
    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(503, json={})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = BatraceClient(config(retries=1, wait_timeout=0.03), http_client=http)
        start = time.monotonic()
        with pytest.raises(RequestTimeoutError):
            await client.search("Synthetic")
        assert time.monotonic() - start < 0.5 and calls == 1
        await client.close()


@pytest.mark.asyncio
async def test_cache_hit_force_refresh_and_new_client_persistence(tmp_path):
    calls = 0
    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=search_payload(name=f"Synthetic {calls}"))
    store = Storage(tmp_path / "data")
    await store.open()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        first = BatraceClient(config(), storage=store, http_client=http)
        original = await first.search("Synthetic")
        cached = await first.search("Synthetic")
        assert calls == 1 and cached.data == original.data
        assert not cached.stale
        await first.close()
        await store.close()
        store = Storage(tmp_path / "data")
        await store.open()
        second = BatraceClient(config(), storage=store, http_client=http)
        assert (await second.search("Synthetic")).data[0].name == "Synthetic 1"
        assert calls == 1
        assert (await second.search("Synthetic", force=True)).data[0].name == "Synthetic 2"
        assert calls == 2
        await second.close()
    await store.close()


@pytest.mark.asyncio
async def test_stale_fallback_preserves_original_fetch_time_and_marks_reason():
    fail = False
    def handler(request):
        return httpx.Response(503, json={}) if fail else httpx.Response(200, json=search_payload())
    store = MemoryStorage()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = BatraceClient(config(), storage=store, http_client=http)
        await client.search("Synthetic")
        store.expire()
        fetched = next(iter(store.entries.values()))[1]
        fail = True
        result = await client.search("Synthetic")
        assert result.stale and result.stale_reason == "upstream"
        assert result.fetched_at.timestamp() == pytest.approx(fetched, abs=0.000001, rel=0)
        assert client.status()["stale_hits"] == 1
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status,payload,error", [
    (401, {}, AuthenticationError), (404, {}, NoDataError), (200, {}, SchemaError),
])
async def test_nontransient_failures_never_hide_behind_stale_cache(status, payload, error):
    fail = False
    def handler(request):
        return httpx.Response(status, json=payload) if fail else httpx.Response(200, json=search_payload())
    store = MemoryStorage()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = BatraceClient(config(), storage=store, http_client=http)
        await client.search("Synthetic")
        store.expire()
        fail = True
        with pytest.raises(error):
            await client.search("Synthetic")
        await client.close()


@pytest.mark.asyncio
async def test_stale_retention_limit_is_enforced():
    fail = False
    def handler(request):
        if fail:
            raise httpx.ConnectError("SYNTHETIC", request=request)
        return httpx.Response(200, json=search_payload())
    store = MemoryStorage()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = BatraceClient(config(stale_hours=1), storage=store, http_client=http)
        await client.search("Synthetic")
        store.expire(age=3601)
        fail = True
        with pytest.raises(UpstreamError):
            await client.search("Synthetic")
        await client.close()


@pytest.mark.asyncio
async def test_identical_concurrent_misses_make_one_request():
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0
    async def handler(request):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return httpx.Response(200, json=search_payload())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = BatraceClient(config(queue_size=0, concurrency=1), http_client=http)
        tasks = [asyncio.create_task(client.search("Synthetic")) for _ in range(20)]
        await entered.wait()
        assert client.status()["inflight"] == 1 and calls == 1
        release.set()
        results = await asyncio.gather(*tasks)
        assert all(r.data[0].id == "001" for r in results)
        assert client.status()["inflight"] == 0
        await client.close()


@pytest.mark.asyncio
async def test_queue_is_bounded_but_existing_request_can_join():
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0
    async def handler(request):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return httpx.Response(200, json=search_payload())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = BatraceClient(config(concurrency=1, queue_size=1), http_client=http)
        a = asyncio.create_task(client.search("A"))
        await entered.wait()
        b = asyncio.create_task(client.search("B"))
        await asyncio.sleep(0)
        with pytest.raises(QueueFullError):
            await client.search("C")
        joined = asyncio.create_task(client.search("A"))
        release.set()
        await asyncio.gather(a, b, joined)
        assert calls == 2
        await client.close()


@pytest.mark.asyncio
async def test_global_request_spacing_and_max_concurrency():
    starts, active, peak = [], 0, 0
    async def handler(request):
        nonlocal active, peak
        starts.append(time.monotonic())
        active += 1
        peak = max(active, peak)
        await asyncio.sleep(0.2)
        active -= 1
        return httpx.Response(200, json=search_payload())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = BatraceClient(config(concurrency=2, request_interval=0.08), http_client=http)
        await asyncio.gather(*(client.search(str(i)) for i in range(4)))
        assert peak == 2
        assert all(b - a >= 0.07 for a, b in zip(starts, starts[1:]))
        await client.close()


@pytest.mark.asyncio
async def test_queue_wait_and_transport_wait_share_one_deadline():
    calls = 0
    async def handler(request):
        nonlocal calls
        calls += 1
        await asyncio.Event().wait()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = BatraceClient(config(concurrency=1, wait_timeout=0.03), http_client=http)
        results = await asyncio.gather(client.search("A"), client.search("B"), return_exceptions=True)
        assert all(isinstance(r, RequestTimeoutError) for r in results)
        assert calls == 1
        assert client.status()["inflight"] == 0
        await client.close()


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_cancel_another_joined_waiter():
    entered, release = asyncio.Event(), asyncio.Event()
    async def handler(request):
        entered.set()
        await release.wait()
        return httpx.Response(200, json=search_payload())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = BatraceClient(config(), http_client=http)
        a = asyncio.create_task(client.search("Synthetic"))
        await entered.wait()
        b = asyncio.create_task(client.search("Synthetic"))
        await asyncio.sleep(0)
        a.cancel()
        with pytest.raises(asyncio.CancelledError):
            await a
        release.set()
        assert (await b).data[0].id == "001"
        await client.close()


@pytest.mark.asyncio
async def test_last_waiter_cancel_releases_request_and_allows_recovery():
    entered, stopped = asyncio.Event(), asyncio.Event()
    calls = 0
    async def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        return httpx.Response(200, json=search_payload())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = BatraceClient(config(), http_client=http)
        task = asyncio.create_task(client.search("Synthetic"))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stopped.is_set() and client.status()["inflight"] == 0
        assert (await client.search("Synthetic")).data[0].id == "001"
        await client.close()


@pytest.mark.asyncio
async def test_close_cancels_and_awaits_active_and_queued_work():
    entered, stopped = asyncio.Event(), asyncio.Event()
    async def handler(request):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = BatraceClient(config(concurrency=1), http_client=http)
        a = asyncio.create_task(client.search("A"))
        await entered.wait()
        b = asyncio.create_task(client.search("B"))
        await asyncio.sleep(0)
        await client.close()
        results = await asyncio.gather(a, b, return_exceptions=True)
        assert all(isinstance(r, ClientClosedError) for r in results)
        assert stopped.is_set() and client.status()["inflight"] == 0
        with pytest.raises(ClientClosedError):
            await client.search("C")
        await client.close()
        assert not http.is_closed


@pytest.mark.asyncio
async def test_owned_http_client_is_closed(monkeypatch):
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=search_payload())))
    monkeypatch.setattr(client_module.httpx, "AsyncClient", lambda: http)
    client = BatraceClient(config())
    await client.search("Synthetic")
    await client.close()
    assert http.is_closed


@pytest.mark.asyncio
async def test_cache_capacity_and_clear_during_refresh():
    entered, release = asyncio.Event(), asyncio.Event()
    async def handler(request):
        if request.url.params["q"] == "slow":
            entered.set()
            await release.wait()
        return httpx.Response(200, json=search_payload())
    store = MemoryStorage()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = BatraceClient(config(cache_max_entries=2), storage=store, http_client=http)
        for q in ("A", "B", "C"):
            await client.search(q)
        assert await store.cache_count() == 2
        task = asyncio.create_task(client.search("slow"))
        await entered.wait()
        await client.clear_cache()
        release.set()
        await task
        assert await store.cache_count() == 0
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("complete,ended,ttl", [
    (True, None, 86400), (False, None, 60), (True, "now", 60),
])
async def test_complete_old_match_cache_is_long_but_fresh_or_partial_is_short(complete, ended, ttl):
    payload = match_payload(complete=complete, ended=(time.time() if ended == "now" else ended))
    def handler(request):
        assert request.url.path == "/api/match" and request.url.params["matchid"] == "42"
        return httpx.Response(200, json=payload)
    store = MemoryStorage()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = BatraceClient(config(), storage=store, http_client=http)
        assert (await client.match("42")).data.id == "42"
        assert store.ttls[-1] == ttl
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_start,ended,ttl", [
    (True, 1700001200, 86400), (True, "now", 60), (False, "now", 60),
])
async def test_match_cache_age_uses_observed_end_not_start_plus_duration(missing_start, ended, ttl):
    payload = match_payload(ended=time.time() if ended == "now" else ended)
    if missing_start:
        payload["matchInfo"].pop("StartTime")
    else:
        payload["matchInfo"]["TotalPlayTimeInSec"] = 1  # Inference would incorrectly age this by 599s.
    store = MemoryStorage()
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json=payload)
    )) as http:
        client = BatraceClient(config(), storage=store, http_client=http)
        result = await client.match("42")
        assert result.data.ended_at.timestamp() == pytest.approx(payload["matchInfo"]["EndTime"], abs=1e-6, rel=0)
        if missing_start:
            assert result.data.started_at is None
        assert store.ttls[-1] == ttl
        key, (raw, fetched, expires) = next(iter(store.entries.items()))
        # Avoid sub-microsecond clock rounding making a just-written fetch look future-dated.
        store.entries[key] = (json.loads(json.dumps(raw, allow_nan=False)), fetched - 1, expires)
        assert (await client.match("42")).data == result.data  # Raw cache reparse retains the timestamps.
        assert client.status()["requests"] == 1
        await client.close()


@pytest.mark.asyncio
async def test_failed_request_does_not_leave_permanent_inflight_and_can_recover():
    calls = 0
    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("SYNTHETIC", request=request)
        return httpx.Response(200, json=search_payload())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = BatraceClient(config(), http_client=http)
        with pytest.raises(RequestTimeoutError):
            await client.search("Synthetic")
        assert (await client.search("Synthetic")).data[0].id == "001"
        await client.close()


@pytest.mark.asyncio
async def test_force_joining_fresh_cache_lookup_promotes_it_to_real_revalidation():
    class DelayedCache(MemoryStorage):
        delay = False
        entered = asyncio.Event()
        release = asyncio.Event()

        async def get_cache(self, key):
            if self.delay:
                self.entered.set()
                await self.release.wait()
            return await super().get_cache(key)

    calls = 0
    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=search_payload(name=f"Synthetic {calls}"))
    store = DelayedCache()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = BatraceClient(config(), storage=store, http_client=http)
        await client.search("Synthetic")
        store.delay = True
        normal = asyncio.create_task(client.search("Synthetic"))
        await store.entered.wait()
        forced = asyncio.create_task(client.search("Synthetic", force=True))
        await asyncio.sleep(0)
        store.release.set()
        results = await asyncio.gather(normal, forced)
        assert calls == 2  # exactly one initial request plus one promoted refresh
        assert all(r.data[0].name == "Synthetic 2" and not r.stale for r in results)
        await client.close()


@pytest.mark.asyncio
async def test_force_joining_active_upstream_still_makes_one_request():
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0
    async def handler(request):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return httpx.Response(200, json=search_payload())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = BatraceClient(config(), http_client=http)
        normal = asyncio.create_task(client.search("Synthetic"))
        await entered.wait()
        forced = asyncio.create_task(client.search("Synthetic", force=True))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(normal, forced)
        assert calls == 1
        await client.close()
