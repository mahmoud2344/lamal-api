"""Command line entry point: ``lamal-api sync | serve | info | version``.

Uses ``argparse`` from the standard library rather than a CLI framework — the
surface is three commands and the dependency budget is better spent elsewhere.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import __version__
from .config import Settings, get_settings
from .db.engine import create_db_engine, init_schema


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _parse_years(spec: str | None) -> list[int] | None:
    if not spec:
        return None
    settings = Settings(sync_years=spec)
    return settings.requested_years()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lamal-api",
        description="Swiss health insurance premium API — data sync and server.",
    )
    parser.add_argument("--version", action="version", version=f"lamal-api {__version__}")
    parser.add_argument(
        "--database-url",
        help="Override DATABASE_URL for this invocation.",
    )
    parser.add_argument("--log-level", default=None, help="DEBUG, INFO, WARNING, ERROR.")
    sub = parser.add_subparsers(dest="command", required=True)

    sync_cmd = sub.add_parser(
        "sync",
        help="Download the latest official data and load it into the database.",
        description=(
            "Idempotent. Re-running against unchanged upstream files does nothing. "
            "With no --years, the premium year currently published is loaded."
        ),
    )
    sync_cmd.add_argument(
        "--years",
        help="Premium years to load, e.g. '2026', '2024,2026' or '2024-2026'. "
        "Years other than the live one are read from the official yearly archives.",
    )
    sync_cmd.add_argument(
        "--force",
        action="store_true",
        help="Reload even when the upstream file hash is unchanged.",
    )
    sync_cmd.add_argument(
        "--cache-dir",
        type=Path,
        help="Keep downloads in this directory instead of a temporary one.",
    )

    serve_cmd = sub.add_parser("serve", help="Run the HTTP API server.")
    serve_cmd.add_argument("--host")
    serve_cmd.add_argument("--port", type=int)
    serve_cmd.add_argument("--reload", action="store_true", help="Auto-reload for development.")

    sub.add_parser(
        "info",
        help="Show the resolved upstream sources and what the database currently holds.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    if args.database_url:
        settings = settings.model_copy(update={"database_url": args.database_url})
    _configure_logging(args.log_level.upper() if args.log_level else settings.log_level)

    if args.command == "sync":
        return _cmd_sync(settings, args)
    if args.command == "serve":
        return _cmd_serve(settings, args)
    if args.command == "info":
        return _cmd_info(settings)
    return 2


def _cmd_sync(settings: Settings, args: argparse.Namespace) -> int:
    from .fetch.sync import sync

    engine = create_db_engine(settings)
    init_schema(engine)
    report = sync(
        engine,
        settings,
        years=_parse_years(args.years),
        force=args.force,
        cache_dir=args.cache_dir,
    )
    print(report.summary())
    failed = [y for y in report.years if y.status == "failed"]
    return 1 if failed else 0


def _cmd_serve(settings: Settings, args: argparse.Namespace) -> int:
    import uvicorn

    host = args.host or settings.host
    port = args.port or settings.port
    uvicorn.run(
        "lamal_api.api.app:create_app",
        factory=True,
        host=host,
        port=port,
        reload=args.reload,
        log_level=settings.log_level.lower(),
    )
    return 0


def _cmd_info(settings: Settings) -> int:
    from sqlalchemy import func, select

    from .db import models
    from .fetch import ckan
    from .fetch.download import make_client

    engine = create_db_engine(settings)
    init_schema(engine)

    print(f"lamal-api {__version__}")
    print(f"database: {engine.url.render_as_string(hide_password=True)}\n")

    with engine.connect() as conn:
        rows = conn.execute(
            select(
                models.premium.c.premium_year,
                func.count().label("n"),
            ).group_by(models.premium.c.premium_year)
        ).all()
        if rows:
            print("premium years in database:")
            for year, count in sorted(rows):
                print(f"  {year}: {count:,} rows")
        else:
            print("premium years in database: none — run 'lamal-api sync'")

        sources = conn.execute(
            select(
                models.data_source.c.kind,
                models.data_source.c.premium_year,
                models.data_source.c.file_name,
                models.data_source.c.fetched_at,
            ).order_by(models.data_source.c.kind)
        ).all()
        if sources:
            print("\ningested sources:")
            for kind, year, name, fetched in sources:
                print(f"  {kind:<16} {year or '-'!s:<6} {name:<32} {fetched}")

    print("\nupstream resources advertised by opendata.swiss:")
    try:
        with make_client(
            user_agent=settings.user_agent, timeout=settings.http_timeout_seconds
        ) as client:
            catalog = ckan.fetch_catalog(client)
        print(ckan.summarise(catalog.resources))
        print(f"\narchive years available: {catalog.archive_years()}")
    except Exception as exc:
        print(f"  (could not reach opendata.swiss: {exc})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
