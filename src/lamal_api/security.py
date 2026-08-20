"""API key generation, hashing and storage.

This service exposes **public federal open data**: there is nothing
confidential behind a key, no writes and no user accounts. Keys are therefore
not a security boundary. They exist so that an operator can

* protect their own bandwidth and CPU from a runaway client,
* tell which client is generating the load, and
* rate-limit per *client* rather than per IP — IP-based limits punish everyone
  behind a shared NAT for one caller's traffic.

Keys are stored as SHA-256 hashes and shown exactly once, at creation. A
leaked database therefore hands over no usable key. Plain SHA-256 is the right
choice here rather than a slow password hash: these are 32-character random
secrets, not human-chosen passwords, so there is nothing to brute-force, and
the digest is computed on every request.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import Engine, select, update
from sqlalchemy.engine import Connection

from .db import models

#: Distinguishes our keys in logs and config files, and makes an accidentally
#: committed key greppable by secret scanners.
KEY_PREFIX = "lam_"

#: How much of the key is stored in clear for display. Four random characters
#: after the prefix is enough to tell keys apart without being useful alone.
DISPLAY_PREFIX_LENGTH = len(KEY_PREFIX) + 4

#: 24 random bytes -> 32 URL-safe characters, ~192 bits of entropy.
_TOKEN_BYTES = 24


@dataclass(frozen=True)
class ApiKeyRecord:
    """A stored key, without the secret itself."""

    prefix: str
    name: str
    rate_limit_per_minute: int | None
    created_at: datetime
    last_used_at: datetime | None
    request_count: int
    revoked_at: datetime | None

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None


def generate_key() -> tuple[str, str, str]:
    """Mint a new key.

    :returns: ``(key, display_prefix, key_hash)``. The key is the only copy —
        it is never recoverable from the database.
    """
    key = f"{KEY_PREFIX}{secrets.token_urlsafe(_TOKEN_BYTES)}"
    return key, key[:DISPLAY_PREFIX_LENGTH], hash_key(key)


def hash_key(key: str) -> str:
    """SHA-256 of the key, hex encoded."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def extract_key(header_value: str | None, authorization: str | None) -> str | None:
    """Pull the key out of ``X-API-Key`` or ``Authorization: Bearer ...``."""
    if header_value and header_value.strip():
        return header_value.strip()
    if authorization:
        scheme, _, credentials = authorization.partition(" ")
        if scheme.lower() == "bearer" and credentials.strip():
            return credentials.strip()
    return None


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------


def _to_record(row: object) -> ApiKeyRecord:
    m = row._mapping  # type: ignore[attr-defined]
    return ApiKeyRecord(
        prefix=m["prefix"],
        name=m["name"],
        rate_limit_per_minute=m["rate_limit_per_minute"],
        created_at=m["created_at"],
        last_used_at=m["last_used_at"],
        request_count=m["request_count"],
        revoked_at=m["revoked_at"],
    )


def create_key(
    engine: Engine, *, name: str, rate_limit_per_minute: int | None = None
) -> tuple[str, ApiKeyRecord]:
    """Create and store a key. The returned secret is shown only this once."""
    if not name.strip():
        raise ValueError("an API key needs a name, so you can tell them apart later")
    if rate_limit_per_minute is not None and rate_limit_per_minute < 0:
        raise ValueError("rate_limit_per_minute cannot be negative")

    key, prefix, key_hash = generate_key()
    now = datetime.now(UTC)
    with engine.begin() as conn:
        conn.execute(
            models.api_key.insert().values(
                prefix=prefix,
                key_hash=key_hash,
                name=name.strip(),
                rate_limit_per_minute=rate_limit_per_minute,
                created_at=now,
                last_used_at=None,
                request_count=0,
                revoked_at=None,
            )
        )
    return key, ApiKeyRecord(
        prefix=prefix,
        name=name.strip(),
        rate_limit_per_minute=rate_limit_per_minute,
        created_at=now,
        last_used_at=None,
        request_count=0,
        revoked_at=None,
    )


def list_keys(engine: Engine, *, include_revoked: bool = False) -> list[ApiKeyRecord]:
    stmt = select(models.api_key).order_by(models.api_key.c.created_at)
    if not include_revoked:
        stmt = stmt.where(models.api_key.c.revoked_at.is_(None))
    with engine.connect() as conn:
        return [_to_record(row) for row in conn.execute(stmt)]


def revoke_key(engine: Engine, prefix: str) -> ApiKeyRecord | None:
    """Revoke by display prefix. Takes effect on the next request, no restart."""
    now = datetime.now(UTC)
    with engine.begin() as conn:
        row = conn.execute(select(models.api_key).where(models.api_key.c.prefix == prefix)).first()
        if row is None:
            return None
        record = _to_record(row)
        if record.revoked_at is None:
            conn.execute(
                update(models.api_key)
                .where(models.api_key.c.prefix == prefix)
                .values(revoked_at=now)
            )
    return record


def authenticate(conn: Connection, key: str) -> ApiKeyRecord | None:
    """Resolve a presented key to an active record, or ``None``.

    The lookup is by hash, so the secret never has to be compared in Python
    and never appears in a query log in clear.
    """
    row = conn.execute(
        select(models.api_key).where(
            models.api_key.c.key_hash == hash_key(key),
            models.api_key.c.revoked_at.is_(None),
        )
    ).first()
    return _to_record(row) if row is not None else None


def flush_usage(engine: Engine, counts: dict[str, int], seen_at: datetime) -> None:
    """Persist buffered request counts.

    Usage is accumulated in memory and written periodically rather than once
    per request: a write on every call would turn a read-only service into a
    write-heavy one and serialise every request behind SQLite's writer lock.
    """
    if not counts:
        return
    with engine.begin() as conn:
        for prefix, count in counts.items():
            conn.execute(
                update(models.api_key)
                .where(models.api_key.c.prefix == prefix)
                .values(
                    request_count=models.api_key.c.request_count + count,
                    last_used_at=seen_at,
                )
            )
