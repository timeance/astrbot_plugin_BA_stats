"""SQLite bindings and persistent JSON cache in the plugin data directory."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import json
import math
from pathlib import Path
import time
from typing import AsyncIterator, Callable

import aiosqlite

if __package__:
    from .quota import AdmissionDeferred, GLOBAL_DAILY_LIMIT, QuotaActor, QuotaExceededError, quota_day
else:  # The standalone storage tests import this module without AstrBot.
    from quota import AdmissionDeferred, GLOBAL_DAILY_LIMIT, QuotaActor, QuotaExceededError, quota_day


@dataclass(frozen=True)
class Binding:
    namespace: str
    player_id: str
    display_name: str
    source_url: str = ""
    updated_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not isinstance(self.player_id, str) or not self.player_id:
            raise ValueError("玩家 ID 须为非空字符串。")
        if not isinstance(self.namespace, str) or not self.namespace:
            raise ValueError("数据源命名空间不能为空。")


class Storage:
    def __init__(
        self,
        data_dir: str | Path,
        *,
        max_entries: int = 1000,
        max_age_seconds: float = 604800,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if (type(max_entries) is not int or max_entries < 0
                or not math.isfinite(max_age_seconds) or max_age_seconds < 0):
            raise ValueError("缓存容量和最长保留时间须为非负有限数值。")
        self.path = Path(data_dir) / "ba_stats.sqlite3"
        self.max_entries = max_entries
        self.max_age_seconds = max_age_seconds
        self._clock = clock if clock is not None else time.time
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    def _connection(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("数据库尚未打开。")
        return self._db

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """Keep every write and its cancellation cleanup inside the connection lock."""
        async with self._lock:
            db = self._connection()
            try:
                async with db.execute("BEGIN IMMEDIATE"):
                    pass
                yield db
                await db.commit()
            except BaseException:
                # aiosqlite still runs queued SQL after its waiter is cancelled.
                # Drain rollback, even on repeated cancellation, before reuse.
                rollback = asyncio.create_task(db.rollback())
                while not rollback.done():
                    try:
                        await asyncio.shield(rollback)
                    except asyncio.CancelledError:
                        continue
                rollback.result()
                raise

    async def open(self) -> None:
        async with self._lock:
            if self._db is not None:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            db = await aiosqlite.connect(self.path)
            try:
                await db.executescript("""
                    CREATE TABLE IF NOT EXISTS bindings (
                        platform TEXT NOT NULL, user_id TEXT NOT NULL,
                        namespace TEXT NOT NULL, player_id TEXT NOT NULL,
                        display_name TEXT NOT NULL, source_url TEXT NOT NULL,
                        updated_at REAL NOT NULL, PRIMARY KEY (platform, user_id)
                    );
                    CREATE TABLE IF NOT EXISTS cache (
                        cache_key TEXT PRIMARY KEY, payload TEXT NOT NULL,
                        fetched_at REAL NOT NULL, expires_at REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS cache_fetched_at ON cache (fetched_at);
                    CREATE TABLE IF NOT EXISTS request_quotas (
                        day TEXT NOT NULL, scope TEXT NOT NULL,
                        platform TEXT NOT NULL, subject TEXT NOT NULL,
                        used INTEGER NOT NULL CHECK (used >= 0),
                        PRIMARY KEY (day, scope, platform, subject)
                    );
                """)
                await db.commit()
            except BaseException:
                await db.close()
                raise
            self._db = db

    async def close(self) -> None:
        async with self._lock:
            if self._db is not None:
                await self._db.close()
                self._db = None

    async def get_binding(self, platform: str, user: str) -> Binding | None:
        async with self._lock:
            async with self._connection().execute(
                "SELECT namespace, player_id, display_name, source_url, updated_at "
                "FROM bindings WHERE platform = ? AND user_id = ?", (platform, user)
            ) as cursor:
                row = await cursor.fetchone()
        return Binding(*row) if row is not None else None

    async def set_binding(self, platform: str, user: str, binding: Binding) -> None:
        async with self._transaction() as db:
            await db.execute(
                "INSERT INTO bindings VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(platform, user_id) DO UPDATE SET namespace=excluded.namespace, "
                "player_id=excluded.player_id, display_name=excluded.display_name, "
                "source_url=excluded.source_url, updated_at=excluded.updated_at",
                (platform, user, binding.namespace, binding.player_id, binding.display_name,
                 binding.source_url, binding.updated_at),
            )

    async def unbind(self, platform: str, user: str) -> bool:
        async with self._transaction() as db:
            async with db.execute(
                "DELETE FROM bindings WHERE platform = ? AND user_id = ?", (platform, user)
            ) as cursor:
                removed = cursor.rowcount > 0
            return removed

    async def get_cache(self, key: str) -> tuple[dict | list, float, float] | None:
        """Return retained entries, including expired ones, for labelled fallback."""
        async with self._lock:
            async with self._connection().execute(
                "SELECT payload, fetched_at, expires_at FROM cache WHERE cache_key = ?", (key,)
            ) as cursor:
                row = await cursor.fetchone()
        if row is None:
            return None
        return json.loads(row[0]), float(row[1]), float(row[2])

    async def put_cache(self, key: str, payload: dict | list, ttl: float) -> None:
        if not isinstance(payload, (dict, list)):
            raise ValueError("缓存内容须为字典或列表。")
        if not math.isfinite(ttl) or ttl < 0:
            raise ValueError("缓存有效期须为非负有限秒数。")
        serialized = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        async with self._transaction() as db:
            now = self._clock()
            await db.execute(
                "INSERT INTO cache VALUES (?, ?, ?, ?) ON CONFLICT(cache_key) DO UPDATE SET "
                "payload=excluded.payload, fetched_at=excluded.fetched_at, expires_at=excluded.expires_at",
                (key, serialized, now, now + ttl),
            )
            await self._prune(db, self.max_entries, self.max_age_seconds)

    async def clear_cache(self) -> None:
        async with self._transaction() as db:
            await db.execute("DELETE FROM cache")

    async def _prune(self, db: aiosqlite.Connection, maximum: int, max_age: float) -> int:
        async with db.execute("DELETE FROM cache WHERE fetched_at <= ?", (self._clock() - max_age,)) as cursor:
            removed = cursor.rowcount
        async with db.execute(
            "DELETE FROM cache WHERE cache_key IN (SELECT cache_key FROM cache "
            "ORDER BY fetched_at DESC, cache_key ASC LIMIT -1 OFFSET ?)", (maximum,)
        ) as cursor:
            return removed + cursor.rowcount

    async def prune_cache(self, max_entries: int | None = None, max_age_seconds: float | None = None) -> int:
        """Use constructor bounds unless overridden; never touch bindings."""
        max_entries = self.max_entries if max_entries is None else max_entries
        max_age_seconds = self.max_age_seconds if max_age_seconds is None else max_age_seconds
        if (type(max_entries) is not int or max_entries < 0
                or not math.isfinite(max_age_seconds) or max_age_seconds < 0):
            raise ValueError("缓存容量和最长保留时间须为非负有限数值。")
        async with self._transaction() as db:
            removed = await self._prune(db, max_entries, max_age_seconds)
            return removed

    async def cache_count(self) -> int:
        async with self._lock:
            async with self._connection().execute("SELECT COUNT(*) FROM cache") as cursor:
                row = await cursor.fetchone()
                return int(row[0])

    async def _quota_usage(self, db: aiosqlite.Connection, day: str, actor: QuotaActor | None) -> dict:
        usage = {"day": day, "global": 0, "user": 0 if actor else None,
                 "group": 0 if actor and actor.group else None}
        platform, user, group = (actor.platform, actor.user, actor.group) if actor else ("", "", "")
        async with db.execute(
            "SELECT scope, used FROM request_quotas WHERE day = ? AND ("
            "(scope = 'global' AND platform = '' AND subject = '') OR "
            "(scope = 'user' AND platform = ? AND subject = ?) OR "
            "(scope = 'group' AND platform = ? AND subject = ?))",
            (day, platform, user, platform, group),
        ) as cursor:
            for scope, used in await cursor.fetchall():
                usage[scope] = int(used)
        return usage

    async def quota_usage(self, actor: QuotaActor | None = None) -> dict:
        """Read today's committed usage in a single snapshot; never reset it."""
        async with self._lock:
            return await self._quota_usage(self._connection(), quota_day(self._clock()), actor)

    async def consume_request(
        self, actor: QuotaActor | None = None, *, user_limit: int = 20, group_limit: int = 100,
        ready: Callable[[], bool] | None = None,
    ) -> dict:
        """Reserve one request atomically across all applicable daily buckets.

        The global limit is fixed. Zero disables only the user/group limit,
        never their accounting. No credentials or data-source namespace enter
        these keys. Cancellation/errors before commit roll back every bucket.
        """
        if any(type(value) is not int or value < 0 for value in (user_limit, group_limit)):
            raise ValueError("用户和群每日额度必须为非负整数，0 表示不限。")
        async with self._transaction() as db:
            if ready is not None and not ready():
                raise AdmissionDeferred
            # Evaluate the date AFTER obtaining the cross-connection write lock:
            # a queued request can cross Beijing midnight.
            day = quota_day(self._clock())
            usage = await self._quota_usage(db, day, actor)
            for scope, limit in (("global", GLOBAL_DAILY_LIMIT), ("user", user_limit),
                                 ("group", group_limit)):
                if usage[scope] is not None and limit and usage[scope] >= limit:
                    raise QuotaExceededError(scope, limit, usage[scope])
            buckets = [(day, "global", "", "")]
            if actor is not None:
                buckets.append((day, "user", actor.platform, actor.user))
                if actor.group:
                    buckets.append((day, "group", actor.platform, actor.group))
            async with db.executemany(
                "INSERT INTO request_quotas (day, scope, platform, subject, used) "
                "VALUES (?, ?, ?, ?, 1) ON CONFLICT(day, scope, platform, subject) "
                "DO UPDATE SET used = request_quotas.used + 1", buckets,
            ):
                pass
            # Check again after SQLite awaits, while the write is uncommitted.
            # Defer outside the transaction rather than waiting after charging.
            if quota_day(self._clock()) != day or (ready is not None and not ready()):
                raise AdmissionDeferred
        for scope in ("global", "user", "group"):
            if usage[scope] is not None:
                usage[scope] += 1
        return usage
