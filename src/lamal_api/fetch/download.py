"""Streaming downloads with hashing and retries."""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

_CHUNK = 1 << 20  # 1 MiB
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


@dataclass(frozen=True)
class Download:
    """A file that was fetched from an official source."""

    url: str
    path: Path
    sha256: str
    byte_size: int

    @property
    def file_name(self) -> str:
        return self.path.name


def make_client(*, user_agent: str, timeout: float, ca_bundle: str | None = None) -> httpx.Client:
    """Build the HTTP client used for every federal endpoint.

    ``ca_bundle`` points at a PEM file of trusted roots. It exists for hosts
    behind a TLS-inspecting proxy, where the proxy's root CA is not in the
    bundled certifi store and every federal request would otherwise fail
    certificate verification.
    """
    verify: str | bool = True
    if ca_bundle:
        bundle = Path(ca_bundle)
        if not bundle.is_file():
            raise FileNotFoundError(f"CA_BUNDLE does not exist: {bundle}")
        verify = str(bundle)
        log.info("verifying TLS against custom CA bundle %s", bundle)

    return httpx.Client(
        headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
        timeout=httpx.Timeout(timeout, connect=30.0),
        follow_redirects=True,
        verify=verify,
    )


def download(
    client: httpx.Client,
    url: str,
    dest: Path,
    *,
    attempts: int = 3,
    backoff: float = 2.0,
) -> Download:
    """Download ``url`` into ``dest``, returning its size and SHA-256.

    The hash is what makes :func:`lamal_api.fetch.sync.sync` idempotent: an
    unchanged file is recognised before anything touches the database.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            digest = hashlib.sha256()
            size = 0
            tmp = dest.with_suffix(dest.suffix + ".part")
            with client.stream("GET", url) as response:
                if response.status_code in _RETRY_STATUS:
                    response.read()
                    raise httpx.HTTPStatusError(
                        f"HTTP {response.status_code}", request=response.request, response=response
                    )
                response.raise_for_status()
                with tmp.open("wb") as fh:
                    for chunk in response.iter_bytes(_CHUNK):
                        fh.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
            tmp.replace(dest)
            log.info("downloaded %s (%s bytes) from %s", dest.name, f"{size:,}", url)
            return Download(url=url, path=dest, sha256=digest.hexdigest(), byte_size=size)
        except (httpx.HTTPError, OSError) as exc:
            last_error = exc
            if attempt == attempts:
                break
            wait = backoff ** (attempt - 1)
            log.warning(
                "download failed (attempt %d/%d) for %s: %s — retrying in %.0fs",
                attempt,
                attempts,
                url,
                exc,
                wait,
            )
            time.sleep(wait)

    raise RuntimeError(f"could not download {url}: {last_error}") from last_error
