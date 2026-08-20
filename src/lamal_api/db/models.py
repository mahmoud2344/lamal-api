"""Database schema, defined with SQLAlchemy Core.

Core rather than the ORM: this service only ever bulk-inserts and filters, so
the ORM's identity map and lazy loading would buy nothing and cost throughput.
Core also emits portable DDL for both SQLite and PostgreSQL from one
definition.

Design notes
------------
* Money lives in ``premium_centimes`` as an integer. Floats cannot represent
  ``427.85`` exactly and the API promises centime precision.
* ``ix_premium_query`` deliberately ends with ``premium_centimes`` so the main
  endpoint's ``ORDER BY premium ASC`` is satisfied by the index itself.
* Every table that varies by premium year carries ``premium_year`` in its
  primary key, which is what makes a per-year re-sync an atomic
  delete-then-insert inside one transaction.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
)

metadata = MetaData()


# --------------------------------------------------------------------------
# Provenance: one row per official file snapshot that was ingested.
# --------------------------------------------------------------------------
data_source = Table(
    "data_source",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("kind", String(32), nullable=False),
    Column("premium_year", Integer, nullable=True),
    Column("source_url", String(1024), nullable=False),
    Column("file_name", String(255), nullable=False),
    Column("content_sha256", String(64), nullable=False),
    Column("byte_size", BigInteger, nullable=False),
    Column("row_count", Integer, nullable=False),
    Column("published_at", String(64), nullable=True),
    Column("fetched_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("kind", "premium_year", "content_sha256", name="uq_data_source_snapshot"),
)
Index("ix_data_source_kind_year", data_source.c.kind, data_source.c.premium_year)


# --------------------------------------------------------------------------
# Reference data
# --------------------------------------------------------------------------
insurer = Table(
    "insurer",
    metadata,
    Column("bag_number", Integer, primary_key=True),
    Column("name", String(255), nullable=False),
    Column("domicile", String(255), nullable=True),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)


#: API keys, stored as SHA-256 hashes — the secret itself is never persisted.
#: Only used when the operator enables authentication; the table is created
#: unconditionally so turning auth on never needs a migration.
api_key = Table(
    "api_key",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("prefix", String(16), nullable=False, unique=True),
    Column("key_hash", String(64), nullable=False, unique=True),
    Column("name", String(255), nullable=False),
    # NULL means "fall back to the global RATE_LIMIT_PER_MINUTE".
    Column("rate_limit_per_minute", Integer, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("last_used_at", DateTime(timezone=True), nullable=True),
    Column("request_count", BigInteger, nullable=False, server_default="0"),
    Column("revoked_at", DateTime(timezone=True), nullable=True),
)
Index("ix_api_key_hash", api_key.c.key_hash)


commune = Table(
    "commune",
    metadata,
    Column("premium_year", Integer, primary_key=True),
    Column("bfs_number", Integer, primary_key=True),
    Column("name", String(255), nullable=False),
    Column("canton", String(2), nullable=False),
    Column("district", String(255), nullable=True),
    Column("region", String(16), nullable=False),
)
Index("ix_commune_canton", commune.c.premium_year, commune.c.canton)
Index("ix_commune_name", commune.c.premium_year, commune.c.name)


# A postal code can span several communes and a commune can hold several
# postal codes, so this is a genuine many-to-many bridge, not a lookup.
postal_code_commune = Table(
    "postal_code_commune",
    metadata,
    Column("premium_year", Integer, primary_key=True),
    Column("postal_code", Integer, primary_key=True),
    Column("locality", String(255), primary_key=True),
    Column("bfs_number", Integer, primary_key=True),
)
Index(
    "ix_pcc_lookup",
    postal_code_commune.c.premium_year,
    postal_code_commune.c.postal_code,
)


tariff = Table(
    "tariff",
    metadata,
    Column("premium_year", Integer, primary_key=True),
    Column("insurer_bag_number", Integer, primary_key=True),
    Column("tariff_code", String(64), primary_key=True),
    Column("tariff_type", String(16), nullable=False),
    Column("name_de", String(255), nullable=True),
    Column("name_fr", String(255), nullable=True),
    Column("name_it", String(255), nullable=True),
    Column("sort_order", Integer, nullable=True),
)


# Alternative models are sometimes sold only in a named list of communes
# (``Einzugsgebiete.csv`` with ``Eingeschränkt = Y``). The comma-separated BFS
# list is exploded into one row per commune so it can be joined, not parsed.
tariff_restriction = Table(
    "tariff_restriction",
    metadata,
    Column("premium_year", Integer, primary_key=True),
    Column("insurer_bag_number", Integer, primary_key=True),
    Column("canton", String(2), primary_key=True),
    Column("region", String(16), primary_key=True),
    Column("tariff_code", String(64), primary_key=True),
    Column("bfs_number", Integer, primary_key=True),
)
Index(
    "ix_restriction_tariff",
    tariff_restriction.c.premium_year,
    tariff_restriction.c.insurer_bag_number,
    tariff_restriction.c.canton,
    tariff_restriction.c.region,
    tariff_restriction.c.tariff_code,
)


# --------------------------------------------------------------------------
# The premium tables
# --------------------------------------------------------------------------
premium = Table(
    "premium",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("premium_year", Integer, nullable=False),
    Column("survey_year", Integer, nullable=False),
    Column("insurer_bag_number", Integer, nullable=False),
    Column("canton", String(2), nullable=False),
    Column("region", String(16), nullable=False),
    Column("age_class", String(8), nullable=False),
    Column("age_subgroup", String(4), nullable=False, server_default=""),
    Column("accident", String(8), nullable=False),
    Column("tariff_code", String(64), nullable=False),
    Column("tariff_type", String(16), nullable=False),
    Column("tariff_label", String(255), nullable=False),
    Column("franchise_chf", Integer, nullable=False),
    Column("franchise_level", String(8), nullable=False),
    Column("premium_centimes", Integer, nullable=False),
    Column("is_standard_franchise", Boolean, nullable=False),
    UniqueConstraint(
        "premium_year",
        "insurer_bag_number",
        "canton",
        "region",
        "age_class",
        "accident",
        "tariff_code",
        "age_subgroup",
        "franchise_chf",
        name="uq_premium_natural_key",
    ),
)

# The main endpoint's exact filter set, with the sort column last.
Index(
    "ix_premium_query",
    premium.c.premium_year,
    premium.c.canton,
    premium.c.region,
    premium.c.age_class,
    premium.c.franchise_chf,
    premium.c.accident,
    premium.c.age_subgroup,
    premium.c.premium_centimes,
)
Index("ix_premium_insurer", premium.c.premium_year, premium.c.insurer_bag_number)
Index("ix_premium_type", premium.c.premium_year, premium.c.tariff_type)


# EU/EFTA cross-border premiums are keyed by country and have no canton, no
# regional split and only the standard model, so they get their own table
# rather than nullable columns on ``premium``.
premium_eu = Table(
    "premium_eu",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("premium_year", Integer, nullable=False),
    Column("survey_year", Integer, nullable=False),
    Column("insurer_bag_number", Integer, nullable=False),
    Column("country", String(8), nullable=False),
    Column("region", String(16), nullable=False),
    Column("age_class", String(8), nullable=False),
    Column("age_subgroup", String(4), nullable=False, server_default=""),
    Column("accident", String(8), nullable=False),
    Column("tariff_code", String(64), nullable=False),
    Column("tariff_type", String(16), nullable=False),
    Column("tariff_label", String(255), nullable=False),
    Column("franchise_chf", Integer, nullable=False),
    Column("franchise_level", String(8), nullable=False),
    Column("premium_centimes", Integer, nullable=False),
    Column("is_standard_franchise", Boolean, nullable=False),
    UniqueConstraint(
        "premium_year",
        "insurer_bag_number",
        "country",
        "age_class",
        "accident",
        "tariff_code",
        "age_subgroup",
        "franchise_chf",
        name="uq_premium_eu_natural_key",
    ),
)
Index(
    "ix_premium_eu_query",
    premium_eu.c.premium_year,
    premium_eu.c.country,
    premium_eu.c.age_class,
    premium_eu.c.franchise_chf,
    premium_eu.c.accident,
    premium_eu.c.age_subgroup,
    premium_eu.c.premium_centimes,
)


#: Tables wiped and rewritten per premium year on every sync.
YEAR_SCOPED_TABLES = (
    premium,
    premium_eu,
    commune,
    postal_code_commune,
    tariff,
    tariff_restriction,
)
