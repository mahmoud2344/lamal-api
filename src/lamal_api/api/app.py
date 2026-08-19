"""FastAPI application factory."""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse

from .. import __version__
from ..config import Settings, get_settings
from ..db.engine import create_db_engine, init_schema
from ..domain.errors import LamalError
from ..scheduler import start_background_sync, start_scheduler
from .routers import premiums, reference, regions

log = logging.getLogger(__name__)

DESCRIPTION = """
A REST API for **Swiss mandatory health insurance (LAMal/KVG) premiums**, built on the
official open data published by the Federal Office of Public Health (FOPH/BAG/OFSP).

Premiums are **looked up, never calculated**. Every figure returned is a value the FOPH
approved and published; this service filters and sorts them.

### How a query resolves

1. A postal code is resolved to a commune (BFS number), and the commune to a premium
   region. The BFS number is authoritative — the official region file states that the
   postal code is only indicative.
2. The birth year is turned into an age class using the age the person *reaches during*
   the premium year: 0–18 children, 19–25 young adults, 26+ adults.
3. The franchise is validated against that age class.
4. Matching approved premiums are returned, cheapest first.

### Things worth knowing

* One insurer can offer several named tariffs of the same type at different prices. They
  are returned as separate results — that is not duplication.
* Not every insurer operates in every canton and region, so empty results are normal.
* Children default to age subgroup `K1`. `K3`/`K4`/`K5` are sibling discount tiers that
  only apply when several children are insured together.
* A few alternative models are sold only in specific communes; they are filtered out
  automatically when the commune is known.

**Unofficial service.** Not affiliated with or endorsed by the Swiss Confederation.
Verify any premium at [priminfo.admin.ch](https://www.priminfo.admin.ch) before acting on it.
"""

TAGS_METADATA = [
    {"name": "premiums", "description": "Premium lookup — the main endpoints."},
    {"name": "regions", "description": "Postal code and commune to premium region."},
    {"name": "reference", "description": "Insurers, franchise options and data provenance."},
    {"name": "ops", "description": "Health checks."},
]


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the ASGI application."""
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_db_engine(settings)
        init_schema(engine)
        app.state.engine = engine
        app.state.settings = settings

        if settings.sync_on_startup:
            # On a worker thread: the API answers /health immediately while the
            # first sync, which downloads ~23 MB, runs behind it.
            start_background_sync(engine, settings, reason="startup")

        scheduler = start_scheduler(engine, settings)
        try:
            yield
        finally:
            if scheduler is not None:
                scheduler.shutdown(wait=False)
            engine.dispose()

    app = FastAPI(
        title="lamal-api",
        version=__version__,
        description=DESCRIPTION,
        openapi_tags=TAGS_METADATA,
        lifespan=lifespan,
        root_path=settings.root_path,
        license_info={"name": "MIT", "url": "https://opensource.org/licenses/MIT"},
        contact={
            "name": "lamal-api",
            "url": "https://github.com/mahmoud2344/lamal-api",
        },
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list(),
        allow_credentials=False,
        allow_methods=["GET"],
        allow_headers=["*"],
    )

    if settings.rate_limit_per_minute > 0:
        _install_rate_limit(app, settings.rate_limit_per_minute)

    @app.exception_handler(LamalError)
    async def _domain_error(_: Request, exc: LamalError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content=exc.to_payload())

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": "invalid_request",
                "message": "One or more parameters are invalid.",
                "details": {"errors": exc.errors()},
            },
        )

    @app.get("/", include_in_schema=False)
    async def _root() -> RedirectResponse:
        return RedirectResponse(url=f"{settings.root_path}/docs")

    app.include_router(premiums.router)
    app.include_router(regions.router)
    app.include_router(reference.router)
    return app


def _install_rate_limit(app: FastAPI, per_minute: int) -> None:
    """A deliberately simple in-memory per-IP limiter.

    Single-process only, and reset on restart. Anything beyond that belongs in
    a reverse proxy, which is why this is off unless RATE_LIMIT_PER_MINUTE is
    set.
    """
    hits: defaultdict[str, deque[float]] = defaultdict(deque)
    window = 60.0

    @app.middleware("http")
    async def _limit(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.url.path in {"/health", "/docs", "/openapi.json"}:
            return await call_next(request)
        client = request.client.host if request.client else "unknown"
        now = time.monotonic()
        bucket = hits[client]
        while bucket and now - bucket[0] > window:
            bucket.popleft()
        if len(bucket) >= per_minute:
            retry_after = int(window - (now - bucket[0])) + 1
            return JSONResponse(
                status_code=429,
                headers={"Retry-After": str(retry_after)},
                content={
                    "error": "rate_limited",
                    "message": f"Too many requests. The limit is {per_minute} per minute.",
                },
            )
        bucket.append(now)
        return await call_next(request)
