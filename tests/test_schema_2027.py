"""Reading the renamed codes the FOPH uses from premium year 2027.

The rows under test are the real 2026 fixtures rewritten in the 2027 layout (see
``conftest.write_2027_layout``). Apart from the tariff type, which the FOPH
reclassified rather than renamed, loading either file must produce identical
records. The golden priminfo tests additionally run end to end on both layouts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from conftest import (
    FILES_2026,
    HEADER_2027_CATCHMENT,
    HEADER_2027_PREMIUMS_CH,
    HEADER_2027_TARIFFS,
    SourceFiles,
)
from lamal_api.domain.codes import Territory
from lamal_api.fetch import vocabulary
from lamal_api.fetch.normalize import (
    EmptySourceFileError,
    SourceFormatError,
    premium_rows,
    restriction_rows,
    tariff_rows,
)
from lamal_api.fetch.readers import detect_delimiter, detect_encoding
from lamal_api.fetch.sync import peek_premium_year


def _without(rows: Any, *fields: str) -> list[dict[str, Any]]:
    return [{k: v for k, v in row.items() if k not in fields} for row in rows]


class TestVocabulary:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("AKA_01_KIN", "AKL-KIN"),
            ("AKA_02_JUG", "AKL-JUG"),
            ("AKA_03_ERW", "AKL-ERW"),
            ("AKL-ERW", "AKL-ERW"),
        ],
    )
    def test_age_class(self, raw: str, expected: str) -> None:
        assert vocabulary.age_class(raw) == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("MIT_UNF", "MIT-UNF"), ("OHN_UNF", "OHN-UNF"), ("OHN-UNF", "OHN-UNF")],
    )
    def test_accident(self, raw: str, expected: str) -> None:
        assert vocabulary.accident(raw) == expected

    def test_region_takes_its_territory_from_the_file(self) -> None:
        """PR_REG_0 no longer says CH or EU; the file being read decides."""
        assert vocabulary.region("PR_REG_3", Territory.SWITZERLAND) == "PR-REG CH3"
        assert vocabulary.region("PR_REG_0", Territory.EU_EFTA) == "PR-REG EU0"
        assert vocabulary.region("PR-REG CH2", Territory.SWITZERLAND) == "PR-REG CH2"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("E1", ""), ("J1", ""), ("", ""), ("K1", "K1"), ("K2", "K2"), ("K5", "K5")],
    )
    def test_base_adult_subgroups_become_empty(self, raw: str, expected: str) -> None:
        """Adults are looked up by an empty subgroup, so E1 left as-is would make
        every adult query come back empty."""
        assert vocabulary.age_subgroup(raw) == expected

    def test_other_adult_subgroups_are_not_collapsed(self) -> None:
        """E2 must not be mistaken for the ordinary adult rate."""
        assert vocabulary.age_subgroup("E2") == "E2"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("FRASTU_01", "FRAST1"), ("FRASTU_07", "FRAST7"), ("FRAST6", "FRAST6")],
    )
    def test_franchise_level(self, raw: str, expected: str) -> None:
        assert vocabulary.franchise_level(raw) == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("FRA_01_E_0300", 300),
            ("FRA_06_E_2500", 2500),
            ("FRA_01_K_0000", 0),
            ("FRA_07_K_0600", 600),
            ("FRA-2500", 2500),
            ("FRA-0", 0),
        ],
    )
    def test_franchise_amount(self, raw: str, expected: int) -> None:
        assert vocabulary.franchise_amount(raw) == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("BASE", "TAR-BASE"),
            ("TAR-BASE", "TAR-BASE"),
            ("HAM", "TAR-HAM"),
            ("TAR-DIV", "TAR-DIV"),
            ("PRAXIS", "TAR-PRAXIS"),
            ("FLEX", "TAR-FLEX"),
            ("TEL_DIG", "TAR-TEL_DIG"),
            ("PHARM", "TAR-PHARM"),
        ],
    )
    def test_tariff_types_keep_their_own_classification(self, raw: str, expected: str) -> None:
        assert vocabulary.tariff_type(raw) == expected

    @pytest.mark.parametrize(("raw", "expected"), [("AT", "EU AT"), ("EU AT", "EU AT")])
    def test_country(self, raw: str, expected: str) -> None:
        assert vocabulary.country(raw) == expected

    @pytest.mark.parametrize(
        ("translate", "raw"),
        [
            (vocabulary.age_class, "AKA_04_SEN"),
            (vocabulary.accident, "MIT UNF"),
            (vocabulary.age_subgroup, "X1"),
            (vocabulary.franchise_level, "FRA_01"),
            (vocabulary.franchise_amount, "FRA_01_E"),
            (vocabulary.tariff_type, "TELMED"),
            (vocabulary.country, "Austria"),
        ],
    )
    def test_unknown_codes_are_rejected(self, translate: Any, raw: str) -> None:
        with pytest.raises(vocabulary.UnknownCodeError, match=raw):
            translate(raw)

    def test_unknown_region_is_rejected(self) -> None:
        with pytest.raises(vocabulary.UnknownCodeError, match="Region"):
            vocabulary.region("PR_REG_A", Territory.SWITZERLAND)


class TestLayoutFixture:
    """Guards the rewritten fixture itself, so the equivalence below means something."""

    def test_uses_the_published_2027_headers(self, files_2027: SourceFiles) -> None:
        for path, header in (
            (files_2027.premiums, HEADER_2027_PREMIUMS_CH),
            (files_2027.tariffs, HEADER_2027_TARIFFS),
            (files_2027.catchment, HEADER_2027_CATCHMENT),
        ):
            assert path.read_text(encoding="utf-8-sig").splitlines()[0] == header

    def test_codes_are_really_in_the_new_spelling(self, files_2027: SourceFiles) -> None:
        text = files_2027.premiums.read_text(encoding="utf-8-sig")
        assert "AKL-" not in text
        assert "PR-REG" not in text
        assert "FRAST" in text and "FRAST1," not in text
        assert ",E1," in text  # the sample has no young adults, so no J1
        assert "TAR-" not in text

    def test_reference_files_switched_to_commas(self, files_2027: SourceFiles) -> None:
        for path in (files_2027.tariffs, files_2027.catchment):
            assert detect_delimiter(path, detect_encoding(path)) == ","


class TestLoadersReadBothLayouts:
    def test_premiums_are_identical_apart_from_tariff_type(self, files_2027: SourceFiles) -> None:
        old = list(premium_rows(FILES_2026.premiums))
        new = list(premium_rows(files_2027.premiums))
        assert old
        assert _without(new, "tariff_type") == _without(old, "tariff_type")

    def test_premium_tariff_types_come_from_the_2027_classification(
        self, files_2027: SourceFiles
    ) -> None:
        types = {r["tariff_type"] for r in premium_rows(files_2027.premiums)}
        assert types == {"TAR-BASE", "TAR-PRAXIS", "TAR-TEL_DIG"}

    def test_tariffs_are_identical_apart_from_type_and_sort_order(
        self, files_2027: SourceFiles
    ) -> None:
        """2027 drops Sort.-Nr.; the ALT rows now carry AKA_* codes and are
        still skipped."""
        old = list(tariff_rows(FILES_2026.tariffs))
        new = list(tariff_rows(files_2027.tariffs))
        assert _without(new, "tariff_type", "sort_order") == _without(
            old, "tariff_type", "sort_order"
        )
        assert all(r["sort_order"] is None for r in new)

    def test_restrictions_are_identical(self, files_2027: SourceFiles) -> None:
        """Region is the join key between a restriction and a premium. If it were
        stored untranslated, the restricted model would be offered everywhere."""
        old = list(restriction_rows(FILES_2026.catchment))
        new = list(restriction_rows(files_2027.catchment))
        assert len(new) == 60
        assert new == old

    def test_eu_premiums(self, tmp_path: Path) -> None:
        header = HEADER_2027_PREMIUMS_CH.replace("Kanton", "Land")
        eu = tmp_path / "Prämien_EU.csv"
        eu.write_text(
            f"{header}\n"
            "8,AT,P_OKPEU,2027,2026,PR_REG_0,AKA_03_ERW,MIT_UNF,BASE,BASE,E1,FRASTU_01,"
            "FRA_01_E_0300,123.45,1,1,Grundversicherung\n",
            encoding="utf-8-sig",
        )
        (row,) = premium_rows(eu, territory="EU")
        assert row["country"] == "EU AT"
        assert row["region"] == "PR-REG EU0"
        assert row["age_subgroup"] == ""
        assert row["franchise_chf"] == 300
        assert row["premium_centimes"] == 12345

    def test_errors_name_the_file_and_line(self, files_2027: SourceFiles, tmp_path: Path) -> None:
        text = files_2027.premiums.read_text(encoding="utf-8-sig").replace(
            "AKA_03_ERW", "AKA_04_SEN", 1
        )
        broken = tmp_path / "Prämien_CH.csv"
        broken.write_text(text, encoding="utf-8-sig")
        with pytest.raises(SourceFormatError, match=r"Prämien_CH\.csv:\d+: unknown Altersklasse"):
            list(premium_rows(broken))


class TestHeaderOnlyFiles:
    """What the FOPH publishes in the weeks before a new premium year."""

    def test_premium_year_cannot_be_read_and_says_why(self, tmp_path: Path) -> None:
        empty = tmp_path / "Prämien_CH.csv"
        empty.write_text(HEADER_2027_PREMIUMS_CH + "\n", encoding="utf-8-sig")
        with pytest.raises(EmptySourceFileError, match="no data rows"):
            peek_premium_year(empty)

    def test_is_still_a_source_format_error(self) -> None:
        """Callers that catch SourceFormatError keep working."""
        assert issubclass(EmptySourceFileError, SourceFormatError)

    def test_loaders_yield_nothing(self, tmp_path: Path) -> None:
        for name, header, loader in (
            ("Prämien_CH.csv", HEADER_2027_PREMIUMS_CH, premium_rows),
            ("Tarife.csv", HEADER_2027_TARIFFS, tariff_rows),
            ("Einzugsgebiete.csv", HEADER_2027_CATCHMENT, restriction_rows),
        ):
            path = tmp_path / name
            path.write_text(header + "\n", encoding="utf-8-sig")
            assert list(loader(path)) == []
