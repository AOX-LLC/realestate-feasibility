"""Market pack tests: the shipped packs load, and the schema rejects unsafe or malformed ones."""

import copy
import re
import tomllib
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from feasibility.cli import app
from feasibility.markets.loader import PACKS_DIR, PackError, get_pack, load_pack, pack_paths
from feasibility.markets.schema import MarketPack

PERSONAL_DATA_COLUMNS = [
    "OWNER_NAME1",
    "OWNER_ADDRESS_LINE1",
    "PHONE_NUM",
    "BIZ_NAME",
    "LEGAL1",
    "TAXPAYER_REP",
]

runner = CliRunner()


def dallas_dict() -> dict[str, Any]:
    with (PACKS_DIR / "dallas.toml").open("rb") as pack_file:
        return copy.deepcopy(tomllib.load(pack_file))


@pytest.mark.parametrize("path", pack_paths(), ids=lambda path: path.name)
def test_shipped_pack_loads_and_validates_from_the_cli(path: Path) -> None:
    assert load_pack(path).market.id == path.stem

    result = runner.invoke(app, ["market", "validate", str(path)])

    assert result.exit_code == 0, result.output


def test_dallas_parcel_source_references_exactly_the_four_files() -> None:
    parcels = get_pack("dallas").sources.parcels

    assert set(parcels.files) == {"ACCOUNT_INFO", "ACCOUNT_APPRL_YEAR", "LAND", "RES_DETAIL"}
    assert {spec.file for spec in parcels.fields.values()} <= set(parcels.files)


def test_current_kind_drops_value_fields_and_certified_keeps_them() -> None:
    parcels = get_pack("dallas").sources.parcels
    value_fields = {"land_value", "improvement_value", "total_value"}

    assert value_fields.isdisjoint(parcels.fields_for("current"))
    assert value_fields <= set(parcels.fields_for("certified"))


def test_archive_pattern_extracts_the_year() -> None:
    match = re.match(get_pack("dallas").sources.parcels.archive_pattern, "DCAD2026_CURRENT.ZIP")

    assert match is not None
    assert match.group("year") == "2026"


def test_buy_box_zips_are_five_digits() -> None:
    zips = get_pack("dallas").buy_box.zips

    assert zips
    assert all(len(zip_code) == 5 and zip_code.isdigit() for zip_code in zips)


@pytest.mark.parametrize("column", PERSONAL_DATA_COLUMNS)
def test_personal_data_columns_are_rejected(column: str) -> None:
    data = dallas_dict()
    data["sources"]["parcels"]["fields"]["street_name"]["column"] = column

    with pytest.raises(ValidationError, match="personal-data"):
        MarketPack.model_validate(data)


def test_personal_data_file_member_is_rejected() -> None:
    data = dallas_dict()
    data["sources"]["parcels"]["files"]["LAND"]["member"] = "MULTI_OWNER.CSV"

    with pytest.raises(ValidationError, match="personal-data"):
        MarketPack.model_validate(data)


def test_unknown_top_level_key_is_rejected() -> None:
    data = dallas_dict()
    data["surprise"] = 1

    with pytest.raises(ValidationError):
        MarketPack.model_validate(data)


def test_unknown_key_inside_parcel_source_is_rejected() -> None:
    data = dallas_dict()
    data["sources"]["parcels"]["surprise"] = 1

    with pytest.raises(ValidationError):
        MarketPack.model_validate(data)


def test_field_referencing_an_undeclared_file_is_rejected() -> None:
    data = dallas_dict()
    data["sources"]["parcels"]["fields"]["zoning"]["file"] = "MISSING"

    with pytest.raises(ValidationError, match="undeclared files"):
        MarketPack.model_validate(data)


def test_skip_accounts_on_a_non_base_file_is_rejected() -> None:
    data = dallas_dict()
    data["sources"]["parcels"]["skip_accounts"]["file"] = "LAND"

    with pytest.raises(ValidationError, match="base file"):
        MarketPack.model_validate(data)


def test_archive_pattern_without_a_year_group_is_rejected() -> None:
    data = dallas_dict()
    data["sources"]["parcels"]["archive_pattern"] = r"^DCAD\d{4}_CURRENT\.ZIP$"

    with pytest.raises(ValidationError, match="year"):
        MarketPack.model_validate(data)


def test_market_id_must_match_the_file_name(tmp_path: Path) -> None:
    text = (PACKS_DIR / "dallas.toml").read_text(encoding="utf-8")
    pack_file = tmp_path / "other.toml"
    pack_file.write_text(text, encoding="utf-8")

    with pytest.raises(PackError, match="must match the file name"):
        load_pack(pack_file)


def test_unknown_market_raises_pack_error() -> None:
    with pytest.raises(PackError):
        get_pack("nope")


def test_sum_on_a_text_field_is_rejected() -> None:
    data = dallas_dict()
    data["sources"]["parcels"]["fields"]["zoning"]["aggregate"] = "sum"

    with pytest.raises(ValidationError, match="'sum' needs transform"):
        MarketPack.model_validate(data)


def test_malformed_archive_pattern_is_a_validation_error() -> None:
    data = dallas_dict()
    data["sources"]["parcels"]["archive_pattern"] = "^DCAD(?P<year>\\d{4}"

    with pytest.raises(ValidationError, match="not a valid regex"):
        MarketPack.model_validate(data)


def test_dallas_sourcing_section_loads() -> None:
    sourcing = get_pack("dallas").sourcing

    assert sourcing.source_priority == ["mls", "rentcast"]
    assert sourcing.scoring.land_ratio_weight == 35


SOURCING_BREAKS = [
    ("scoring", "age_weight", "25", "weights must sum to 100"),
    ("scoring", "age_full_year", 1965, "age_full_year"),
    ("scoring", "land_ratio_full", "0.55", "land_ratio_full"),
    ("scoring", "lot_full_sqft", "6000", "lot_full_sqft"),
    ("scoring", "price_land_full", "1.75", "price_land_full"),
]


@pytest.mark.parametrize(("section", "key", "value", "message"), SOURCING_BREAKS)
def test_sourcing_cross_field_rules_reject_one_wrong_field(
    section: str, key: str, value: object, message: str
) -> None:
    data = dallas_dict()
    data["sourcing"][section][key] = value

    with pytest.raises(ValidationError, match=message):
        MarketPack.model_validate(data)


def test_sourcing_section_is_required() -> None:
    data = dallas_dict()
    del data["sourcing"]

    with pytest.raises(ValidationError, match="sourcing"):
        MarketPack.model_validate(data)


def test_unknown_key_inside_sourcing_is_rejected() -> None:
    data = dallas_dict()
    data["sourcing"]["scoring"]["surprise"] = 1

    with pytest.raises(ValidationError):
        MarketPack.model_validate(data)
