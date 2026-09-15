"""Sync orchestration: discover, download, validate, load.

Guarantees
----------
**Idempotent.** Every ingested file is recorded in ``data_source`` with its
SHA-256. Re-running a sync against unchanged upstream files touches nothing and
returns ``skipped``.

**Atomic per year.** A year's rows are deleted and re-inserted inside a single
transaction, so readers either see the old year or the new one, never a
half-loaded table. This is also how mid-year corrections are absorbed: the FOPH
republishes the whole file rather than emitting supersession rows, and the
premium data carries no validity columns to honour.

**Self-updating.** The premium year is read out of the downloaded file rather
than configured, so the late-September release of a new year is picked up
without a code or config change.

**Survives the September gap.** Before releasing a new premium year, the FOPH
moves the current year into its yearly archive and replaces the loose files
with header-only ones. A sync during those weeks loads the newest archived year
instead, so a fresh installation still starts with data.
"""

from __future__ import annotations

import logging
import tempfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import islice
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import Engine, Table, delete, insert, select

from ..config import Settings
from ..db import models
from ..db.engine import optimize
from . import ckan, priminfo
from .download import Download, download, make_client
from .normalize import (
    EmptySourceFileError,
    SourceFormatError,
    commune_and_postal_rows,
    insurer_rows,
    premium_rows,
    region_workbook_year,
    restriction_rows,
    tariff_rows,
)
from .readers import extract_from_zip, read_csv_dicts

log = logging.getLogger(__name__)

_INSERT_CHUNK = 5_000


@dataclass
class YearResult:
    """What happened for one premium year."""

    year: int
    status: str  # 'loaded' | 'skipped' | 'failed'
    premium_rows: int = 0
    eu_rows: int = 0
    tariff_rows: int = 0
    restriction_rows: int = 0
    message: str = ""


@dataclass
class SyncReport:
    """Outcome of a whole sync run."""

    started_at: datetime
    finished_at: datetime | None = None
    live_year: int | None = None
    years: list[YearResult] = field(default_factory=list)
    communes: int = 0
    postal_links: int = 0
    insurers: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def loaded_years(self) -> list[int]:
        return [y.year for y in self.years if y.status == "loaded"]

    def summary(self) -> str:
        live = self.live_year if self.live_year is not None else "none (published without data)"
        parts = [f"live premium year: {live}"]
        for result in self.years:
            if result.status == "loaded":
                parts.append(
                    f"{result.year}: loaded {result.premium_rows:,} CH premiums, "
                    f"{result.eu_rows:,} EU premiums, {result.tariff_rows:,} tariffs, "
                    f"{result.restriction_rows:,} commune restrictions"
                )
            else:
                parts.append(f"{result.year}: {result.status} ({result.message})")
        parts.append(f"communes: {self.communes:,}, postal links: {self.postal_links:,}")
        parts.append(f"insurer names: {self.insurers}")
        for warning in self.warnings:
            parts.append(f"warning: {warning}")
        if self.finished_at:
            parts.append(f"took {(self.finished_at - self.started_at).total_seconds():.1f}s")
        return "\n".join(parts)


def sync(
    engine: Engine,
    settings: Settings,
    *,
    years: Iterable[int] | None = None,
    force: bool = False,
    cache_dir: Path | None = None,
) -> SyncReport:
    """Fetch the official data and load it into the database."""
    report = SyncReport(started_at=datetime.now(UTC))
    with tempfile.TemporaryDirectory(prefix="lamal-sync-") as tmp:
        workdir = cache_dir or Path(tmp)
        workdir.mkdir(parents=True, exist_ok=True)
        with make_client(
            user_agent=settings.user_agent,
            timeout=settings.http_timeout_seconds,
            ca_bundle=settings.ca_bundle,
        ) as client:
            _run(engine, settings, client, workdir, report, years, force)
    report.finished_at = datetime.now(UTC)
    return report


def _run(
    engine: Engine,
    settings: Settings,
    client: httpx.Client,
    workdir: Path,
    report: SyncReport,
    years: Iterable[int] | None,
    force: bool,
) -> None:
    catalog = ckan.fetch_catalog(client)

    live_premiums = download(client, catalog.premiums_ch.url, workdir / "Praemien_CH.csv")
    live_year: int | None
    try:
        live_year = peek_premium_year(live_premiums.path)
    except EmptySourceFileError:
        live_year = None
        message = (
            f"{catalog.premiums_ch.file_name} is published without data, as it is for a few "
            f"weeks before each new premium year; loading from the yearly archives instead"
        )
        log.warning(message)
        report.warnings.append(message)
    else:
        log.info("live premium file carries premium year %d", live_year)
    report.live_year = live_year

    requested = sorted(set(years)) if years is not None else settings.requested_years()
    targets = resolve_years(requested, live_year, catalog.archive_years())

    for year in targets:
        try:
            if year == live_year:
                result = _load_live_year(
                    engine, client, catalog, workdir, live_premiums, year, force
                )
            else:
                result = _load_archived_year(engine, client, catalog, workdir, year, force)
        except (SourceFormatError, LookupError, RuntimeError, httpx.HTTPError) as exc:
            log.exception("sync failed for premium year %d", year)
            result = YearResult(year=year, status="failed", message=str(exc))
            report.warnings.append(f"premium year {year} failed: {exc}")
        report.years.append(result)

    _load_regions(engine, client, workdir, report, targets, force)
    _load_insurers(engine, client, workdir, report, max(targets), force)

    with engine.begin() as conn:
        optimize(conn)


# --------------------------------------------------------------------------
# Premium years
# --------------------------------------------------------------------------


def resolve_years(
    requested: list[int] | None, live_year: int | None, archive_years: list[int]
) -> list[int]:
    """Decide which premium years a sync loads.

    Years the operator asked for always win. Otherwise the sync loads the year
    the live files carry or, while those are published without data, the newest
    archived year — the one that was just moved out of the live files.
    """
    if requested:
        return requested
    if live_year is not None:
        return [live_year]
    if archive_years:
        return [max(archive_years)]
    raise SourceFormatError(
        "the live premium file has no data rows and no yearly archive is published"
    )


def peek_premium_year(path: Path) -> int:
    """Read the business year from the first data row of a premium CSV."""
    for row in read_csv_dicts(path):
        raw = row.get("Geschäftsjahr", "").strip()
        if raw.isdigit():
            return int(raw)
        raise SourceFormatError(f"{path.name}: first row has Geschäftsjahr={raw!r}")
    raise EmptySourceFileError(f"{path.name} contains no data rows")


def _load_live_year(
    engine: Engine,
    client: httpx.Client,
    catalog: ckan.Catalog,
    workdir: Path,
    premiums: Download,
    year: int,
    force: bool,
) -> YearResult:
    """Load the year currently published as loose files."""
    if not force and _already_ingested(engine, "premiums_ch", year, premiums.sha256):
        log.info("premium year %d already up to date (sha256 unchanged)", year)
        return YearResult(year=year, status="skipped", message="unchanged upstream file")

    eu = download(client, catalog.premiums_eu.url, workdir / "Praemien_EU.csv")
    tariffs = download(client, catalog.tariffs.url, workdir / "Tarife.csv")
    catchment = download(client, catalog.catchment_areas.url, workdir / "Einzugsgebiete.csv")

    return _ingest_year(
        engine,
        year,
        premiums=premiums,
        eu=eu,
        tariffs=tariffs,
        catchment=catchment,
    )


def _load_archived_year(
    engine: Engine,
    client: httpx.Client,
    catalog: ckan.Catalog,
    workdir: Path,
    year: int,
    force: bool,
) -> YearResult:
    """Load a past year from its official yearly archive."""
    resource = catalog.archive(year)
    if resource is None:
        available = catalog.archive_years()
        return YearResult(
            year=year,
            status="failed",
            message=f"no archive published for {year}; available: {available}",
        )

    archive = download(client, resource.url, workdir / f"Archiv_Praemien_{year}.zip")
    if not force and _already_ingested(engine, "premiums_ch", year, archive.sha256):
        return YearResult(year=year, status="skipped", message="unchanged archive")

    extract_dir = workdir / str(year)
    # Premiums are read from CSV only - see readers.read_tabular_dicts.
    premium_path = extract_from_zip(
        archive.path, ("Prämien_CH.csv", "Praemien_CH.csv"), extract_dir
    )
    premiums = Download(
        url=resource.url,
        path=premium_path,
        sha256=archive.sha256,
        byte_size=archive.byte_size,
    )

    eu = _optional_member(archive, ("Prämien_EU.csv", "Praemien_EU.csv"), extract_dir, resource.url)
    tariffs = _optional_member(archive, ("Tarife.csv", "Tarife.xlsx"), extract_dir, resource.url)
    catchment = _optional_member(
        archive,
        ("Einzugsgebiete.csv", "Einzugsgebiete.xlsx"),
        extract_dir,
        resource.url,
    )
    return _ingest_year(
        engine, year, premiums=premiums, eu=eu, tariffs=tariffs, catchment=catchment
    )


def _optional_member(
    archive: Download, names: tuple[str, ...], dest: Path, url: str
) -> Download | None:
    try:
        path = extract_from_zip(archive.path, names, dest)
    except FileNotFoundError:
        log.warning("%s contains none of %s; skipping", archive.file_name, list(names))
        return None
    return Download(url=url, path=path, sha256=archive.sha256, byte_size=path.stat().st_size)


def _ingest_year(
    engine: Engine,
    year: int,
    *,
    premiums: Download,
    eu: Download | None,
    tariffs: Download | None,
    catchment: Download | None,
) -> YearResult:
    """Replace one premium year's data inside a single transaction."""
    result = YearResult(year=year, status="loaded")

    with engine.begin() as conn:
        conn.execute(delete(models.premium).where(models.premium.c.premium_year == year))
        result.premium_rows = _bulk_insert(
            conn,
            models.premium,
            _expect_year(premium_rows(premiums.path), year, premiums.file_name),
        )
        _record_source(conn, "premiums_ch", year, premiums, result.premium_rows)

        conn.execute(delete(models.premium_eu).where(models.premium_eu.c.premium_year == year))
        if eu is not None:
            result.eu_rows = _bulk_insert(
                conn,
                models.premium_eu,
                _expect_year(premium_rows(eu.path, territory="EU"), year, eu.file_name),
            )
            _record_source(conn, "premiums_eu", year, eu, result.eu_rows)

        conn.execute(delete(models.tariff).where(models.tariff.c.premium_year == year))
        if tariffs is not None:
            result.tariff_rows = _bulk_insert(
                conn,
                models.tariff,
                _expect_year(tariff_rows(tariffs.path), year, tariffs.file_name),
            )
            _record_source(conn, "tariffs", year, tariffs, result.tariff_rows)

        conn.execute(
            delete(models.tariff_restriction).where(
                models.tariff_restriction.c.premium_year == year
            )
        )
        if catchment is not None:
            result.restriction_rows = _bulk_insert(
                conn,
                models.tariff_restriction,
                _expect_year(restriction_rows(catchment.path), year, catchment.file_name),
            )
            _record_source(conn, "catchment_areas", year, catchment, result.restriction_rows)

    log.info(
        "premium year %d loaded: %s CH rows, %s EU rows, %s tariffs, %s restrictions",
        year,
        f"{result.premium_rows:,}",
        f"{result.eu_rows:,}",
        f"{result.tariff_rows:,}",
        f"{result.restriction_rows:,}",
    )
    return result


def _expect_year(
    rows: Iterator[dict[str, Any]], year: int, source: str
) -> Iterator[dict[str, Any]]:
    """Guard against loading a file whose business year is not what we asked for."""
    for row in rows:
        if row["premium_year"] != year:
            raise SourceFormatError(
                f"{source} contains premium year {row['premium_year']} but {year} was expected"
            )
        yield row


# --------------------------------------------------------------------------
# Regions and insurers
# --------------------------------------------------------------------------


def _load_regions(
    engine: Engine,
    client: httpx.Client,
    workdir: Path,
    report: SyncReport,
    targets: list[int],
    force: bool,
) -> None:
    """Load the commune/postal-code to premium-region mapping.

    The workbook is filed under the year it declares itself valid for, which is
    not always the year of the live premium file: between the September premium
    release and January, premiums are already for next year while the region
    workbook still describes the current one. The query layer resolves this by
    falling back to the most recent region year at or before the premium year.
    """
    urls: dict[str, None] = {}
    for year in targets:
        urls[priminfo.regions_url_for_year(client, year)] = None

    for index, url in enumerate(urls):
        try:
            book = download(client, url, workdir / f"praemienregionen_{index}.xlsx")
            workbook_year = region_workbook_year(book.path, fallback=max(targets))
            if not force and _already_ingested(engine, "regions", workbook_year, book.sha256):
                log.info("premium regions for %d already up to date", workbook_year)
                continue
            communes, postal = commune_and_postal_rows(book.path, workbook_year)
            with engine.begin() as conn:
                conn.execute(
                    delete(models.commune).where(models.commune.c.premium_year == workbook_year)
                )
                conn.execute(
                    delete(models.postal_code_commune).where(
                        models.postal_code_commune.c.premium_year == workbook_year
                    )
                )
                report.communes += _bulk_insert(conn, models.commune, iter(communes))
                report.postal_links += _bulk_insert(conn, models.postal_code_commune, iter(postal))
                _record_source(conn, "regions", workbook_year, book, len(communes))
        except (SourceFormatError, RuntimeError, httpx.HTTPError, ValueError) as exc:
            log.exception("could not load premium regions from %s", url)
            report.warnings.append(f"premium regions from {url} failed: {exc}")


def _load_insurers(
    engine: Engine,
    client: httpx.Client,
    workdir: Path,
    report: SyncReport,
    year: int,
    force: bool,
) -> None:
    """Load insurer names. Best-effort: the API works without them."""
    url = priminfo.find_insurer_directory_url(client, year)
    if url is None:
        report.warnings.append(
            "insurer directory not found on priminfo.admin.ch; "
            "/v1/insurers will return BAG numbers without names"
        )
        return
    try:
        book = download(client, url, workdir / "insurers.xlsx")
        if not force and _already_ingested(engine, "insurers", None, book.sha256):
            log.info("insurer directory already up to date")
            return
        records = insurer_rows(book.path)
        now = datetime.now(UTC)
        with engine.begin() as conn:
            conn.execute(delete(models.insurer))
            report.insurers = _bulk_insert(
                conn, models.insurer, iter([{**r, "updated_at": now} for r in records])
            )
            _record_source(conn, "insurers", None, book, len(records))
    except (SourceFormatError, RuntimeError, httpx.HTTPError, ValueError) as exc:
        log.warning("could not load insurer names: %s", exc)
        report.warnings.append(f"insurer names unavailable: {exc}")


# --------------------------------------------------------------------------
# Low-level helpers
# --------------------------------------------------------------------------


def _bulk_insert(conn: Any, table: Table, rows: Iterator[dict[str, Any]]) -> int:
    """Insert rows in chunks, returning the count."""
    total = 0
    while chunk := list(islice(rows, _INSERT_CHUNK)):
        conn.execute(insert(table), chunk)
        total += len(chunk)
    return total


def _record_source(conn: Any, kind: str, year: int | None, file: Download, row_count: int) -> None:
    conn.execute(
        delete(models.data_source).where(
            models.data_source.c.kind == kind,
            models.data_source.c.premium_year.is_(None)
            if year is None
            else models.data_source.c.premium_year == year,
        )
    )
    conn.execute(
        insert(models.data_source).values(
            kind=kind,
            premium_year=year,
            source_url=file.url,
            file_name=file.file_name,
            content_sha256=file.sha256,
            byte_size=file.byte_size,
            row_count=row_count,
            published_at=None,
            fetched_at=datetime.now(UTC),
        )
    )


def _already_ingested(engine: Engine, kind: str, year: int | None, sha256: str) -> bool:
    """True when this exact file version is already in the database."""
    condition = (
        models.data_source.c.premium_year.is_(None)
        if year is None
        else models.data_source.c.premium_year == year
    )
    with engine.connect() as conn:
        found = conn.execute(
            select(models.data_source.c.id).where(
                models.data_source.c.kind == kind,
                condition,
                models.data_source.c.content_sha256 == sha256,
            )
        ).first()
    return found is not None
