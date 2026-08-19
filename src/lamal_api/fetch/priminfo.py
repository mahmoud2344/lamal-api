"""Discovery of the two files that are *not* on opendata.swiss.

The CKAN premium dataset does not contain:

1. **the commune/postal-code to premium-region mapping** — without it a postal
   code cannot be turned into a premium region at all;
2. **insurer names** — the premium file only carries BAG numbers.

Both come from the priminfo.admin.ch download section. The region workbook sits
at a stable URL; the insurer directory is date-stamped per year, so it is
resolved by pattern first and by scraping the download page as a fallback.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

PRIMINFO_BASE = "https://www.priminfo.admin.ch"
DOWNLOADS_PAGE = f"{PRIMINFO_BASE}/de/downloads/aktuell"

#: Premium regions valid for the current premium year.
REGIONS_URL = f"{PRIMINFO_BASE}/downloads/praemienregionen.xlsx"

#: Archived region workbooks, e.g. .../praemienregionen_2018.xls
REGIONS_ARCHIVE_TEMPLATE = (
    f"{PRIMINFO_BASE}/downloads/archiv/praemienregionen/praemienregionen_{{year}}.{{ext}}"
)

_INSURER_HREF_RE = re.compile(
    r"""href=["']([^"']*zugelassene-krankenversicherer[^"']*\.xlsx)["']""",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PriminfoSources:
    """Resolved priminfo URLs for one sync run."""

    regions_url: str
    insurers_url: str | None


def resolve(client: httpx.Client, premium_year: int) -> PriminfoSources:
    """Resolve the priminfo files needed for ``premium_year``."""
    return PriminfoSources(
        regions_url=REGIONS_URL,
        insurers_url=find_insurer_directory_url(client, premium_year),
    )


def find_insurer_directory_url(client: httpx.Client, premium_year: int) -> str | None:
    """Locate the approved-insurer directory workbook.

    Insurer names are *enrichment*: the API is fully functional without them,
    returning BAG numbers alone. So a failure here is logged and swallowed
    rather than aborting the whole sync.
    """
    direct = f"{PRIMINFO_BASE}/downloads/zugelassene-krankenversicherer-{premium_year}-01-01.xlsx"
    if _exists(client, direct):
        return direct

    try:
        response = client.get(DOWNLOADS_PAGE)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("could not read the priminfo download page: %s", exc)
        return None

    matches = _INSURER_HREF_RE.findall(response.text)
    if not matches:
        log.warning("no insurer directory link found on %s", DOWNLOADS_PAGE)
        return None

    # Prefer a link that mentions the target year, else take the newest.
    for href in matches:
        if str(premium_year) in href:
            return _absolute(href)
    return _absolute(sorted(matches)[-1])


def regions_url_for_year(client: httpx.Client, premium_year: int) -> str:
    """Region workbook for a given year, falling back to the live file.

    The live workbook always describes the year currently on sale. Older years
    live under ``/downloads/archiv/praemienregionen/``, published as ``.xlsx``
    for recent years and ``.xls`` further back; only ``.xlsx`` can be read, so
    an ``.xls``-only year falls back to the current mapping.
    """
    candidate = REGIONS_ARCHIVE_TEMPLATE.format(year=premium_year, ext="xlsx")
    if _exists(client, candidate):
        return candidate
    return REGIONS_URL


def _exists(client: httpx.Client, url: str) -> bool:
    try:
        response = client.head(url)
        if response.status_code == 405:  # server dislikes HEAD; probe with a ranged GET
            response = client.get(url, headers={"Range": "bytes=0-0"})
        return response.status_code < 400
    except httpx.HTTPError:
        return False


def _absolute(href: str) -> str:
    if href.startswith("http"):
        return href
    return f"{PRIMINFO_BASE}/{href.lstrip('/')}"
