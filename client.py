"""Independent BATrace adapter with bounded scheduling and persistent caching.

Creating a client never probes the source. All network methods require explicit
access enablement and this plugin's own operator-approved client identity.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import math
import time
from typing import Any, Awaitable, Callable, Protocol, TypeVar
from urllib.parse import urlsplit

import httpx

if __package__:
    from .quota import AdmissionDeferred, GLOBAL_DAILY_LIMIT, QuotaActor, QuotaExceededError, quota_day
    from .models import (
        FetchResult, Match, Participant, Player, canonical_id, parse_match, parse_player,
        parse_recent, parse_search, utc_datetime,
    )
else:  # Direct module imports for framework-independent tests.
    from quota import AdmissionDeferred, GLOBAL_DAILY_LIMIT, QuotaActor, QuotaExceededError, quota_day
    from models import (
        FetchResult, Match, Participant, Player, canonical_id, parse_match, parse_player,
        parse_recent, parse_search, utc_datetime,
    )

T = TypeVar("T")


class CacheStorage(Protocol):
    async def get_cache(self, key: str) -> tuple[Any, Any, Any] | None: ...
    async def put_cache(self, key: str, payload: Any, ttl: float) -> None: ...
    async def clear_cache(self) -> None: ...
    async def prune_cache(self, max_entries: int = 1000, max_age_seconds: float = 604800) -> Any: ...
    async def cache_count(self) -> int: ...
    async def consume_request(self, actor: QuotaActor | None = None, *, user_limit: int = 20,
                              group_limit: int = 100, ready: Callable[[], bool] | None = None) -> dict: ...
    async def quota_usage(self, actor: QuotaActor | None = None) -> dict: ...


@dataclass(frozen=True)
class ClientConfig:
    enabled: bool = False
    base_url: str = "https://bot.batrace.top"
    client_id: str = ""
    api_token: str = field(default="", repr=False)
    request_interval: float = 1.0
    concurrency: int = 2
    queue_size: int = 20
    request_timeout: float = 20.0
    wait_timeout: float = 45.0
    retries: int = 1
    cache_max_entries: int = 1000
    stale_hours: float = 24.0
    user_daily_limit: int = 20
    group_daily_limit: int = 100


class ClientError(Exception):
    code = "client_error"


class NotConfiguredError(ClientError):
    code = "not_configured"


class AuthenticationError(ClientError):
    code = "authentication"


class RateLimitError(ClientError):
    code = "rate_limited"

    def __init__(self, message: str = "数据源请求受限，请稍后重试。", *, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class RequestTimeoutError(ClientError):
    code = "timeout"


class UpstreamError(ClientError):
    code = "upstream"


class NoDataError(ClientError):
    code = "no_data"


class SchemaError(ClientError):
    code = "schema"


class IdentityMismatchError(ClientError):
    code = "identity_mismatch"


def participant_for(match: Match, player_id: str) -> Participant | None:
    """Empty rosters are unknown; a nonempty roster must contain the target."""
    person = next((p for p in match.players if p.player_id == player_id), None)
    if match.players and person is None:
        raise IdentityMismatchError("数据源返回了目标玩家未参与的对局，已中止本次查询。")
    return person


class QueueFullError(ClientError):
    code = "queue_full"


class ClientClosedError(ClientError):
    code = "closed"


@dataclass
class _Flight:
    task: asyncio.Task
    waiters: int = 0
    force: bool = False


class BatraceClient:
    """One shared instance per plugin; injected HTTP client remains caller-owned.

    search(query), player(id), recent(id, limit), match(id) return FetchResult.
    ``force=True`` bypasses fresh cache but keeps the same admission/rate limits.
    close() cancels and awaits owned jobs; it closes only an internally-created
    AsyncClient. The caller closes an injected AsyncClient and Storage itself.
    """

    def __init__(
        self, config: ClientConfig | None = None, *,
        storage: CacheStorage | None = None, http_client: httpx.AsyncClient | None = None,
    ):
        self.config = config or ClientConfig()
        c = self.config
        if any(not math.isfinite(x) or x < 0 for x in (c.request_interval, c.stale_hours)):
            raise ValueError("interval and stale age must be finite and nonnegative")
        if any(not math.isfinite(x) or x <= 0 for x in (c.request_timeout, c.wait_timeout)):
            raise ValueError("timeouts must be finite and positive")
        if any(isinstance(x, bool) or not isinstance(x, int) for x in (
            c.concurrency, c.queue_size, c.retries, c.cache_max_entries,
        )) or not (1 <= c.concurrency <= 8 and 0 <= c.queue_size <= 1000
                   and 0 <= c.retries <= 2 and c.cache_max_entries > 0):
            raise ValueError("invalid scheduler/cache bounds")
        if any(type(x) is not int or not 0 <= x <= GLOBAL_DAILY_LIMIT
               for x in (c.user_daily_limit, c.group_daily_limit)):
            raise ValueError("user and group daily limits must be integers between 0 and 100")
        self._storage = storage
        self._http = http_client
        self._owns_http = http_client is None
        self._closed = False
        self._slots = asyncio.Semaphore(c.concurrency)
        self._start_lock = asyncio.Lock()
        self._cache_lock = asyncio.Lock()
        self._last_start: float | None = None
        self._not_before = 0.0
        self._flights: dict[tuple[str, QuotaActor | None], _Flight] = {}
        self._actor: ContextVar[QuotaActor | None] = ContextVar("ba_stats_actor", default=None)
        # Standalone adapter tests may omit Storage. The plugin always supplies
        # SQLite; identified requests require it so user/group limits persist.
        self._local_quota_day = ""
        self._local_quota_used = 0
        self._generation = 0
        self._requests = 0
        self._cache_hits = 0
        self._stale_hits = 0
        self._last_error: str | None = None
        # Includes origin AND identity, so a different deployment/credential can
        # never accidentally read another source's cached identity response.
        source = json.dumps([c.base_url.rstrip("/"), c.client_id, c.api_token])
        self._namespace = "batrace:v1:" + hashlib.sha256(source.encode()).hexdigest()[:24]

    @property
    def namespace(self) -> str:
        """Stable binding identity: credential rotation must not change it."""
        origin = self.config.base_url.rstrip("/")
        return "batrace:" + hashlib.sha256(origin.encode()).hexdigest()[:16]

    @property
    def configured(self) -> bool:
        c = self.config
        if not c.enabled or not c.client_id.strip() or "ba-dragon" in c.client_id.casefold():
            return False
        if any(ch in c.client_id + c.api_token for ch in "\r\n"):
            return False
        try:
            u = urlsplit(c.base_url)
            _ = u.port
            return bool(u.scheme == "https" and u.hostname and not u.username and not u.password
                        and not u.query and not u.fragment and u.path in {"", "/"})
        except ValueError:
            return False

    def _check_ready(self) -> None:
        if self._closed:
            raise ClientClosedError("数据源客户端已关闭，请稍后重试。")
        if not self.configured:
            raise NotConfiguredError("数据源接入未配置：需开启在线查询并填写本插件独立的获准客户端标识。")

    def status(self) -> dict[str, Any]:
        """Safe admin diagnostics: never return token, header, query or URL."""
        return {
            "configured": self.configured, "closed": self._closed,
            "inflight": len(self._flights), "requests": self._requests,
            "cache_hits": self._cache_hits, "stale_hits": self._stale_hits,
            "last_error": self._last_error,
        }

    @contextmanager
    def request_scope(self, actor: QuotaActor):
        """Carry the trusted event identity into all child requests/retries."""
        if self._storage is None:
            raise RuntimeError("用户与群额度需要持久化存储。")
        token = self._actor.set(actor)
        try:
            yield
        finally:
            self._actor.reset(token)

    def _local_usage(self) -> int:
        day = quota_day(time.time())
        if self._local_quota_day != day:
            self._local_quota_day, self._local_quota_used = day, 0
        return self._local_quota_used

    async def _check_daily_quota(self) -> None:
        """Read-only preflight; reservation at send time remains authoritative."""
        usage = (await self._storage.quota_usage(self._actor.get()) if self._storage is not None
                 else {"global": self._local_usage(), "user": None, "group": None})
        for scope, limit in (("global", GLOBAL_DAILY_LIMIT), ("user", self.config.user_daily_limit),
                             ("group", self.config.group_daily_limit)):
            if usage[scope] is not None and limit and usage[scope] >= limit:
                raise QuotaExceededError(scope, limit, usage[scope])

    async def _consume_request(self) -> None:
        if self._storage is not None:
            await self._storage.consume_request(
                self._actor.get(), user_limit=self.config.user_daily_limit,
                group_limit=self.config.group_daily_limit,
                ready=self._send_ready,
            )
            return
        if self._local_usage() >= GLOBAL_DAILY_LIMIT:
            raise QuotaExceededError("global", GLOBAL_DAILY_LIMIT, self._local_quota_used)
        self._local_quota_used += 1

    async def search(self, query: str, *, force: bool = False) -> FetchResult[list[Player]]:
        if not isinstance(query, str) or not query.strip() or len(query) > 200:
            raise ValueError("query must contain 1–200 characters")
        async def load() -> Any:
            return await self._request("/api/players/search", {"q": query, "limit": 20})
        return await self._get("search", [query, 20], load, parse_search, 600, force)

    async def player(self, id: str, *, force: bool = False) -> FetchResult[Player]:
        pid = canonical_id(id)
        async def load() -> Any:
            search = await self._request("/api/players/search", {"q": pid, "limit": 20})
            try:
                candidates = parse_search(search)
            except (ValueError, TypeError, OverflowError) as exc:
                raise SchemaError("玩家搜索返回的数据结构发生变化，暂无法解析。") from exc
            if not any(p.id == pid for p in candidates):
                raise IdentityMismatchError("数据源未返回对应的规范玩家 ID，无法核实身份。")
            analysis = await self._request("/api/analysis/player", {"stbid": pid})
            return {"search": search, "analysis": analysis}
        return await self._get("player", [pid], load, lambda p: parse_player(p, pid), 900, force)

    async def recent(self, id: str, limit: int = 5, *, force: bool = False) -> FetchResult[list[Match]]:
        pid = canonical_id(id)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
            raise ValueError("recent limit must be between 1 and 20")
        async def load() -> Any:
            return await self._request("/api/players/matches", {"stbid": pid, "limit": limit})
        def decode(raw: Any) -> list[Match]:
            matches = parse_recent(raw)
            for match in matches:
                participant_for(match, pid)
            return matches[:limit]
        return await self._get("recent", [pid, limit], load, decode, 600, force)

    async def match(self, id: str, *, force: bool = False) -> FetchResult[Match]:
        mid = canonical_id(id)
        async def load() -> Any:
            return await self._request("/api/match", {"matchid": mid})
        return await self._get("match", [mid], load, lambda p: parse_match(p, mid), 86400, force)

    async def _get(
        self, operation: str, parameters: list[Any], loader: Callable[[], Awaitable[Any]],
        decoder: Callable[[Any], T], ttl: float, force: bool,
    ) -> FetchResult[T]:
        self._check_ready()
        digest = hashlib.sha256(json.dumps(parameters, ensure_ascii=False).encode()).hexdigest()
        key = f"{self._namespace}:{operation}:{digest}"
        # Keep cache shared, but only coalesce network jobs for the same sender
        # and group. Another caller must not spend or bypass the owner's quota.
        flight_key = (key, self._actor.get())
        # force joins an already running refresh; no duplicate bypass lane.
        flight = self._flights.get(flight_key)
        if flight is not None and flight.task.done():
            self._flights.pop(flight_key, None)
            flight = None
        if flight is None:
            if len(self._flights) >= self.config.concurrency + self.config.queue_size:
                self._last_error = QueueFullError.code
                raise QueueFullError("当前请求排队已满，请稍后重试。")
            task = asyncio.create_task(self._load(key, loader, decoder, ttl, lambda: flight.force))
            flight = _Flight(task, force=force)
            self._flights[flight_key] = flight
            def finished(t: asyncio.Task) -> None:
                if self._flights.get(flight_key) is flight:
                    self._flights.pop(flight_key, None)
                if not t.cancelled():
                    t.exception()  # retrieve errors even if every waiter was cancelled
            task.add_done_callback(finished)
        elif force:
            # A bind revalidation joining a cache lookup must promote the shared
            # job before it decides whether a fresh cache hit is acceptable.
            flight.force = True
        flight.waiters += 1
        try:
            return await asyncio.shield(flight.task)
        except asyncio.CancelledError:
            if self._closed:
                raise ClientClosedError("数据源客户端已关闭，请稍后重试。") from None
            raise
        finally:
            flight.waiters -= 1
            if not flight.waiters and not flight.task.done():
                flight.task.cancel()
                await asyncio.gather(flight.task, return_exceptions=True)

    @staticmethod
    def _decode(decoder: Callable[[Any], T], payload: Any) -> T:
        try:
            return decoder(payload)
        except ClientError:
            raise
        except (ValueError, TypeError, OverflowError, OSError, KeyError) as exc:
            raise SchemaError("数据源返回的数据结构发生变化，暂无法解析。") from exc

    async def _load(
        self, key: str, loader: Callable[[], Awaitable[Any]], decoder: Callable[[Any], T],
        ttl: float, force: Callable[[], bool],
    ) -> FetchResult[T]:
        cached: tuple[T, datetime, datetime] | None = None
        generation = self._generation
        try:
            async with asyncio.timeout(self.config.wait_timeout):
                if self._storage is not None:
                    hit = await self._storage.get_cache(key)
                    if hit is not None:
                        try:
                            payload, fetched, expires = hit
                            fetched, expires = utc_datetime(fetched), utc_datetime(expires)
                            if fetched is None or expires is None or fetched > expires:
                                raise ValueError("invalid cache timestamps")
                            cached = (self._decode(decoder, payload), fetched, expires)
                        except (ClientError, ValueError, TypeError, OverflowError, OSError):
                            cached = None  # old/incompatible cache must be refetched
                    now = datetime.now(timezone.utc)
                    if cached and not force() and cached[2] > now and cached[1] <= now:
                        self._cache_hits += 1
                        return self._result(cached[0], cached[1])
                await self._check_daily_quota()
                async with self._slots:
                    self._check_ready()
                    raw = await loader()
                    data = self._decode(decoder, raw)
                fetched_at = datetime.now(timezone.utc)
                if isinstance(data, Match):
                    # A structural complete flag is insufficient for a just-ended
                    # match: keep it short until at least five minutes after end.
                    end = None
                    if data.started_at is not None and data.duration_seconds is not None:
                        end = data.started_at.timestamp() + data.duration_seconds
                    if not data.complete or end is None or time.time() - end < 300:
                        ttl = 60
                if self._storage is not None:
                    async with self._cache_lock:
                        if generation == self._generation:
                            await self._storage.put_cache(key, raw, ttl)
                            # Enforce the configured bound at every insertion;
                            # rely on Storage's constructor bound as well.
                            await self._storage.prune_cache(
                                max_entries=self.config.cache_max_entries, max_age_seconds=604800,
                            )
                self._last_error = None
                return self._result(data, fetched_at)
        except TimeoutError:
            error: ClientError = RequestTimeoutError("本次查询超过总等待时间，请稍后重试。")
        except (ClientError, QuotaExceededError) as exc:
            error = exc
        self._last_error = error.code
        # No fallback for identity, authorization, not-found or schema failures.
        if isinstance(error, (RequestTimeoutError, UpstreamError, RateLimitError)) and cached:
            # Quota can become exhausted while this job waits/backoffs. Do not
            # disguise that local limit as a temporary-source stale fallback.
            try:
                await self._check_daily_quota()
            except QuotaExceededError as quota_error:
                self._last_error = quota_error.code
                raise
            age = (datetime.now(timezone.utc) - cached[1]).total_seconds()
            if 0 <= age <= self.config.stale_hours * 3600:
                self._stale_hits += 1
                return self._result(cached[0], cached[1], stale=True, reason=error.code)
        raise error

    @staticmethod
    def _result(data: T, fetched_at: datetime, *, stale: bool = False, reason: str | None = None) -> FetchResult[T]:
        records = data if isinstance(data, list) else [data]
        times = [x.updated_at for x in records if getattr(x, "updated_at", None) is not None]
        return FetchResult(data, fetched_at, stale, max(times) if times else None, reason)

    def _send_ready(self) -> bool:
        self._check_ready()
        now = time.monotonic()
        return now >= self._not_before and (self._last_start is None
                or now >= self._last_start + self.config.request_interval)

    async def _pace(self) -> None:
        """Wait at the send gate; the caller holds _start_lock until dispatch."""
        while True:
            now = time.monotonic()
            at = max(self._not_before, (self._last_start + self.config.request_interval)
                     if self._last_start is not None else now)
            if at <= now:
                break
            await asyncio.sleep(at - now)
        self._check_ready()

    @staticmethod
    def _retry_after(value: str | None) -> float | None:
        if value is None:
            return None
        try:
            seconds = float(value)
        except ValueError:
            try:
                stamp = parsedate_to_datetime(value)
                if stamp.tzinfo is None:
                    return None
                seconds = stamp.timestamp() - time.time()
            except (ValueError, TypeError, OverflowError):
                return None
        return max(0.0, seconds) if math.isfinite(seconds) else None

    async def _request(self, path: str, params: dict[str, Any]) -> Any:
        if self._http is None:
            self._http = httpx.AsyncClient()
        c = self.config
        headers = {"Accept": "application/json", "User-Agent": "astrbot-plugin-ba-stats", "X-Client": c.client_id}
        if c.api_token:
            # Optional Bearer scheme is a deployment choice, NOT a verified
            # BATrace requirement; configure only if operator-approved.
            headers["Authorization"] = "Bearer " + c.api_token
        for attempt in range(c.retries + 1):
            await self._check_daily_quota()
            async with self._start_lock:
                while True:
                    await self._pace()
                    try:
                        await self._consume_request()
                    except AdmissionDeferred:
                        # Quota rolled back before commit: wait without holding
                        # SQLite and reserve against the final admission date.
                        continue
                    break
                self._last_start = time.monotonic()
                self._requests += 1
            # No suspension between releasing the gate and handing off to HTTP;
            # only request starts are serialized, never response processing.
            delay = 30.0
            try:
                response = await self._http.get(
                    c.base_url.rstrip("/") + path, params=params, headers=headers,
                    timeout=c.request_timeout, follow_redirects=False,
                )
            except httpx.TimeoutException:
                error: ClientError = RequestTimeoutError("连接或读取数据源超时，请稍后重试。")
            except httpx.RequestError:
                error = UpstreamError("暂时无法连接到数据源，请稍后重试。")
            else:
                status = response.status_code
                if status in {401, 403}:
                    raise AuthenticationError("数据源鉴权失败或拒绝访问，请管理员检查客户端标识与令牌配置。")
                content_type = response.headers.get("content-type", "").split(";")[0].lower()
                is_json = content_type == "application/json" or content_type.endswith("+json")
                if not is_json and any(mark in response.text for mark in (
                    "TencentEOCaptchaWidget", "EO-Bot-Captcha-Token", "Security Verification", "__tst_status",
                )):
                    raise AuthenticationError("数据源触发了人机验证，当前接入暂不可用，请管理员联系运营方核实。")
                if status == 429:
                    delay = self._retry_after(response.headers.get("Retry-After"))
                    delay = delay if delay is not None else 30.0
                    error = RateLimitError(retry_after=delay)
                    self._not_before = max(self._not_before, time.monotonic() + delay)
                    if delay >= c.wait_timeout:
                        raise error
                elif status >= 500:
                    error = UpstreamError(f"""数据源服务暂时异常（HTTP {status}），请稍后重试。""")
                else:
                    if not is_json:
                        raise SchemaError("数据源返回了非 JSON 格式的响应，暂无法解析。")
                    try:
                        payload = response.json()
                    except ValueError as exc:
                        raise SchemaError("数据源返回的 JSON 内容无效，暂无法解析。") from exc
                    if status == 404:
                        raise NoDataError("数据源暂未收录该记录，无法据此判断账号是否存在。")
                    if not 200 <= status < 300:
                        raise ClientError(f"""数据源拒绝了本次请求（HTTP {status}）。""")
                    return payload
            if attempt >= c.retries:
                raise error
            # Backoff belongs to the same bounded job/deadline and rate gate.
            self._not_before = max(self._not_before, time.monotonic() + delay)
        raise UpstreamError("数据源请求失败")  # unreachable, keeps type checkers precise

    async def clear_cache(self) -> None:
        async with self._cache_lock:
            self._generation += 1  # an older in-flight result must not refill cleared cache
            if self._storage is not None:
                await self._storage.clear_cache()

    async def close(self) -> None:
        self._closed = True
        jobs = [f.task for f in self._flights.values()]
        for job in jobs:
            job.cancel()
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)
        self._flights.clear()
        if self._owns_http and self._http is not None:
            await self._http.aclose()
