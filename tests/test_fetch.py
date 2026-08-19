"""Readers, normalisation and source discovery."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from lamal_api.fetch.ckan import Catalog, CkanResource, decode_resource_path
from lamal_api.fetch.normalize import (
    SourceFormatError,
    premium_rows,
    restriction_rows,
    tariff_rows,
)
from lamal_api.fetch.readers import (
    detect_delimiter,
    detect_encoding,
    read_csv_dicts,
    require_columns,
)

FIXTURES = Path(__file__).parent / "fixtures"
PREMIUMS = FIXTURES / "praemien_ch_sample.csv"
TARIFFS = FIXTURES / "tarife_sample.csv"
CATCHMENT = FIXTURES / "einzugsgebiete_sample.csv"


class TestReaders:
    def test_premium_file_is_comma_separated(self) -> None:
        assert detect_delimiter(PREMIUMS, detect_encoding(PREMIUMS)) == ","

    def test_reference_files_are_semicolon_separated(self) -> None:
        """Files from the same dataset use different delimiters, which is why
        the delimiter is sniffed rather than assumed."""
        assert detect_delimiter(TARIFFS, detect_encoding(TARIFFS)) == ";"
        assert detect_delimiter(CATCHMENT, detect_encoding(CATCHMENT)) == ";"

    def test_bom_is_stripped_from_the_first_column_name(self) -> None:
        assert detect_encoding(PREMIUMS) == "utf-8-sig"
        first = next(iter(read_csv_dicts(PREMIUMS)))
        assert "Versicherer" in first
        assert not any(key.startswith("﻿") for key in first)

    def test_umlauts_survive(self) -> None:
        first = next(iter(read_csv_dicts(PREMIUMS)))
        assert "Prämie" in first
        assert "Geschäftsjahr" in first

    def test_ragged_row_is_rejected(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.csv"
        bad.write_text("a,b,c\n1,2,3\n4,5\n", encoding="utf-8")
        with pytest.raises(ValueError, match="expected 3"):
            list(read_csv_dicts(bad))

    def test_require_columns_names_what_is_missing(self) -> None:
        with pytest.raises(ValueError, match="Prämie"):
            require_columns(["Versicherer", "Kanton"], ["Versicherer", "Prämie"], "x.csv")


class TestPremiumNormalisation:
    def test_reads_every_fixture_row(self) -> None:
        """Every data line becomes exactly one record — nothing silently dropped."""
        data_lines = PREMIUMS.read_text(encoding="utf-8-sig").strip().splitlines()[1:]
        assert len(list(premium_rows(PREMIUMS))) == len(data_lines)

    def test_insurer_number_becomes_an_int(self) -> None:
        """The premium file writes 8 while Tarife.csv writes 0008; both must
        land on the same integer or the tables cannot be joined."""
        rows = list(premium_rows(PREMIUMS))
        assert all(isinstance(r["insurer_bag_number"], int) for r in rows)
        tariffs = list(tariff_rows(TARIFFS))
        assert all(isinstance(t["insurer_bag_number"], int) for t in tariffs)
        assert {r["insurer_bag_number"] for r in rows} & {t["insurer_bag_number"] for t in tariffs}

    def test_money_is_stored_as_centimes(self) -> None:
        rows = list(premium_rows(PREMIUMS))
        assert all(isinstance(r["premium_centimes"], int) for r in rows)
        assert all(r["premium_centimes"] > 0 for r in rows)

    def test_franchise_code_is_parsed_to_an_amount(self) -> None:
        rows = list(premium_rows(PREMIUMS))
        assert {r["franchise_chf"] for r in rows} <= {
            0,
            100,
            200,
            300,
            400,
            500,
            600,
            1000,
            1500,
            2000,
            2500,
        }

    def test_standard_franchise_flag_tracks_frast1(self) -> None:
        """isBaseF marks the ordinary franchise and is exactly FRAST1."""
        for row in premium_rows(PREMIUMS):
            assert row["is_standard_franchise"] == (row["franchise_level"] == "FRAST1")

    def test_vocabularies_are_validated(self, tmp_path: Path) -> None:
        text = PREMIUMS.read_text(encoding="utf-8-sig").replace("TAR-HAM", "TAR-WAT", 1)
        broken = tmp_path / "broken.csv"
        broken.write_text(text, encoding="utf-8-sig")
        with pytest.raises(SourceFormatError, match="unknown Tariftyp"):
            list(premium_rows(broken))

    def test_unknown_age_class_is_rejected(self, tmp_path: Path) -> None:
        text = PREMIUMS.read_text(encoding="utf-8-sig").replace("AKL-ERW", "AKL-XXX", 1)
        broken = tmp_path / "broken.csv"
        broken.write_text(text, encoding="utf-8-sig")
        with pytest.raises(SourceFormatError, match="unknown Altersklasse"):
            list(premium_rows(broken))


class TestTariffNormalisation:
    def test_age_category_rows_are_dropped(self) -> None:
        """Tarife.csv mixes insurance models (MOD) with age-category labels
        (ALT). Only the models are tariffs."""
        codes = {t["tariff_code"] for t in tariff_rows(TARIFFS)}
        assert not codes & {"E1", "J1", "K1", "K3", "K5"}

    def test_short_tariff_types_are_normalised(self) -> None:
        """Tarife.csv writes HAM where the premium file writes TAR-HAM."""
        types = {t["tariff_type"] for t in tariff_rows(TARIFFS)}
        assert types <= {"TAR-BASE", "TAR-HAM", "TAR-HMO", "TAR-DIV"}

    def test_multilingual_names_are_kept(self) -> None:
        by_code = {(t["insurer_bag_number"], t["tariff_code"]): t for t in tariff_rows(TARIFFS)}
        base = next(t for k, t in by_code.items() if k[1] == "BASE")
        assert base["name_de"] == "Grundversicherung"
        assert base["name_fr"] == "Assurance de base"


class TestRestrictionNormalisation:
    def test_only_restricted_rows_produce_entries(self) -> None:
        rows = list(restriction_rows(CATCHMENT))
        assert {r["tariff_code"] for r in rows} == {"HMO_PLUS"}

    def test_commune_list_is_exploded(self) -> None:
        rows = list(restriction_rows(CATCHMENT))
        assert len(rows) == 60
        communes = {r["bfs_number"] for r in rows}
        assert 401 in communes  # Aefligen is inside the catchment area
        assert 301 not in communes  # Aarberg is not

    def test_insurer_is_unpadded(self) -> None:
        rows = list(restriction_rows(CATCHMENT))
        assert {r["insurer_bag_number"] for r in rows} == {1555}


class TestCkanDiscovery:
    def test_decodes_the_base64_download_path(self) -> None:
        """CKAN resources for this dataset have empty titles; the real file
        name only exists inside the base64 `path` query parameter."""
        url = (
            "https://opendata.bagnet.ch/?r=/download&path=L1ByYWVtaWVuL1Byw6RtaWVuX0NILmNzdg%3D%3D"
        )
        assert decode_resource_path(url) == "/Praemien/Prämien_CH.csv"

    def test_decodes_the_region_and_archive_paths(self) -> None:
        archive_b64 = "L1ByYWVtaWVuL0FyY2hpdl9QcmFlbWllbl8yMDI1LnppcA=="
        cases = {
            "L1ByYWVtaWVuL0Vpbnp1Z3NnZWJpZXRlLmNzdg==": "/Praemien/Einzugsgebiete.csv",
            "L1ByYWVtaWVuL1RhcmlmZS5jc3Y=": "/Praemien/Tarife.csv",
            archive_b64: "/Praemien/Archiv_Praemien_2025.zip",
        }
        for encoded, expected in cases.items():
            url = f"https://opendata.bagnet.ch/?r=/download&path={encoded}"
            assert decode_resource_path(url) == expected

    def test_returns_none_for_a_plain_url(self) -> None:
        assert decode_resource_path("https://example.invalid/file.csv") is None

    def test_catalog_matches_names_ignoring_accents(self) -> None:
        catalog = _catalog(["Prämien_CH.csv", "Tarife.csv", "Archiv_Praemien_2024.zip"])
        assert catalog.premiums_ch.file_name == "Prämien_CH.csv"
        # The ASCII spelling used by the archives resolves to the same file.
        assert catalog.find("Praemien_CH.csv").file_name == "Prämien_CH.csv"

    def test_archive_years_are_discovered(self) -> None:
        catalog = _catalog(
            ["Prämien_CH.csv", "Archiv_Praemien_2024.zip", "Archiv_Praemien_2025.zip"]
        )
        assert catalog.archive_years() == [2024, 2025]
        assert catalog.archive(2025) is not None
        assert catalog.archive(1999) is None

    def test_missing_file_raises_with_the_available_list(self) -> None:
        catalog = _catalog(["Tarife.csv"])
        with pytest.raises(LookupError, match=re.escape("Tarife.csv")):
            catalog.find("Prämien_CH.csv")


def _catalog(names: list[str]) -> Catalog:
    return Catalog(
        dataset_id="health-insurance-premiums",
        dataset_page="https://example.invalid",
        modified=None,
        resources=tuple(
            CkanResource(file_name=n, url=f"https://example.invalid/{n}", format="CSV")
            for n in names
        ),
    )
