"""Single-use candidate selection, isolated by platform/session/user.

Methods are synchronous and locked: call begin before awaiting the network,
then publish with its generation. Requests and candidates stay opaque.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import itertools
import math
import secrets
import threading
import time
from typing import Any, Callable, Iterable


Scope = tuple[str, str, str]


@dataclass(frozen=True)
class Pending:
    scope: Scope
    generation: int
    code: str
    request: Any
    candidates: tuple[Any, ...]
    created_at: float
    expires_at: float


@dataclass
class _Slot:
    generation: int
    expires_at: float
    pending: Pending | None = None


class PendingManager:
    def __init__(
        self,
        ttl: float = 120,
        max_entries: int = 1000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not math.isfinite(ttl) or ttl <= 0 or type(max_entries) is not int or max_entries < 1:
            raise ValueError("候选有效期和容量须大于零。")
        self.ttl = ttl
        self.max_entries = max_entries
        self._clock = clock
        self._generations = itertools.count(1)
        self._slots: OrderedDict[Scope, _Slot] = OrderedDict()
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        for scope in [scope for scope, slot in self._slots.items() if slot.expires_at <= now]:
            del self._slots[scope]

    def begin(self, scope: Scope) -> int:
        """Invalidate the old request immediately, before network work starts."""
        with self._lock:
            now = self._clock()
            self._prune(now)
            self._slots.pop(scope, None)
            while len(self._slots) >= self.max_entries:
                self._slots.popitem(last=False)
            generation = next(self._generations)
            self._slots[scope] = _Slot(generation, now + self.ttl)
            return generation

    def is_current(self, scope: Scope, generation: int) -> bool:
        """Check an unexpired generation without consuming or extending it."""
        with self._lock:
            self._prune(self._clock())
            slot = self._slots.get(scope)
            return slot is not None and slot.generation == generation

    def clear(self) -> None:
        """Invalidate all work on shutdown without recycling generations."""
        with self._lock:
            self._slots.clear()

    def publish(
        self, scope: Scope, generation: int, request: Any, candidates: Iterable[Any]
    ) -> Pending | None:
        """Return None if replaced, expired, consumed, or already published."""
        values = tuple(candidates)
        with self._lock:
            now = self._clock()
            self._prune(now)
            slot = self._slots.get(scope)
            if slot is None or slot.generation != generation or slot.pending is not None:
                return None
            if not values:
                del self._slots[scope]
                return None
            pending = Pending(scope, generation, f"{generation:x}{secrets.token_hex(2)}",
                              request, values, now, now + self.ttl)
            slot.pending = pending
            slot.expires_at = pending.expires_at
            return pending

    def consume(self, scope: Scope, code: str, index: int) -> tuple[Any, Any]:
        """Atomically consume a one-based choice; invalid choices preserve it."""
        with self._lock:
            self._prune(self._clock())
            slot = self._slots.get(scope)
            pending = slot.pending if slot is not None else None
            if pending is None or pending.code != code:
                raise ValueError("该选择请求不存在、已过期，或不属于当前会话，请重新发起查询。")
            if type(index) is not int or not 1 <= index <= len(pending.candidates):
                raise ValueError(f"候选序号须为 1 到 {len(pending.candidates)} 的整数。")
            del self._slots[scope]
            return pending.request, pending.candidates[index - 1]

    def cancel(self, scope: Scope, code: str) -> bool:
        """Cancel only the matching request; return False for any stale code."""
        with self._lock:
            self._prune(self._clock())
            slot = self._slots.get(scope)
            if slot is None or slot.pending is None or slot.pending.code != code:
                return False
            del self._slots[scope]
            return True
