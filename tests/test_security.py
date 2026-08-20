"""API keys, rate limiting and the access-control middleware."""

from __future__ import annotations

import time
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select

from lamal_api.api.access import SlidingWindowLimiter, UsageBuffer
from lamal_api.api.app import create_app
from lamal_api.config import Settings
from lamal_api.db import models
from lamal_api.security import (
    KEY_PREFIX,
    create_key,
    extract_key,
    generate_key,
    hash_key,
    list_keys,
    revoke_key,
)


class TestKeyGeneration:
    def test_keys_are_prefixed_and_long(self) -> None:
        key, prefix, digest = generate_key()
        assert key.startswith(KEY_PREFIX)
        assert len(key) > 30
        assert prefix == key[:8]
        assert len(digest) == 64

    def test_keys_are_unique(self) -> None:
        keys = {generate_key()[0] for _ in range(500)}
        assert len(keys) == 500

    def test_hash_is_stable_and_one_way(self) -> None:
        key, _, digest = generate_key()
        assert hash_key(key) == digest
        assert key not in digest

    @pytest.mark.parametrize(
        ("header", "authorization", "expected"),
        [
            ("lam_abc", None, "lam_abc"),
            ("  lam_abc  ", None, "lam_abc"),
            (None, "Bearer lam_abc", "lam_abc"),
            (None, "bearer lam_abc", "lam_abc"),
            (None, "Basic lam_abc", None),  # wrong scheme
            (None, None, None),
            ("", "Bearer lam_xyz", "lam_xyz"),  # empty header falls through
        ],
    )
    def test_extract_key(
        self, header: str | None, authorization: str | None, expected: str | None
    ) -> None:
        assert extract_key(header, authorization) == expected


class TestKeyStorage:
    def test_secret_is_never_persisted(self, empty_engine: Engine) -> None:
        key, record = create_key(empty_engine, name="test")
        with empty_engine.connect() as conn:
            rows = conn.execute(select(models.api_key)).all()
        assert len(rows) == 1
        stored = rows[0]._mapping
        assert stored["key_hash"] == hash_key(key)
        assert key not in str(dict(stored))
        assert stored["prefix"] == record.prefix

    def test_name_is_required(self, empty_engine: Engine) -> None:
        with pytest.raises(ValueError, match="needs a name"):
            create_key(empty_engine, name="   ")

    def test_list_hides_revoked_by_default(self, empty_engine: Engine) -> None:
        create_key(empty_engine, name="keep")
        _, gone = create_key(empty_engine, name="drop")
        revoke_key(empty_engine, gone.prefix)

        assert [r.name for r in list_keys(empty_engine)] == ["keep"]
        assert {r.name for r in list_keys(empty_engine, include_revoked=True)} == {"keep", "drop"}

    def test_revoking_an_unknown_prefix_returns_none(self, empty_engine: Engine) -> None:
        assert revoke_key(empty_engine, "lam_nope") is None


class TestSlidingWindowLimiter:
    def test_allows_up_to_the_limit_then_blocks(self) -> None:
        limiter = SlidingWindowLimiter()
        assert all(limiter.check("a", 3) is None for _ in range(3))
        retry_after = limiter.check("a", 3)
        assert retry_after is not None and retry_after > 0

    def test_identities_are_independent(self) -> None:
        limiter = SlidingWindowLimiter()
        for _ in range(3):
            limiter.check("a", 3)
        assert limiter.check("b", 3) is None

    def test_window_expiry_frees_the_budget(self) -> None:
        limiter = SlidingWindowLimiter(window=0.15)
        assert all(limiter.check("a", 2) is None for _ in range(2))
        assert limiter.check("a", 2) is not None
        time.sleep(0.2)
        assert limiter.check("a", 2) is None

    def test_idle_buckets_are_evicted(self) -> None:
        """The old limiter kept one deque per client forever."""
        limiter = SlidingWindowLimiter(window=0.05, sweep_interval=0.05)
        for i in range(200):
            limiter.check(f"client-{i}", 10)
        assert limiter.tracked == 200
        time.sleep(0.12)
        limiter.check("trigger-sweep", 10)
        assert limiter.tracked < 200


class TestUsageBuffer:
    def test_counts_accumulate_before_a_flush(self, empty_engine: Engine) -> None:
        _, record = create_key(empty_engine, name="client")
        buffer = UsageBuffer(interval=3600)
        for _ in range(5):
            buffer.record(record.prefix)
        # Not yet written.
        assert list_keys(empty_engine)[0].request_count == 0
        buffer.maybe_flush(empty_engine, force=True)
        assert list_keys(empty_engine)[0].request_count == 5

    def test_flush_records_last_used(self, empty_engine: Engine) -> None:
        _, record = create_key(empty_engine, name="client")
        buffer = UsageBuffer(interval=3600)
        buffer.record(record.prefix)
        buffer.maybe_flush(empty_engine, force=True)
        assert list_keys(empty_engine)[0].last_used_at is not None

    def test_flushing_nothing_is_harmless(self, empty_engine: Engine) -> None:
        UsageBuffer().maybe_flush(empty_engine, force=True)

    def test_first_sighting_of_a_key_flushes_immediately(self, empty_engine: Engine) -> None:
        """Otherwise a key under active traffic reads as 'never used'."""
        _, record = create_key(empty_engine, name="client")
        buffer = UsageBuffer(interval=3600)  # long interval: only the first-sight rule can fire
        buffer.record(record.prefix)
        buffer.maybe_flush(empty_engine)  # no force
        stored = list_keys(empty_engine)[0]
        assert stored.request_count == 1
        assert stored.last_used_at is not None

    def test_later_requests_are_batched(self, empty_engine: Engine) -> None:
        _, record = create_key(empty_engine, name="client")
        buffer = UsageBuffer(interval=3600)
        buffer.record(record.prefix)
        buffer.maybe_flush(empty_engine)
        for _ in range(4):
            buffer.record(record.prefix)
            buffer.maybe_flush(empty_engine)
        assert list_keys(empty_engine)[0].request_count == 1  # still buffered
        buffer.maybe_flush(empty_engine, force=True)
        assert list_keys(empty_engine)[0].request_count == 5


@pytest.fixture
def auth_client(engine: Engine, settings: Settings) -> Iterator[tuple[TestClient, str]]:
    """A client against an instance that requires a key."""
    key, _ = create_key(engine, name="test client")
    engine.dispose()
    secured = settings.model_copy(update={"auth_enabled": True})
    with TestClient(create_app(secured)) as client:
        yield client, key


PROTECTED = "/v1/premiums?postal_code=1003&birth_year=1990&accident_coverage=false"


class TestAuthDisabledByDefault:
    def test_no_key_needed(self, client: TestClient) -> None:
        """The quickstart must keep working without configuration."""
        assert client.get(PROTECTED).status_code == 200

    def test_an_unexpected_key_is_ignored(self, client: TestClient) -> None:
        assert client.get(PROTECTED, headers={"X-API-Key": "lam_whatever"}).status_code == 200


class TestAuthEnabled:
    def test_request_without_a_key_is_rejected(self, auth_client: tuple[TestClient, str]) -> None:
        client, _ = auth_client
        response = client.get(PROTECTED)
        assert response.status_code == 401
        assert response.json()["error"] == "missing_api_key"
        assert response.headers["WWW-Authenticate"] == "Bearer"

    def test_valid_key_in_header(self, auth_client: tuple[TestClient, str]) -> None:
        client, key = auth_client
        assert client.get(PROTECTED, headers={"X-API-Key": key}).status_code == 200

    def test_valid_key_as_bearer_token(self, auth_client: tuple[TestClient, str]) -> None:
        client, key = auth_client
        response = client.get(PROTECTED, headers={"Authorization": f"Bearer {key}"})
        assert response.status_code == 200

    def test_unknown_key_is_rejected(self, auth_client: tuple[TestClient, str]) -> None:
        client, _ = auth_client
        response = client.get(PROTECTED, headers={"X-API-Key": "lam_not_a_real_key"})
        assert response.status_code == 403
        assert response.json()["error"] == "invalid_api_key"

    def test_health_stays_public(self, auth_client: tuple[TestClient, str]) -> None:
        """The container HEALTHCHECK calls this; requiring a key restart-loops it."""
        client, _ = auth_client
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_docs_require_a_key(self, auth_client: tuple[TestClient, str]) -> None:
        client, key = auth_client
        assert client.get("/openapi.json").status_code == 401
        assert client.get("/openapi.json", headers={"X-API-Key": key}).status_code == 200

    def test_revoked_key_stops_working_without_a_restart(
        self, auth_client: tuple[TestClient, str], settings: Settings
    ) -> None:
        client, key = auth_client
        assert client.get(PROTECTED, headers={"X-API-Key": key}).status_code == 200

        from lamal_api.db.engine import create_db_engine

        engine = create_db_engine(settings)
        prefix = key[:8]
        revoke_key(engine, prefix)
        engine.dispose()

        response = client.get(PROTECTED, headers={"X-API-Key": key})
        assert response.status_code == 403


class TestRateLimiting:
    def test_ip_based_when_auth_is_off(self, engine: Engine, settings: Settings) -> None:
        engine.dispose()
        limited = settings.model_copy(update={"rate_limit_per_minute": 3})
        with TestClient(create_app(limited)) as client:
            codes = [client.get(PROTECTED).status_code for _ in range(5)]
        assert codes[:3] == [200, 200, 200]
        assert codes[3:] == [429, 429]

    def test_health_is_never_limited(self, engine: Engine, settings: Settings) -> None:
        engine.dispose()
        limited = settings.model_copy(update={"rate_limit_per_minute": 2})
        with TestClient(create_app(limited)) as client:
            for _ in range(5):
                client.get(PROTECTED)
            assert client.get("/health").status_code == 200

    def test_429_carries_retry_after(self, engine: Engine, settings: Settings) -> None:
        engine.dispose()
        limited = settings.model_copy(update={"rate_limit_per_minute": 1})
        with TestClient(create_app(limited)) as client:
            client.get(PROTECTED)
            response = client.get(PROTECTED)
        assert response.status_code == 429
        assert int(response.headers["Retry-After"]) > 0
        assert response.json()["error"] == "rate_limited"

    def test_per_key_limit_overrides_the_global_one(
        self, engine: Engine, settings: Settings
    ) -> None:
        """A key with its own budget is not bound by the instance default."""
        generous, _ = create_key(engine, name="generous", rate_limit_per_minute=10)
        stingy, _ = create_key(engine, name="stingy", rate_limit_per_minute=2)
        engine.dispose()
        secured = settings.model_copy(update={"auth_enabled": True, "rate_limit_per_minute": 2})
        with TestClient(create_app(secured)) as client:
            stingy_codes = [
                client.get(PROTECTED, headers={"X-API-Key": stingy}).status_code for _ in range(3)
            ]
            generous_codes = [
                client.get(PROTECTED, headers={"X-API-Key": generous}).status_code for _ in range(5)
            ]
        assert stingy_codes == [200, 200, 429]
        assert generous_codes == [200] * 5

    def test_keys_are_limited_independently(self, engine: Engine, settings: Settings) -> None:
        """The point of keys: one noisy client cannot throttle another."""
        first, _ = create_key(engine, name="first", rate_limit_per_minute=2)
        second, _ = create_key(engine, name="second", rate_limit_per_minute=2)
        engine.dispose()
        secured = settings.model_copy(update={"auth_enabled": True})
        with TestClient(create_app(secured)) as client:
            for _ in range(3):
                client.get(PROTECTED, headers={"X-API-Key": first})
            assert client.get(PROTECTED, headers={"X-API-Key": second}).status_code == 200


class TestUsageTracking:
    def test_requests_are_counted_against_the_key(self, engine: Engine, settings: Settings) -> None:
        key, record = create_key(engine, name="counted")
        engine.dispose()
        secured = settings.model_copy(update={"auth_enabled": True})
        with TestClient(create_app(secured)) as client:
            for _ in range(3):
                client.get(PROTECTED, headers={"X-API-Key": key})
        # The buffer is flushed on shutdown, so counts survive the client closing.
        from lamal_api.db.engine import create_db_engine

        engine2 = create_db_engine(settings)
        stored = next(r for r in list_keys(engine2) if r.prefix == record.prefix)
        engine2.dispose()
        assert stored.request_count == 3
        assert stored.last_used_at is not None
