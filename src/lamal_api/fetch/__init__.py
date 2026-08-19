"""Discovery, download and loading of the official FOPH/BAG premium data."""

from __future__ import annotations

from .ckan import Catalog, CkanResource, decode_resource_path, fetch_catalog
from .sync import SyncReport, YearResult, peek_premium_year, sync

__all__ = [
    "Catalog",
    "CkanResource",
    "SyncReport",
    "YearResult",
    "decode_resource_path",
    "fetch_catalog",
    "peek_premium_year",
    "sync",
]
