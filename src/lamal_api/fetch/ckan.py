"""Discovery of the FOPH premium files through the opendata.swiss CKAN API.

Download URLs are never hard-coded. They are read from the CKAN package at
request time, which is what lets the fetcher survive the annual reshuffle and
pick up a new premium year on its own.

One wrinkle worth knowing: the CKAN resources for this dataset carry **empty
titles**, and their URLs look like::

    https://opendata.bagnet.ch/?r=/download&path=L1ByYWVtaWVuL1Byw6RtaWVuX0NILmNzdg%3D%3D

The ``path`` parameter is a base64-encoded server path, so the only reliable
way to tell the files apart is to decode it::

    L1ByYWVtaWVuL1Byw6RtaWVuX0NILmNzdg== -> /Praemien/Prämien_CH.csv
"""

from __future__ import annotations

import base64
import binascii
import logging
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

log = logging.getLogger(__name__)

CKAN_API = "https://ckan.opendata.swiss/api/3/action"
DATASET_ID = "health-insurance-premiums"

#: opendata.swiss landing page, cited in ``/v1/meta`` and the README.
DATASET_PAGE = f"https://opendata.swiss/en/dataset/{DATASET_ID}"

_ARCHIVE_RE = re.compile(r"archiv_praemien_(\d{4})\.zip$", re.IGNORECASE)


@dataclass(frozen=True)
class CkanResource:
    """One downloadable file advertised by the CKAN package."""

    file_name: str
    url: str
    format: str
    decoded_path: str | None = None
    issued: str | None = None
    modified: str | None = None


@dataclass(frozen=True)
class Catalog:
    """The resolved set of files for the premium dataset."""

    dataset_id: str
    dataset_page: str
    modified: str | None
    resources: tuple[CkanResource, ...]

    def find(self, *names: str) -> CkanResource:
        """Return the resource whose file name matches any of ``names``."""
        wanted = {_fold(n) for n in names}
        for resource in self.resources:
            if _fold(resource.file_name) in wanted:
                return resource
        raise LookupError(
            f"none of {list(names)} found in the CKAN dataset. "
            f"Available: {sorted(r.file_name for r in self.resources)}"
        )

    def find_optional(self, *names: str) -> CkanResource | None:
        try:
            return self.find(*names)
        except LookupError:
            return None

    def archive(self, year: int) -> CkanResource | None:
        """The yearly archive ZIP for ``year``, if published."""
        return self.find_optional(f"Archiv_Praemien_{year}.zip")

    def archive_years(self) -> list[int]:
        """Premium years available as archives, oldest first."""
        years = []
        for resource in self.resources:
            match = _ARCHIVE_RE.search(resource.file_name)
            if match:
                years.append(int(match.group(1)))
        return sorted(years)

    # Canonical files of the live (current-year) release.
    @property
    def premiums_ch(self) -> CkanResource:
        return self.find("Prämien_CH.csv", "Praemien_CH.csv")

    @property
    def premiums_eu(self) -> CkanResource:
        return self.find("Prämien_EU.csv", "Praemien_EU.csv")

    @property
    def tariffs(self) -> CkanResource:
        return self.find("Tarife.csv")

    @property
    def catchment_areas(self) -> CkanResource:
        return self.find("Einzugsgebiete.csv")


def fetch_catalog(client: httpx.Client, dataset_id: str = DATASET_ID) -> Catalog:
    """Resolve the dataset's current download URLs from CKAN."""
    response = client.get(f"{CKAN_API}/package_show", params={"id": dataset_id})
    response.raise_for_status()
    payload = response.json()
    if not payload.get("success"):
        raise RuntimeError(f"CKAN rejected package_show for {dataset_id!r}: {payload}")
    result = payload["result"]

    resources = tuple(_parse_resource(r) for r in result.get("resources", []))
    if not resources:
        raise RuntimeError(f"CKAN package {dataset_id!r} advertises no resources")

    catalog = Catalog(
        dataset_id=dataset_id,
        dataset_page=DATASET_PAGE,
        modified=_first_str(result.get("modified")),
        resources=resources,
    )
    log.info(
        "CKAN catalog resolved: %d resources, dataset modified %s",
        len(resources),
        catalog.modified,
    )
    return catalog


def _parse_resource(raw: dict[str, object]) -> CkanResource:
    url = str(raw.get("download_url") or raw.get("url") or "")
    decoded = decode_resource_path(url)
    file_name = Path(decoded).name if decoded else _name_from_url(url, raw)
    return CkanResource(
        file_name=file_name,
        url=url,
        format=str(raw.get("format") or "").upper(),
        decoded_path=decoded,
        issued=_first_str(raw.get("issued")),
        modified=_first_str(raw.get("modified")),
    )


def decode_resource_path(url: str) -> str | None:
    """Decode the base64 ``path`` query parameter of a bagnet download URL.

    Returns ``None`` when the URL does not use that scheme, so callers can fall
    back to the URL basename.
    """
    values = parse_qs(urlparse(url).query).get("path")
    if not values:
        return None
    raw = values[0]
    raw += "=" * (-len(raw) % 4)  # restore stripped base64 padding
    try:
        return unicodedata.normalize("NFC", base64.b64decode(raw).decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, ValueError):
        log.debug("could not base64-decode resource path %r", values[0])
        return None


def _name_from_url(url: str, raw: dict[str, object]) -> str:
    name = Path(urlparse(url).path).name
    if name:
        return name
    title = raw.get("title") or raw.get("name")
    if isinstance(title, dict):
        for value in title.values():
            if value:
                return str(value)
    return str(title or "unknown")


def _first_str(value: object) -> str | None:
    """CKAN returns either a plain string or a per-language mapping."""
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    if isinstance(value, dict):
        for key in ("de", "fr", "it", "en"):
            candidate = value.get(key)
            if candidate:
                return str(candidate)
        for candidate in value.values():
            if candidate:
                return str(candidate)
    return str(value)


#: The federal files use both German spellings of the same name — the live
#: resource is ``Prämien_CH.csv`` while the archives call it
#: ``Praemien_CH.csv``. Transliterating umlauts the German way (ä -> ae) makes
#: the two fold to one key; plain accent-stripping would give "pramien" and
#: silently fail to match.
_UMLAUT_MAP = str.maketrans(
    {"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss"}
)


def _fold(name: str) -> str:
    base = unicodedata.normalize("NFC", Path(name).name).translate(_UMLAUT_MAP)
    decomposed = unicodedata.normalize("NFKD", base)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def summarise(resources: Iterable[CkanResource]) -> str:
    """Human-readable listing, used by ``lamal-api info``."""
    return "\n".join(f"  {r.format:<5} {r.file_name}" for r in resources)
