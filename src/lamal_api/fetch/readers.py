"""Readers for the official CSV and XLSX files.

The federal files are not uniform and their quirks are load-bearing:

* ``Prämien_CH.csv`` and ``Prämien_EU.csv`` are **comma**-separated, while
  ``Einzugsgebiete.csv`` and ``Tarife.csv`` from the *same* dataset are
  **semicolon**-separated. The delimiter is therefore sniffed per file.
* All of them are UTF-8 with a BOM today, but archived years are not
  guaranteed to be, so the encoding is detected rather than assumed.
* The XLSX workbooks put their real header several rows down, under a title
  block, so header rows are located by matching known column labels instead of
  being hard-coded to a row number.
"""

from __future__ import annotations

import codecs
import csv
import logging
import unicodedata
import zipfile
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

log = logging.getLogger(__name__)

csv.field_size_limit(1 << 24)

#: Tried in order; the first that decodes the whole file wins.
_ENCODINGS = ("utf-8-sig", "utf-8", "cp1252", "latin-1")
_DELIMITERS = (";", ",", "\t", "|")


def detect_encoding(path: Path) -> str:
    """Return the first candidate encoding that decodes ``path`` cleanly.

    Uses an incremental decoder so a 22 MB file costs no memory and multi-byte
    sequences straddling a chunk boundary are not mistaken for errors.
    """
    for encoding in _ENCODINGS:
        decoder = codecs.getincrementaldecoder(encoding)()
        try:
            with path.open("rb") as fh:
                while chunk := fh.read(1 << 20):
                    decoder.decode(chunk)
                decoder.decode(b"", final=True)
        except UnicodeDecodeError:
            continue
        return encoding
    # latin-1 maps every byte, so this is unreachable in practice.
    return "latin-1"


def detect_delimiter(path: Path, encoding: str) -> str:
    """Pick the delimiter by counting candidates in the header line."""
    with path.open("r", encoding=encoding, newline="") as fh:
        header = fh.readline()
    if not header:
        raise ValueError(f"{path.name} is empty")
    counts = {d: header.count(d) for d in _DELIMITERS}
    best = max(counts, key=lambda d: counts[d])
    if counts[best] == 0:
        raise ValueError(f"no delimiter found in the header of {path.name}: {header[:120]!r}")
    return best


def read_csv_dicts(path: Path) -> Iterator[dict[str, str]]:
    """Stream a CSV file as dicts, detecting encoding and delimiter.

    Column names are stripped of whitespace and NFC-normalised so that
    ``Prämie`` compares equal regardless of how the umlaut was composed.
    """
    encoding = detect_encoding(path)
    delimiter = detect_delimiter(path, encoding)
    log.debug("reading %s (encoding=%s delimiter=%r)", path.name, encoding, delimiter)

    with path.open("r", encoding=encoding, newline="") as fh:
        reader = csv.reader(fh, delimiter=delimiter)
        try:
            header = [normalize_key(h) for h in next(reader)]
        except StopIteration:
            return
        width = len(header)
        for line_no, row in enumerate(reader, start=2):
            if not any(cell.strip() for cell in row):
                continue
            if len(row) != width:
                raise ValueError(
                    f"{path.name}:{line_no} has {len(row)} fields, expected {width}. "
                    f"The upstream file layout may have changed."
                )
            yield dict(zip(header, row, strict=True))


def normalize_key(value: str) -> str:
    """Normalise a column label for stable lookups."""
    return unicodedata.normalize("NFC", value).strip()


def require_columns(header: Sequence[str], expected: Sequence[str], source: str) -> None:
    """Fail loudly when an upstream file loses a column we depend on."""
    missing = [c for c in expected if c not in header]
    if missing:
        raise ValueError(
            f"{source} is missing expected column(s): {', '.join(missing)}. "
            f"Found: {', '.join(header)}"
        )


def read_xlsx_rows(
    path: Path,
    sheet: str,
    *,
    header_markers: Sequence[str],
    max_header_scan: int = 40,
) -> Iterator[dict[str, Any]]:
    """Stream an XLSX sheet as dicts, locating the header row by content.

    ``header_markers`` are substrings that must all appear somewhere in the
    header row. The federal workbooks prefix their tables with a few rows of
    title and legend text, and they move, so scanning beats a fixed offset.

    Header cells frequently embed a newline to stack the German and French
    label (``"BFS-Nr.\\nNo OFS"``); only the first line is kept as the key.
    """
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        if sheet not in workbook.sheetnames:
            raise ValueError(
                f"{path.name} has no sheet named {sheet!r}; available: {workbook.sheetnames}"
            )
        worksheet = workbook[sheet]
        rows = worksheet.iter_rows(values_only=True)

        header: list[str] | None = None
        for index, row in enumerate(rows):
            if index >= max_header_scan:
                break
            cells = [_header_label(c) for c in row]
            joined = " | ".join(cells).lower()
            if all(marker.lower() in joined for marker in header_markers):
                header = cells
                break
        if header is None:
            raise ValueError(
                f"could not locate the header row in {path.name}!{sheet} "
                f"(looked for {list(header_markers)} in the first {max_header_scan} rows)"
            )

        for row in rows:
            if not any(cell is not None and str(cell).strip() for cell in row):
                continue
            record = {key: value for key, value in zip(header, row, strict=False) if key}
            yield record
    finally:
        workbook.close()


def read_tabular_dicts(
    path: Path,
    *,
    header_markers: Sequence[str],
    sheet: str | None = None,
) -> Iterator[dict[str, str]]:
    """Read a CSV or XLSX table as string dicts.

    The yearly archives do not always ship every file as CSV — 2025, for
    instance, contains ``Tarife.xlsx`` but no ``Tarife.csv`` — so the loaders
    for the small reference tables accept either.

    Monetary files are deliberately **not** routed through here: premiums are
    only ever read from CSV, because a spreadsheet cell hands back a
    :class:`float` and money must not make that round trip.
    """
    suffix = path.suffix.lower()
    if suffix in {".csv", ".txt"}:
        yield from read_csv_dicts(path)
        return
    if suffix not in {".xlsx", ".xlsm"}:
        raise ValueError(f"unsupported table format for {path.name}")

    if sheet is None:
        workbook = load_workbook(path, read_only=True, data_only=True)
        try:
            sheet = workbook.sheetnames[0]
        finally:
            workbook.close()

    for record in read_xlsx_rows(path, sheet, header_markers=header_markers):
        yield {key: _as_text(value) for key, value in record.items()}


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return unicodedata.normalize("NFC", str(value)).strip()


def read_xlsx_cell_texts(path: Path, sheet: str, *, limit: int = 40) -> list[str]:
    """Return the first column's text from the top of a sheet.

    Used to read the validity statement out of the premium-region workbook.
    """
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        if sheet not in workbook.sheetnames:
            return []
        out: list[str] = []
        for index, row in enumerate(workbook[sheet].iter_rows(values_only=True)):
            if index >= limit:
                break
            for cell in row:
                if cell is not None and str(cell).strip():
                    out.append(str(cell).strip())
        return out
    finally:
        workbook.close()


def _header_label(cell: Any) -> str:
    """First line of a possibly multi-lingual, multi-line header cell."""
    if cell is None:
        return ""
    text = unicodedata.normalize("NFC", str(cell)).strip()
    return text.splitlines()[0].strip() if text else ""


def extract_from_zip(archive: Path, member_candidates: Sequence[str], dest_dir: Path) -> Path:
    """Extract the first matching member from a yearly archive.

    Names are matched case- and accent-insensitively because the archives were
    produced over more than a decade on different systems.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as zf:
        index = {_fold(info.filename): info for info in zf.infolist() if not info.is_dir()}
        for candidate in member_candidates:
            info = index.get(_fold(candidate))
            if info is None:
                continue
            target = dest_dir / Path(info.filename).name
            with zf.open(info) as src, target.open("wb") as dst:
                while chunk := src.read(1 << 20):
                    dst.write(chunk)
            return target
    raise FileNotFoundError(
        f"{archive.name} contains none of {list(member_candidates)}; "
        f"it holds: {[i.filename for i in zipfile.ZipFile(archive).infolist()]}"
    )


#: Umlauts are transliterated the German way so ``Prämien_CH.csv`` and
#: ``Praemien_CH.csv`` — both of which occur across the yearly archives — fold
#: to the same key.
_UMLAUT_MAP = str.maketrans(
    {"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss"}
)


def _fold(name: str) -> str:
    """Case- and accent-folded basename, for tolerant file matching."""
    base = unicodedata.normalize("NFC", Path(name).name).translate(_UMLAUT_MAP)
    decomposed = unicodedata.normalize("NFKD", base)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return stripped.casefold()
