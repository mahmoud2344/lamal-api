"""Discovery of the two files that are *not* on opendata.swiss.

The CKAN premium dataset does not contain:

1. **the commune/postal-code to premium-region mapping** — without it a postal
   code cannot be turned into a premium region at all;
2. **insurer names** — the premium file only carries BAG numbers.

Both come from the priminfo.admin.ch download section, whose file names are not
stable. In October 2026, for instance, ``/downloads/praemienregionen.xlsx``
became ``/downloads/praemienregionen-2027.xlsx`` and the archived years moved
out of ``/downloads/archiv/praemienregionen/``. So files are found the way a
person would find them: by reading the links on the download pages. Known
address patterns are only a fallback for when those pages cannot be read.
"""

from __future__ import annotations

import logging
import re

import httpx

log = logging.getLogger(__name__)

PRIMINFO_BASE = "https://www.priminfo.admin.ch"
#: Lists the files for the premium year currently on sale.
DOWNLOADS_PAGE = f"{PRIMINFO_BASE}/de/downloads/aktuell"
#: Lists the files for past years.
ARCHIVE_PAGE = f"{PRIMINFO_BASE}/de/downloads/archiv"

_REGIONS_HREF_RE = re.compile(
    r"""href=["']([^"']*praemienregionen[^"']*\.xlsx)["']""",
    re.IGNORECASE,
)
_INSURER_HREF_RE = re.compile(
    r"""href=["']([^"']*zugelassene-krankenversicherer[^"']*\.xlsx)["']""",
    re.IGNORECASE,
)

#: Addresses priminfo has used for one year's region workbook, newest first.
_REGIONS_YEAR_PATTERNS = (
    "/downloads/praemienregionen-{year}.xlsx",  # the current year, since 2026-10
    "/downloads/praemienregionen_{year}.xlsx",  # past years, since 2026-10
    "/downloads/archiv/praemienregionen/praemienregionen_{year}.xlsx",  # until 2026-09
)
#: The undated workbook for the current year, used until 2026-09.
_REGIONS_UNDATED = "/downloads/praemienregionen.xlsx"


def regions_url_for_year(client: httpx.Client, premium_year: int) -> str | None:
    """Find the premium-region workbook that applies to ``premium_year``.

    In order: a workbook for that year linked from the current or archive
    download page; one at a known address for that year; then the workbook for
    the year currently on sale. That last fallback is right more often than not,
    because communes rarely change region, and the sync files a workbook under
    the year it says it is valid for, whatever year it was fetched for.

    Returns ``None`` when nothing can be found, so the caller can say so.
    """
    current = _links(client, DOWNLOADS_PAGE, _REGIONS_HREF_RE)
    archived = _links(client, ARCHIVE_PAGE, _REGIONS_HREF_RE)
    for href in (*current, *archived):
        if str(premium_year) in href:
            return href

    for pattern in _REGIONS_YEAR_PATTERNS:
        candidate = PRIMINFO_BASE + pattern.format(year=premium_year)
        if _exists(client, candidate):
            return candidate

    if current:
        log.info("no region workbook for %d on priminfo; using %s", premium_year, current[0])
        return current[0]
    undated = PRIMINFO_BASE + _REGIONS_UNDATED
    return undated if _exists(client, undated) else None


def find_insurer_directory_url(client: httpx.Client, premium_year: int) -> str | None:
    """Locate the approved-insurer directory workbook.

    Insurer names are *enrichment*: the API is fully functional without them,
    returning BAG numbers alone. So a failure here is logged and swallowed
    rather than aborting the whole sync.
    """
    direct = f"{PRIMINFO_BASE}/downloads/zugelassene-krankenversicherer-{premium_year}-01-01.xlsx"
    if _exists(client, direct):
        return direct

    matches = _links(client, DOWNLOADS_PAGE, _INSURER_HREF_RE)
    if not matches:
        log.warning("no insurer directory link found on %s", DOWNLOADS_PAGE)
        return None

    # Prefer a link that mentions the target year, else take the newest.
    for href in matches:
        if str(premium_year) in href:
            return href
    return sorted(matches)[-1]


def _links(client: httpx.Client, page: str, pattern: re.Pattern[str]) -> list[str]:
    """Absolute URLs of the links on ``page`` matching ``pattern``, in page order."""
    try:
        response = client.get(page)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("could not read %s: %s", page, exc)
        return []
    return list(dict.fromkeys(_absolute(href) for href in pattern.findall(response.text)))


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
