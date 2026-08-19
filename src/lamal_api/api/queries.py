"""Data access. Every read the API performs lives here.

The service never computes a price. It resolves a location to a premium
region, works out the age class and franchise from the person's details, and
then *filters* the approved premiums the FOPH published.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import ColumnElement, Select, and_, distinct, func, or_, select
from sqlalchemy.engine import Connection

from ..db import models
from ..domain.codes import AgeClass, region_number
from ..domain.errors import (
    AmbiguousPostalCodeError,
    NoDataError,
    UnknownCommuneError,
    UnknownPostalCodeError,
    UnknownPremiumYearError,
)
from ..domain.rules import valid_franchises
from . import schemas

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Years
# --------------------------------------------------------------------------


def available_premium_years(conn: Connection) -> list[int]:
    rows = conn.execute(
        select(distinct(models.premium.c.premium_year)).order_by(models.premium.c.premium_year)
    ).scalars()
    return list(rows)


def available_region_years(conn: Connection) -> list[int]:
    rows = conn.execute(
        select(distinct(models.commune.c.premium_year)).order_by(models.commune.c.premium_year)
    ).scalars()
    return list(rows)


def current_premium_year(conn: Connection) -> int:
    years = available_premium_years(conn)
    if not years:
        raise NoDataError(
            "No premium data has been loaded yet. Run 'lamal-api sync' "
            "(or wait for the scheduled sync) and try again."
        )
    return years[-1]


def require_premium_year(conn: Connection, year: int | None) -> int:
    """Validate an explicit year, or fall back to the most recent one loaded."""
    years = available_premium_years(conn)
    if not years:
        raise NoDataError(
            "No premium data has been loaded yet. Run 'lamal-api sync' "
            "(or wait for the scheduled sync) and try again."
        )
    if year is None:
        return years[-1]
    if year not in years:
        raise UnknownPremiumYearError(
            f"Premium year {year} is not loaded. Available: "
            f"{', '.join(str(y) for y in years)}. Load older years with "
            f"'lamal-api sync --years {year}'.",
            requested=year,
            available=years,
        )
    return year


def resolve_region_year(conn: Connection, premium_year: int) -> int:
    """Pick the premium-region file that applies to ``premium_year``.

    Premiums and premium regions are published on different schedules: from the
    late-September premium release until January, the premium file already
    carries next year while the region workbook still describes the current
    one. Falling back to the most recent region year at or before the premium
    year keeps postal-code resolution working across that window.
    """
    latest = conn.execute(
        select(func.max(models.commune.c.premium_year)).where(
            models.commune.c.premium_year <= premium_year
        )
    ).scalar()
    if latest is not None:
        return int(latest)
    earliest = conn.execute(select(func.min(models.commune.c.premium_year))).scalar()
    if earliest is None:
        raise NoDataError(
            "No premium-region data has been loaded. Postal code and commune "
            "lookups need it; run 'lamal-api sync'."
        )
    log.warning("no region file at or before %d; falling back to %d", premium_year, int(earliest))
    return int(earliest)


# --------------------------------------------------------------------------
# Location resolution
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedLocation:
    """A location resolved down to a canton and premium region."""

    canton: str
    region: str
    bfs_numbers: tuple[int, ...]
    payload: schemas.LocationResolution


def _commune_model(row: object) -> schemas.Commune:
    r = row._mapping  # type: ignore[attr-defined]
    return schemas.Commune(
        bfs_number=r["bfs_number"],
        name=r["name"],
        canton=r["canton"],
        district=r["district"],
        region=r["region"],
        region_number=region_number(r["region"]),
    )


def resolve_location(
    conn: Connection,
    *,
    region_year: int,
    postal_code: int | None = None,
    bfs_number: int | None = None,
) -> ResolvedLocation:
    """Turn a postal code or BFS number into a canton and premium region.

    The BFS number is authoritative. The official premium-region file states it
    plainly: *"Die PLZ ist somit nicht entscheidend"* / *"Seul le N° OFS fait
    foi"* — a postal code is only an indication, because a person belongs to
    the premium region of their commune of residence.
    """
    if bfs_number is not None:
        row = conn.execute(
            select(models.commune).where(
                models.commune.c.premium_year == region_year,
                models.commune.c.bfs_number == bfs_number,
            )
        ).first()
        if row is None:
            raise UnknownCommuneError(
                f"No commune with BFS number {bfs_number} in the {region_year} premium-region "
                f"file. Communes merge and are renumbered every year; check the current "
                f"number on the Federal Statistical Office commune register.",
                bfs_number=bfs_number,
                region_year=region_year,
            )
        commune = _commune_model(row)
        return ResolvedLocation(
            canton=commune.canton,
            region=commune.region,
            bfs_numbers=(commune.bfs_number,),
            payload=schemas.LocationResolution(
                bfs_number=bfs_number, resolved=commune, candidates=[], ambiguous=False
            ),
        )

    if postal_code is None:
        raise ValueError("either postal_code or bfs_number must be given")

    rows = conn.execute(
        select(
            models.commune.c.bfs_number,
            models.commune.c.name,
            models.commune.c.canton,
            models.commune.c.district,
            models.commune.c.region,
            models.postal_code_commune.c.postal_code,
            models.postal_code_commune.c.locality,
        )
        .select_from(
            models.postal_code_commune.join(
                models.commune,
                and_(
                    models.commune.c.premium_year == models.postal_code_commune.c.premium_year,
                    models.commune.c.bfs_number == models.postal_code_commune.c.bfs_number,
                ),
            )
        )
        .where(
            models.postal_code_commune.c.premium_year == region_year,
            models.postal_code_commune.c.postal_code == postal_code,
        )
        .order_by(models.commune.c.name, models.postal_code_commune.c.locality)
    ).all()

    if not rows:
        raise UnknownPostalCodeError(
            f"Postal code {postal_code} is not in the {region_year} premium-region file. "
            f"Swiss postal codes are four digits (1000–9999); PO-box-only codes and "
            f"codes outside Switzerland are not covered.",
            postal_code=postal_code,
            region_year=region_year,
        )

    candidates = [
        schemas.PostalCodeCandidate(
            bfs_number=r._mapping["bfs_number"],
            name=r._mapping["name"],
            canton=r._mapping["canton"],
            district=r._mapping["district"],
            region=r._mapping["region"],
            region_number=region_number(r._mapping["region"]),
            postal_code=r._mapping["postal_code"],
            locality=r._mapping["locality"],
        )
        for r in rows
    ]

    unique_communes = {c.bfs_number: c for c in candidates}
    distinct_regions = {(c.canton, c.region) for c in candidates}

    if len(distinct_regions) > 1:
        # The premium genuinely differs between these communes, so guessing
        # would return wrong prices. Hand the caller the choice instead.
        raise AmbiguousPostalCodeError(
            f"Postal code {postal_code} spans {len(unique_communes)} communes in "
            f"{len(distinct_regions)} different premium regions, which have different "
            f"premiums. Repeat the request with one of the bfs_number values below.",
            postal_code=postal_code,
            candidates=[c.model_dump() for c in candidates],
        )

    canton, region = next(iter(distinct_regions))
    first = next(iter(unique_communes.values()))
    ambiguous = len(unique_communes) > 1
    return ResolvedLocation(
        canton=canton,
        region=region,
        bfs_numbers=tuple(sorted(unique_communes)),
        payload=schemas.LocationResolution(
            postal_code=postal_code,
            resolved=None if ambiguous else first,
            candidates=candidates,
            ambiguous=ambiguous,
        ),
    )


# --------------------------------------------------------------------------
# Premium search
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PremiumCriteria:
    premium_year: int
    canton: str
    region: str
    age_class: AgeClass
    age_subgroup: str
    franchise_chf: int
    accident: str
    bfs_numbers: tuple[int, ...]
    tariff_types: tuple[str, ...] | None = None
    insurers: tuple[int, ...] | None = None
    limit: int = 50
    offset: int = 0
    sort: str = "premium_asc"


def _base_filter(criteria: PremiumCriteria) -> list[ColumnElement[bool]]:
    p = models.premium.c
    clauses: list[ColumnElement[bool]] = [
        p.premium_year == criteria.premium_year,
        p.canton == criteria.canton,
        p.region == criteria.region,
        p.age_class == criteria.age_class.value,
        p.age_subgroup == criteria.age_subgroup,
        p.franchise_chf == criteria.franchise_chf,
        p.accident == criteria.accident,
    ]
    if criteria.tariff_types:
        clauses.append(p.tariff_type.in_(criteria.tariff_types))
    if criteria.insurers:
        clauses.append(p.insurer_bag_number.in_(criteria.insurers))
    return clauses


def _exclude_unavailable_models(criteria: PremiumCriteria) -> ColumnElement[bool]:
    """Drop alternative models the person's commune is excluded from.

    A handful of HMO and family-doctor models are sold only in a named list of
    communes (``Einzugsgebiete.csv``, ``Eingeschränkt = Y``). Returning them for
    a commune outside that list would quote a price the person cannot buy, so
    they are filtered out with a NOT EXISTS on the restriction table: a tariff
    survives when it has no restriction rows at all, or when at least one of
    the caller's candidate communes appears in them.
    """
    p = models.premium.c
    r = models.tariff_restriction.c

    has_restriction = (
        select(1)
        .where(
            r.premium_year == p.premium_year,
            r.insurer_bag_number == p.insurer_bag_number,
            r.canton == p.canton,
            r.region == p.region,
            r.tariff_code == p.tariff_code,
        )
        .exists()
    )
    covers_our_commune = (
        select(1)
        .where(
            r.premium_year == p.premium_year,
            r.insurer_bag_number == p.insurer_bag_number,
            r.canton == p.canton,
            r.region == p.region,
            r.tariff_code == p.tariff_code,
            r.bfs_number.in_(criteria.bfs_numbers),
        )
        .exists()
    )
    return or_(~has_restriction, covers_our_commune)


def _order_by(sort: str) -> list[ColumnElement[Any]]:
    p = models.premium.c
    if sort == "premium_desc":
        return [p.premium_centimes.desc(), p.insurer_bag_number, p.tariff_code]
    if sort == "insurer":
        return [p.insurer_bag_number, p.premium_centimes, p.tariff_code]
    return [p.premium_centimes, p.insurer_bag_number, p.tariff_code]


def count_premiums(conn: Connection, criteria: PremiumCriteria) -> int:
    stmt = (
        select(func.count())
        .select_from(models.premium)
        .where(*_base_filter(criteria), _exclude_unavailable_models(criteria))
    )
    return int(conn.execute(stmt).scalar() or 0)


def search_premiums(conn: Connection, criteria: PremiumCriteria) -> list[schemas.PremiumResult]:
    """Return the matching approved premiums, cheapest first by default."""
    p = models.premium.c
    i = models.insurer.c
    t = models.tariff.c

    stmt: Select[Any] = (
        select(
            p.insurer_bag_number,
            i.name.label("insurer_name"),
            p.tariff_code,
            p.tariff_type,
            p.tariff_label,
            t.name_de,
            t.name_fr,
            t.name_it,
            p.premium_centimes,
            p.franchise_chf,
            p.franchise_level,
            p.canton,
            p.region,
            p.age_class,
            p.age_subgroup,
            p.accident,
            p.is_standard_franchise,
            p.premium_year,
        )
        .select_from(
            models.premium.outerjoin(
                models.insurer, i.bag_number == p.insurer_bag_number
            ).outerjoin(
                models.tariff,
                and_(
                    t.premium_year == p.premium_year,
                    t.insurer_bag_number == p.insurer_bag_number,
                    t.tariff_code == p.tariff_code,
                ),
            )
        )
        .where(*_base_filter(criteria), _exclude_unavailable_models(criteria))
        .order_by(*_order_by(criteria.sort))
        .limit(criteria.limit)
        .offset(criteria.offset)
    )

    results = []
    for row in conn.execute(stmt):
        m = row._mapping
        results.append(
            schemas.PremiumResult(
                insurer=schemas.InsurerRef(
                    bag_number=m["insurer_bag_number"], name=m["insurer_name"]
                ),
                tariff=schemas.TariffRef(
                    code=m["tariff_code"],
                    type=m["tariff_type"],
                    label=m["tariff_label"],
                    name_de=m["name_de"],
                    name_fr=m["name_fr"],
                    name_it=m["name_it"],
                ),
                premium_chf=m["premium_centimes"] / 100,
                premium_centimes=m["premium_centimes"],
                franchise_chf=m["franchise_chf"],
                franchise_level=m["franchise_level"],
                canton=m["canton"],
                region=m["region"],
                age_class=m["age_class"],
                age_subgroup=m["age_subgroup"],
                accident_coverage=m["accident"] == "MIT-UNF",
                is_standard_franchise=bool(m["is_standard_franchise"]),
                premium_year=m["premium_year"],
            )
        )
    return results


# --------------------------------------------------------------------------
# Reference endpoints
# --------------------------------------------------------------------------


def list_insurers(conn: Connection, premium_year: int) -> list[schemas.InsurerSummary]:
    """Insurers that actually offer premiums in the given year.

    Driven by the premium table rather than the register, so insurers that hold
    an authorisation but sell no basic-insurance premiums never show up.
    """
    p = models.premium.c
    i = models.insurer.c

    rows = conn.execute(
        select(
            p.insurer_bag_number,
            i.name,
            i.domicile,
            func.count(distinct(p.canton)).label("canton_count"),
            func.count(distinct(p.tariff_code)).label("tariff_count"),
        )
        .select_from(models.premium.outerjoin(models.insurer, i.bag_number == p.insurer_bag_number))
        .where(p.premium_year == premium_year)
        .group_by(p.insurer_bag_number, i.name, i.domicile)
        .order_by(i.name.is_(None), i.name, p.insurer_bag_number)
    ).all()

    canton_rows = conn.execute(
        select(distinct(p.insurer_bag_number), p.canton).where(p.premium_year == premium_year)
    ).all()
    cantons: dict[int, list[str]] = {}
    for insurer_number, canton in canton_rows:
        cantons.setdefault(insurer_number, []).append(canton)

    return [
        schemas.InsurerSummary(
            bag_number=r._mapping["insurer_bag_number"],
            name=r._mapping["name"],
            domicile=r._mapping["domicile"],
            cantons=sorted(cantons.get(r._mapping["insurer_bag_number"], [])),
            tariff_count=r._mapping["tariff_count"],
        )
        for r in rows
    ]


def franchise_levels(conn: Connection, premium_year: int, age_class: AgeClass) -> dict[int, str]:
    """Map each franchise amount to its ``FRAST`` level for one age class.

    The set of amounts comes from the statute, not from the loaded rows: the
    franchise scale is fixed by law and a person may choose any of them.
    Deriving the list from whatever data happens to be present would make the
    answer depend on which years were synced. The ``FRAST`` levels are filled
    in from the data where available, since those are a property of the
    published file rather than of the law.
    """
    p = models.premium.c
    rows = conn.execute(
        select(distinct(p.franchise_chf), p.franchise_level).where(
            p.premium_year == premium_year, p.age_class == age_class.value
        )
    ).all()
    observed = {int(amount): level for amount, level in rows}
    return {amount: observed.get(amount, "") for amount in valid_franchises(age_class)}


def premium_row_count(conn: Connection) -> int:
    return int(conn.execute(select(func.count()).select_from(models.premium)).scalar() or 0)


def data_sources(conn: Connection) -> list[schemas.SourceInfo]:
    rows = conn.execute(
        select(models.data_source).order_by(
            models.data_source.c.kind, models.data_source.c.premium_year
        )
    ).all()
    return [
        schemas.SourceInfo(
            kind=r._mapping["kind"],
            premium_year=r._mapping["premium_year"],
            file_name=r._mapping["file_name"],
            source_url=r._mapping["source_url"],
            content_sha256=r._mapping["content_sha256"],
            byte_size=r._mapping["byte_size"],
            row_count=r._mapping["row_count"],
            fetched_at=r._mapping["fetched_at"],
        )
        for r in rows
    ]


def last_sync_at(conn: Connection) -> object | None:
    return conn.execute(select(func.max(models.data_source.c.fetched_at))).scalar()
