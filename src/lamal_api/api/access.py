"""Access control: API keys and rate limiting, in one pass.

Authentication and throttling live in a single middleware on purpose. The rate
limit is applied to the *authenticated identity* when keys are on and to the
client IP when they are off, so the two have to run in a fixed order —
expressing that as two middlewares would depend on Starlette's stacking order,
which is easy to get backwards and silently wrong.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import Engine

from ..config import Settings
from ..security import authenticate, extract_key, flush_usage

log = logging.getLogger(__name__)

#: Always reachable without a key. The container HEALTHCHECK and any
#: orchestrator probe call /health, so requiring a key there would make a
#: correctly configured deployment restart-loop forever.
ALWAYS_PUBLIC: frozenset[str] = frozenset({"/health"})

#: Never rate limited — probes must not be throttled out of existence.
NEVER_LIMITED: frozenset[str] = frozenset({"/health"})

_WINDOW_SECONDS = 60.0
_SWEEP_SECONDS = 300.0
_USAGE_FLUSH_SECONDS = 60.0


class SlidingWindowLimiter:
    """Fixed-memory sliding window counter.

    Buckets are pruned on access and swept periodically, so a caller that stops
    calling stops costing memory. The previous implementation kept one deque
    per client forever, which grew without bound on a public instance.
    """

    def __init__(
        self, window: float = _WINDOW_SECONDS, sweep_interval: float = _SWEEP_SECONDS
    ) -> None:
        self._window = window
        self._sweep_interval = sweep_interval
        self._hits: dict[str, deque[float]] = {}
        self._last_sweep = time.monotonic()

    def check(self, identity: str, limit: int) -> int | None:
        """Register a hit. Returns ``Retry-After`` seconds when over the limit."""
        now = time.monotonic()
        self._maybe_sweep(now)

        bucket = self._hits.get(identity)
        if bucket is None:
            bucket = deque()
            self._hits[identity] = bucket

        cutoff = now - self._window
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()

        if len(bucket) >= limit:
            return int(self._window - (now - bucket[0])) + 1

        bucket.append(now)
        return None

    def _maybe_sweep(self, now: float) -> None:
        if now - self._last_sweep < self._sweep_interval:
            return
        self._last_sweep = now
        cutoff = now - self._window
        stale = [key for key, hits in self._hits.items() if not hits or hits[-1] <= cutoff]
        for key in stale:
            del self._hits[key]
        if stale:
            log.debug("rate limiter swept %d idle buckets", len(stale))

    @property
    def tracked(self) -> int:
        """How many identities are currently held. Used by the tests."""
        return len(self._hits)


class UsageBuffer:
    """Accumulates per-key request counts and flushes them periodically.

    Writing on every request would turn a read-only service into a write-heavy
    one and serialise callers behind SQLite's writer lock, so counts are
    batched. The batching has one exception: the **first** time a key is seen
    in this process the buffer flushes straight away, so ``last_used_at`` is
    accurate within a request instead of lagging by up to a minute. Without
    that, a key under active traffic reads as "never used" — which looks like
    a broken counter rather than a deferred write.

    A hard kill still loses at most one interval of counts. They are usage
    statistics, not billing records, and losing a few is preferable to a
    database write per request.
    """

    def __init__(self, interval: float = _USAGE_FLUSH_SECONDS) -> None:
        self._interval = interval
        self._counts: dict[str, int] = {}
        self._seen: set[str] = set()
        self._flush_soon = False
        self._last_flush = time.monotonic()

    def record(self, prefix: str) -> None:
        if prefix not in self._seen:
            self._seen.add(prefix)
            self._flush_soon = True
        self._counts[prefix] = self._counts.get(prefix, 0) + 1

    def maybe_flush(self, engine: Engine, *, force: bool = False) -> None:
        now = time.monotonic()
        due = force or self._flush_soon or (now - self._last_flush >= self._interval)
        if not due:
            return
        pending, self._counts = self._counts, {}
        self._last_flush = now
        self._flush_soon = False
        if not pending:
            return
        try:
            flush_usage(engine, pending, datetime.now(UTC))
        except Exception:
            log.exception("could not persist API key usage counters")


@dataclass
class _Denied:
    status: int
    error: str
    message: str
    headers: dict[str, str] | None = None

    def response(self) -> JSONResponse:
        return JSONResponse(
            status_code=self.status,
            headers=self.headers or {},
            content={"error": self.error, "message": self.message},
        )


def install_access_control(app: FastAPI, settings: Settings) -> None:
    """Attach key authentication and rate limiting, if either is enabled."""
    if not settings.auth_enabled and settings.rate_limit_per_minute <= 0:
        return

    limiter = SlidingWindowLimiter()
    usage = UsageBuffer()
    app.state.rate_limiter = limiter
    app.state.usage_buffer = usage

    @app.middleware("http")
    async def _access_control(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        path = request.url.path
        engine: Engine = request.app.state.engine

        identity = request.client.host if request.client else "unknown"
        limit = settings.rate_limit_per_minute

        if settings.auth_enabled and path not in ALWAYS_PUBLIC:
            key = extract_key(
                request.headers.get(settings.api_key_header),
                request.headers.get("Authorization"),
            )
            if key is None:
                return _Denied(
                    401,
                    "missing_api_key",
                    f"This instance requires an API key. Send it as the "
                    f"{settings.api_key_header} header, or as "
                    f"'Authorization: Bearer <key>'.",
                    {"WWW-Authenticate": "Bearer"},
                ).response()

            with engine.connect() as conn:
                record = authenticate(conn, key)
            if record is None:
                return _Denied(
                    403,
                    "invalid_api_key",
                    "That API key is not valid, or it has been revoked.",
                ).response()

            identity = record.prefix
            if record.rate_limit_per_minute is not None:
                limit = record.rate_limit_per_minute
            request.state.api_key_prefix = record.prefix
            usage.record(record.prefix)
            usage.maybe_flush(engine)

        if limit > 0 and path not in NEVER_LIMITED:
            retry_after = limiter.check(identity, limit)
            if retry_after is not None:
                return _Denied(
                    429,
                    "rate_limited",
                    f"Too many requests. The limit is {limit} per minute.",
                    {"Retry-After": str(retry_after)},
                ).response()

        return await call_next(request)
