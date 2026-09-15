"""Turn raw federal rows into validated, database-ready records.

Validation is strict on purpose. If the FOPH changes a vocabulary or drops a
column, the sync must fail loudly with a message naming the file and the value,
rather than quietly loading a table that produces wrong premiums.

Codes are translated through :mod:`.vocabulary`, which reads both the files up
to premium year 2026 and the renamed codes used from 2027. The column names did
not change between the two, so one set of loaders serves both.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, TypeVar

from openpyxl import load_workbook

from ..domain.codes import Territory
from ..domain.rules import parse_premium_to_centimes
from . import vocabulary
from .readers import (
    normalize_key,
    read_csv_dicts,
    read_tabular_dicts,
    read_xlsx_rows,
    require_columns,
)

log = logging.getLogger(__name__)

PREMIUM_CH_COLUMNS = (
    "Versicherer",
    "Kanton",
    "Hoheitsgebiet",
    "Geschäftsjahr",
    "Erhebungsjahr",
    "Region",
    "Altersklasse",
    "Unfalleinschluss",
    "Tarif",
    "Tariftyp",
    "Altersuntergruppe",
    "Franchisestufe",
    "Franchise",
    "Prämie",
    "isBaseP",
    "isBaseF",
    "Tarifbezeichnung",
)
PREMIUM_EU_COLUMNS = tuple("Land" if c == "Kanton" else c for c in PREMIUM_CH_COLUMNS)

TARIFF_COLUMNS = (
    "Versicherer",
    "Geschäftsjahr",
    "Kategorie",
    "Tarif",
    "Tariftyp",
    "Name_DE",
    "Name_FR",
    "Name_IT",
)
CATCHMENT_COLUMNS = (
    "Versicherer",
    "Kanton",
    "Geschäftsjahr",
    "Region",
    "Tarif",
    "Tariftyp",
    "Eingeschränkt",
    "Gemeinden-BFS",
)

_T = TypeVar("_T")


class SourceFormatError(ValueError):
    """An official file did not look the way the loader expects."""


class EmptySourceFileError(SourceFormatError):
    """An official file has its header but no data rows.

    The FOPH does this on purpose every September: the loose files are replaced
    by empty ones in the next year's layout before the new premiums are
    released.
    """


def _code(translate: Callable[[str], _T], raw: str, where: str) -> _T:
    """Run a :mod:`.vocabulary` translation, naming the file and line on failure."""
    try:
        return translate(raw)
    except vocabulary.UnknownCodeError as exc:
        raise SourceFormatError(f"{where}: {exc}") from None


def _int(raw: Any, field: str, source: str) -> int:
    text = str(raw).strip()
    if not text:
        raise SourceFormatError(f"{source}: empty {field}")
    try:
        return int(text)
    except ValueError as exc:  # e.g. '0008' is fine, 'n/a' is not
        raise SourceFormatError(f"{source}: {field} is not an integer: {raw!r}") from exc


def premium_rows(path: Path, *, territory: str = "CH") -> Iterator[dict[str, Any]]:
    """Stream ``Prämien_CH.csv`` (or ``Prämien_EU.csv``) as premium records.

    ``Versicherer`` is normalised to an ``int``: the premium file writes it
    unpadded (``8``) while ``Tarife.csv`` and ``Einzugsgebiete.csv`` in the same
    dataset zero-pad it (``0008``). Storing an int is what lets the three files
    join at all.
    """
    table = Territory(territory)
    is_eu = table is Territory.EU_EFTA
    expected = PREMIUM_EU_COLUMNS if is_eu else PREMIUM_CH_COLUMNS
    checked_header = False
    source = path.name

    def in_table(raw: str) -> str:
        return vocabulary.region(raw, table)

    for line, row in enumerate(read_csv_dicts(path), start=2):
        if not checked_header:
            require_columns(list(row), expected, source)
            checked_header = True
        where = f"{source}:{line}"

        record: dict[str, Any] = {
            "premium_year": _int(row["Geschäftsjahr"], "Geschäftsjahr", source),
            "survey_year": _int(row["Erhebungsjahr"], "Erhebungsjahr", source),
            "insurer_bag_number": _int(row["Versicherer"], "Versicherer", source),
            "region": _code(in_table, row["Region"], where),
            "age_class": _code(vocabulary.age_class, row["Altersklasse"], where),
            "age_subgroup": _code(vocabulary.age_subgroup, row["Altersuntergruppe"], where),
            "accident": _code(vocabulary.accident, row["Unfalleinschluss"], where),
            "tariff_code": row["Tarif"].strip(),
            "tariff_type": _code(vocabulary.tariff_type, row["Tariftyp"], where),
            "tariff_label": row["Tarifbezeichnung"].strip(),
            "franchise_chf": _code(vocabulary.franchise_amount, row["Franchise"], where),
            "franchise_level": _code(vocabulary.franchise_level, row["Franchisestufe"], where),
            "premium_centimes": parse_premium_to_centimes(row["Prämie"]),
            # isBaseF marks the ordinary franchise (FRAST1). isBaseP is NOT
            # used: it is only set on the MIT-UNF half of the TAR-BASE rows, so
            # treating it as "is the standard model" silently loses every
            # without-accident standard premium. Tariff type carries that fact.
            "is_standard_franchise": row["isBaseF"].strip() == "1",
        }
        if is_eu:
            record["country"] = _code(vocabulary.country, row["Land"], where)
        else:
            record["canton"] = row["Kanton"].strip()
        yield record


def tariff_rows(path: Path) -> Iterator[dict[str, Any]]:
    """Stream ``Tarife.csv``, keeping only actual insurance models.

    The file mixes two categories under ``Kategorie``: ``MOD`` rows are
    insurance models (what we want) and ``ALT`` rows are age-category labels
    (``E1`` = adults, ``K3`` = "from the 3rd child"), which are not tariffs.

    From 2027 the file adds ``Name_EN`` (not stored yet) and drops ``Sort.-Nr.``,
    so ``sort_order`` is empty for those years.
    """
    checked_header = False
    source = path.name
    rows = read_tabular_dicts(path, header_markers=("Versicherer", "Tarif", "Name_DE"))
    for line, row in enumerate(rows, start=2):
        if not checked_header:
            require_columns(list(row), TARIFF_COLUMNS, source)
            checked_header = True
        if row["Kategorie"].strip().upper() != "MOD":
            continue
        yield {
            "premium_year": _int(row["Geschäftsjahr"], "Geschäftsjahr", source),
            "insurer_bag_number": _int(row["Versicherer"], "Versicherer", source),
            "tariff_code": row["Tarif"].strip(),
            "tariff_type": _code(vocabulary.tariff_type, row["Tariftyp"], f"{source}:{line}"),
            "name_de": row["Name_DE"].strip() or None,
            "name_fr": row["Name_FR"].strip() or None,
            "name_it": row["Name_IT"].strip() or None,
            "sort_order": _optional_int(row.get("Sort.-Nr.")),
        }


def restriction_rows(path: Path) -> Iterator[dict[str, Any]]:
    """Stream the restricted entries of ``Einzugsgebiete.csv``.

    Most alternative models are sold everywhere in their canton and region and
    carry ``Eingeschränkt = N``. A handful are limited to an explicit list of
    communes; the comma-separated BFS list is exploded into one row each so it
    can be joined rather than parsed at query time.

    The region goes through the same translation as the premium file's. The
    query layer matches a restriction to a premium by region, so a code left
    untranslated here would not error: the restriction would simply never
    match, and the model would be offered in every commune.
    """
    checked_header = False
    source = path.name
    rows = read_tabular_dicts(path, header_markers=("Versicherer", "Tarif", "Eingeschränkt"))

    def swiss_region(raw: str) -> str:
        return vocabulary.region(raw, Territory.SWITZERLAND)

    for line, row in enumerate(rows, start=2):
        if not checked_header:
            require_columns(list(row), CATCHMENT_COLUMNS, source)
            checked_header = True
        if row["Eingeschränkt"].strip().upper() != "Y":
            continue
        raw_list = row["Gemeinden-BFS"].strip()
        if not raw_list:
            log.warning(
                "%s: tariff %s of insurer %s is flagged restricted but lists no communes; "
                "treating it as unrestricted",
                source,
                row["Tarif"],
                row["Versicherer"],
            )
            continue
        base = {
            "premium_year": _int(row["Geschäftsjahr"], "Geschäftsjahr", source),
            "insurer_bag_number": _int(row["Versicherer"], "Versicherer", source),
            "canton": row["Kanton"].strip(),
            "region": _code(swiss_region, row["Region"], f"{source}:{line}"),
            "tariff_code": row["Tarif"].strip(),
        }
        for part in raw_list.split(","):
            part = part.strip()
            if part:
                yield {**base, "bfs_number": int(part)}


# --------------------------------------------------------------------------
# Premium regions workbook (priminfo)
# --------------------------------------------------------------------------

_VALIDITY_RE = re.compile(r"(?:ab|du)\s+\d{2}\.\d{2}\.(\d{4})", re.IGNORECASE)

COMMUNE_SHEET = "A_COM"
COMMUNE_HEADER_MARKERS = ("BFS-Nr.", "Kanton", "Gemeinde", "Region", "PLZ")


def region_workbook_year(path: Path, fallback: int) -> int:
    """Read the validity year out of the region workbook's info sheet.

    The workbook states "Prämienregionen gültig ab 01.01.2026 bis 31.12.2026".
    Reading it matters because the region file and the premium file are
    published on different schedules: between late September and January the
    premium file already carries next year while the region workbook still
    describes the current one.
    """
    from .readers import read_xlsx_cell_texts

    for sheet in ("Informationen", "Informations"):
        for text in read_xlsx_cell_texts(path, sheet, limit=10):
            match = _VALIDITY_RE.search(text)
            if match:
                return int(match.group(1))
    log.warning(
        "%s: no validity statement found, filing communes under premium year %d",
        path.name,
        fallback,
    )
    return fallback


def commune_and_postal_rows(
    path: Path, premium_year: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Parse sheet ``A_COM`` into commune and postal-code records.

    Region is stored as a plain integer (0–3) in this workbook and as
    ``'PR-REG CH0'``–``'PR-REG CH3'`` in the premium file, so it is translated
    here — this is the single join that makes a postal code resolve to a price.
    """
    from ..domain.codes import region_code

    communes: dict[int, dict[str, Any]] = {}
    postal: set[tuple[int, int, str, int]] = set()

    for row in read_xlsx_rows(path, COMMUNE_SHEET, header_markers=COMMUNE_HEADER_MARKERS):
        bfs_raw = row.get("BFS-Nr.")
        if bfs_raw is None or not str(bfs_raw).strip().isdigit():
            continue
        bfs = int(bfs_raw)
        region_raw = row.get("Region")
        if region_raw is None or not str(region_raw).strip().isdigit():
            raise SourceFormatError(f"{path.name}: commune {bfs} has region {region_raw!r}")

        communes.setdefault(
            bfs,
            {
                "premium_year": premium_year,
                "bfs_number": bfs,
                "name": _text(row.get("Gemeinde")),
                "canton": _text(row.get("Kanton")).upper(),
                "district": _text(row.get("Bezirk")) or None,
                "region": region_code(int(region_raw)),
            },
        )

        plz_raw = row.get("PLZ")
        if plz_raw is not None and str(plz_raw).strip().isdigit():
            postal.add((premium_year, int(plz_raw), _text(row.get("Ort")), bfs))

    if not communes:
        raise SourceFormatError(f"{path.name}!{COMMUNE_SHEET} produced no communes")

    postal_records = [
        {
            "premium_year": year,
            "postal_code": plz,
            "locality": locality,
            "bfs_number": bfs,
        }
        for year, plz, locality, bfs in sorted(postal)
    ]
    log.info(
        "parsed %d communes and %d postal-code links for %d",
        len(communes),
        len(postal_records),
        premium_year,
    )
    return list(communes.values()), postal_records


# --------------------------------------------------------------------------
# Insurer directory workbook (priminfo)
# --------------------------------------------------------------------------

INSURER_SHEET_MARKER = "index"


def insurer_rows(path: Path) -> list[dict[str, Any]]:
    """Parse the approved-insurer directory into ``bag_number -> name``.

    The ``Index`` sheet is used rather than the detailed one: the detail sheet
    wraps a single insurer across several rows with merged cells and embedded
    newlines, while the index is one clean row per insurer. Note the sheet is
    literally named ``"Index "`` with a trailing space, so it is matched
    loosely.
    """
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        sheet_name = next(
            (s for s in workbook.sheetnames if s.strip().casefold() == INSURER_SHEET_MARKER),
            None,
        )
        if sheet_name is None:
            raise SourceFormatError(
                f"{path.name} has no Index sheet; available: {workbook.sheetnames}"
            )
        worksheet = workbook[sheet_name]

        rows = list(worksheet.iter_rows(values_only=True))
        header_index = _find_header(rows)
        number_col, name_col, place_col = _insurer_columns(rows[header_index])

        records: dict[int, dict[str, Any]] = {}
        for row in rows[header_index + 1 :]:
            if number_col >= len(row):
                continue
            raw_number = row[number_col]
            if not isinstance(raw_number, (int, float)):
                continue
            name = _text(row[name_col]) if name_col < len(row) else ""
            if not name:
                continue
            bag_number = int(raw_number)
            records[bag_number] = {
                "bag_number": bag_number,
                "name": name,
                "domicile": (_text(row[place_col]) if place_col < len(row) else "") or None,
            }
        if not records:
            raise SourceFormatError(f"{path.name}!{sheet_name} produced no insurers")
        log.info("parsed %d insurer names", len(records))
        return list(records.values())
    finally:
        workbook.close()


def _find_header(rows: list[tuple[Any, ...]]) -> int:
    for index, row in enumerate(rows[:30]):
        labels = {_text(c).casefold() for c in row}
        if "nummer" in labels and "name" in labels:
            return index
    raise SourceFormatError("could not find the insurer index header row")


def _insurer_columns(header: tuple[Any, ...]) -> tuple[int, int, int]:
    number_col = name_col = place_col = -1
    for index, cell in enumerate(header):
        label = _text(cell).casefold()
        if label == "nummer":
            number_col = index
        elif label == "name":
            name_col = index
        elif label in {"ort", "sitz"}:
            place_col = index
    if number_col < 0 or name_col < 0:
        raise SourceFormatError(f"insurer index header lacks Nummer/Name: {header}")
    return number_col, name_col, place_col if place_col >= 0 else name_col + 1


def _text(value: Any) -> str:
    if value is None:
        return ""
    return unicodedata.normalize("NFC", str(value)).strip()


def _optional_int(value: Any) -> int | None:
    text = str(value or "").strip()
    if not text.isdigit():
        return None
    return int(text)


__all__ = [
    "EmptySourceFileError",
    "SourceFormatError",
    "commune_and_postal_rows",
    "insurer_rows",
    "normalize_key",
    "premium_rows",
    "region_workbook_year",
    "restriction_rows",
    "tariff_rows",
]
